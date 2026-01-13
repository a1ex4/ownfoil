from flask import Flask, render_template, request, redirect, url_for, jsonify, send_from_directory, Response
from flask_login import LoginManager
from scheduler import init_scheduler
from functools import wraps
from file_watcher import Watcher
import threading
import logging
import sys
import copy
import flask.cli
from datetime import timedelta
flask.cli.show_server_banner = lambda *args: None
from constants import *
from settings import *
from db import *
from shop import *
from auth import *
import titles
from utils import *
from library import *
import titledb
import os
import threading
import time
import uuid
import re

def init():
    global watcher
    global watcher_thread
    # Create and start the file watcher
    logger.info('Initializing File Watcher...')
    watcher = Watcher(on_library_change)
    watcher_thread = threading.Thread(target=watcher.run)
    watcher_thread.daemon = True
    watcher_thread.start()

    # Load initial configuration
    logger.info('Loading initial configuration...')
    reload_conf()

    # init libraries
    library_paths = app_settings['library']['paths']
    init_libraries(app, watcher, library_paths)

     # Initialize job scheduler
    logger.info('Initializing Scheduler...')
    init_scheduler(app)
    
    # Define update_titledb_job
    def update_titledb_job():
        global is_titledb_update_running
        with titledb_update_lock:
            is_titledb_update_running = True
        logger.info("Starting TitleDB update job...")
        try:
            current_settings = load_settings()
            titledb.update_titledb(current_settings)
            logger.info("TitleDB update job completed.")
        except Exception as e:
            logger.error(f"Error during TitleDB update job: {e}")
        finally:
            with titledb_update_lock:
                is_titledb_update_running = False
        
    # Define scan_library_job
    def scan_library_job():
        global is_titledb_update_running
        with titledb_update_lock:
            if is_titledb_update_running:
                logger.info("Skipping scheduled library scan: update_titledb job is currently in progress. Rescheduling in 5 minutes.")
                # Reschedule the job for 5 minutes later
                app.scheduler.add_job(
                    job_id=f'scan_library_rescheduled_{datetime.now().timestamp()}', # Unique ID
                    func=scan_library_job,
                    run_once=True,
                    start_date=datetime.now().replace(microsecond=0) + timedelta(minutes=5)
                )
                return
        logger.info("Starting scheduled library scan job...")
        global scan_in_progress
        with scan_lock:
            if scan_in_progress:
                logger.info(f'Skipping scheduled library scan: scan already in progress.')
                return # Skip the scan if already in progress
            scan_in_progress = True
        try:
            scan_library()
            post_library_change()
            logger.info("Scheduled library scan job completed.")
        except Exception as e:
            logger.error(f"Error during scheduled library scan job: {e}")
        finally:
            with scan_lock:
                scan_in_progress = False

    # Update job: run update_titledb then scan_library once on startup
    def update_db_and_scan_job():
        logger.info("Running update job (TitleDB update and library scan)...")
        update_titledb_job() # This will set/reset the flag
        scan_library_job() # This will check the flag and run if update_titledb_job is done
        logger.info("Update job completed.")

    # Schedule the update job to run immediately and only once
    app.scheduler.add_job(
        job_id='update_db_and_scan',
        func=update_db_and_scan_job,
        interval=timedelta(hours=2),
        run_first=True
    )

os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

## Global variables
app_settings = {}
# Create a global variable and lock for scan_in_progress
scan_in_progress = False
scan_lock = threading.Lock()
# Global flag for titledb update status
is_titledb_update_running = False
titledb_update_lock = threading.Lock()
conversion_jobs = {}
conversion_jobs_lock = threading.Lock()

# Configure logging
formatter = ColoredFormatter(
    '[%(asctime)s.%(msecs)03d] %(levelname)s (%(module)s) %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(formatter)

logging.basicConfig(
    level=logging.INFO,
    handlers=[handler]
)

# Create main logger
logger = logging.getLogger('main')
logger.setLevel(logging.DEBUG)

# Apply filter to hide date from http access logs
logging.getLogger('werkzeug').addFilter(FilterRemoveDateFromWerkzeugLogs())

# Suppress specific Alembic INFO logs
logging.getLogger('alembic.runtime.migration').setLevel(logging.WARNING)

@login_manager.user_loader
def load_user(user_id):
    # since the user_id is just the primary key of our user table, use it in the query for the user
    return User.query.filter_by(id=user_id).first()

def reload_conf():
    global app_settings
    global watcher
    app_settings = load_settings()

def on_library_change(events):
    # TODO refactor: group modified and created together
    with app.app_context():
        created_events = [e for e in events if e.type == 'created']
        modified_events = [e for e in events if e.type != 'created']

        for event in modified_events:
            if event.type == 'moved':
                if file_exists_in_db(event.src_path):
                    # update the path
                    update_file_path(event.directory, event.src_path, event.dest_path)
                else:
                    # add to the database
                    event.src_path = event.dest_path
                    created_events.append(event)

            elif event.type == 'deleted':
                # delete the file from library if it exists
                delete_file_by_filepath(event.src_path)

            elif event.type == 'modified':
                # can happen if file copy has started before the app was running
                add_files_to_library(event.directory, [event.src_path])

        if created_events:
            directories = list(set(e.directory for e in created_events))
            for library_path in directories:
                new_files = [e.src_path for e in created_events if e.directory == library_path]
                add_files_to_library(library_path, new_files)

    post_library_change()

def create_app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = OWNFOIL_DB
    # TODO: generate random secret_key
    app.config['SECRET_KEY'] = '8accb915665f11dfa15c2db1a4e8026905f57716'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)

    app.register_blueprint(auth_blueprint)

    return app

# Create app
app = create_app()


def tinfoil_error(error):
    return jsonify({
        'error': error
    })

def _create_job(kind, total=0):
    job_id = uuid.uuid4().hex
    job = {
        'id': job_id,
        'kind': kind,
        'status': 'running',
        'created_at': time.time(),
        'updated_at': time.time(),
        'progress': {
            'done': 0,
            'total': total,
            'percent': 0,
            'message': ''
        },
        'logs': [],
        'errors': [],
        'summary': None
    }
    with conversion_jobs_lock:
        conversion_jobs[job_id] = job
    return job_id

def _job_log(job_id, message):
    if message is None:
        return
    with conversion_jobs_lock:
        job = conversion_jobs.get(job_id)
        if not job:
            return
        percent_match = re.search(r'Compressed\s+([0-9.]+)%', message)
        if percent_match:
            try:
                job['progress']['percent'] = float(percent_match.group(1))
                job['progress']['message'] = message
            except ValueError:
                pass
        job['logs'].append(message)
        if len(job['logs']) > 500:
            job['logs'] = job['logs'][-500:]
        job['updated_at'] = time.time()

def _job_progress(job_id, done, total):
    with conversion_jobs_lock:
        job = conversion_jobs.get(job_id)
        if not job:
            return
        job['progress']['done'] = done
        job['progress']['total'] = total
        job['updated_at'] = time.time()

def _job_finish(job_id, results):
    with conversion_jobs_lock:
        job = conversion_jobs.get(job_id)
        if not job:
            return
        job['status'] = 'failed' if results.get('errors') else 'success'
        job['errors'] = results.get('errors', [])
        job['summary'] = {
            'converted': results.get('converted', 0),
            'skipped': results.get('skipped', 0),
            'deleted': results.get('deleted', 0),
            'moved': results.get('moved', 0)
        }
        job['updated_at'] = time.time()

def tinfoil_access(f):
    @wraps(f)
    def _tinfoil_access(*args, **kwargs):
        reload_conf()
        hauth_success = None
        auth_success = None
        request.verified_host = None
        # Host verification to prevent hotlinking
        #Tinfoil doesn't send Hauth for file grabs, only directories, so ignore get_game endpoints.
        host_verification = "/api/get_game" not in request.path and (request.is_secure or request.headers.get("X-Forwarded-Proto") == "https")
        if host_verification:
            request_host = request.host
            request_hauth = request.headers.get('Hauth')
            logger.info(f"Secure Tinfoil request from remote host {request_host}, proceeding with host verification.")
            shop_host = app_settings["shop"].get("host")
            shop_hauth = app_settings["shop"].get("hauth")
            if not shop_host:
                logger.error("Missing shop host configuration, Host verification is disabled.")

            elif request_host != shop_host:
                logger.warning(f"Incorrect URL referrer detected: {request_host}.")
                error = f"Incorrect URL `{request_host}`."
                hauth_success = False

            elif not shop_hauth:
                # Try authentication, if an admin user is logging in then set the hauth
                auth_success, auth_error, auth_is_admin =  basic_auth(request)
                if auth_success and auth_is_admin:
                    shop_settings = app_settings['shop']
                    shop_settings['hauth'] = request_hauth
                    set_shop_settings(shop_settings)
                    logger.info(f"Successfully set Hauth value for host {request_host}.")
                    hauth_success = True
                else:
                    logger.warning(f"Hauth value not set for host {request_host}, Host verification is disabled. Connect to the shop from Tinfoil with an admin account to set it.")

            elif request_hauth != shop_hauth:
                logger.warning(f"Incorrect Hauth detected for host: {request_host}.")
                error = f"Incorrect Hauth for URL `{request_host}`."
                hauth_success = False

            else:
                hauth_success = True
                request.verified_host = shop_host

            if hauth_success is False:
                return tinfoil_error(error)
        
        # Now checking auth if shop is private
        if not app_settings['shop']['public']:
            # Shop is private
            if auth_success is None:
                auth_success, auth_error, _ = basic_auth(request)
            if not auth_success:
                return tinfoil_error(auth_error)
        # Auth success
        return f(*args, **kwargs)
    return _tinfoil_access

def access_shop():
    return render_template('index.html', title='Library', admin_account_created=admin_account_created(), valid_keys=app_settings['titles']['valid_keys'])

@access_required('shop')
def access_shop_auth():
    return access_shop()

@app.route('/')
def index():

    @tinfoil_access
    def access_tinfoil_shop():
        shop = {
            "success": app_settings['shop']['motd']
        }
        
        if request.verified_host is not None:
            # enforce client side host verification
            shop["referrer"] = f"https://{request.verified_host}"
            
        shop["files"] = gen_shop_files(db)

        if app_settings['shop']['encrypt']:
            return Response(encrypt_shop(shop), mimetype='application/octet-stream')

        return jsonify(shop)
    
    if all(header in request.headers for header in TINFOIL_HEADERS):
    # if True:
        logger.info(f"Tinfoil connection from {request.remote_addr}")
        return access_tinfoil_shop()
    
    if not app_settings['shop']['public']:
        return access_shop_auth()
    return access_shop()

@app.route('/settings')
@access_required('admin')
def settings_page():
    with open(os.path.join(TITLEDB_DIR, 'languages.json')) as f:
        languages = json.load(f)
        languages = dict(sorted(languages.items()))
    return render_template(
        'settings.html',
        title='Settings',
        languages_from_titledb=languages,
        admin_account_created=admin_account_created(),
        valid_keys=app_settings['titles']['valid_keys'])

@app.route('/manage')
@access_required('admin')
def manage_page():
    return render_template(
        'manage.html',
        title='Manage',
        admin_account_created=admin_account_created())

@app.get('/api/settings')
@access_required('admin')
def get_settings_api():
    reload_conf()
    settings = copy.deepcopy(app_settings)
    if settings['shop'].get('hauth'):
        settings['shop']['hauth'] = True
    else:
        settings['shop']['hauth'] = False
    return jsonify(settings)

@app.post('/api/settings/titles')
@access_required('admin')
def set_titles_settings_api():
    settings = request.json
    region = settings['region']
    language = settings['language']
    with open(os.path.join(TITLEDB_DIR, 'languages.json')) as f:
        languages = json.load(f)
        languages = dict(sorted(languages.items()))

    if region not in languages or language not in languages[region]:
        resp = {
            'success': False,
            'errors': [{
                    'path': 'titles',
                    'error': f"The region/language pair {region}/{language} is not available."
                }]
        }
        return jsonify(resp)
    
    set_titles_settings(region, language)
    reload_conf()
    titledb.update_titledb(app_settings)
    post_library_change()
    resp = {
        'success': True,
        'errors': []
    } 
    return jsonify(resp)

@app.post('/api/settings/shop')
def set_shop_settings_api():
    data = request.json
    set_shop_settings(data)
    reload_conf()
    resp = {
        'success': True,
        'errors': []
    } 
    return jsonify(resp)

@app.post('/api/manage/organize')
@access_required('admin')
def manage_organize_library():
    data = request.json or {}
    dry_run = bool(data.get('dry_run', False))
    verbose = bool(data.get('verbose', False))
    results = organize_library(dry_run=dry_run, verbose=verbose)
    if results.get('success') and not dry_run:
        post_library_change()
    return jsonify(results)

@app.post('/api/manage/delete-updates')
@access_required('admin')
def manage_delete_updates():
    data = request.json or {}
    dry_run = bool(data.get('dry_run', False))
    verbose = bool(data.get('verbose', False))
    results = delete_older_updates(dry_run=dry_run, verbose=verbose)
    if results.get('success') and not dry_run:
        post_library_change()
    return jsonify(results)

@app.post('/api/manage/convert')
@access_required('admin')
def manage_convert_nsz():
    data = request.json or {}
    dry_run = bool(data.get('dry_run', False))
    delete_original = bool(data.get('delete_original', True))
    verbose = bool(data.get('verbose', False))
    threads = data.get('threads')
    command = data.get('command')
    results = convert_to_nsz(
        command_template=command,
        delete_original=delete_original,
        dry_run=dry_run,
        verbose=verbose,
        threads=threads
    )
    if results.get('success') and not dry_run:
        post_library_change()
    return jsonify(results)

@app.get('/api/manage/convertibles')
@access_required('admin')
def manage_convertible_files():
    files = list_convertible_files()
    return jsonify({'success': True, 'files': files})

@app.post('/api/manage/convert-single')
@access_required('admin')
def manage_convert_single():
    data = request.json or {}
    file_id = data.get('file_id')
    dry_run = bool(data.get('dry_run', False))
    delete_original = bool(data.get('delete_original', True))
    verbose = bool(data.get('verbose', False))
    threads = data.get('threads')
    command = data.get('command')
    if not file_id:
        return jsonify({'success': False, 'errors': ['Missing file id.'], 'converted': 0, 'skipped': 0, 'details': []})
    results = convert_single_to_nsz(
        file_id=int(file_id),
        command_template=command,
        delete_original=delete_original,
        dry_run=dry_run,
        verbose=verbose,
        threads=threads
    )
    if results.get('success') and not dry_run:
        post_library_change()
    return jsonify(results)

@app.post('/api/manage/convert-job')
@access_required('admin')
def manage_convert_job():
    data = request.json or {}
    dry_run = bool(data.get('dry_run', False))
    delete_original = bool(data.get('delete_original', True))
    verbose = bool(data.get('verbose', False))
    threads = data.get('threads')
    command = data.get('command')

    job_id = _create_job('convert')

    def _run_job():
        with app.app_context():
            results = convert_to_nsz(
                command_template=command,
                delete_original=delete_original,
                dry_run=dry_run,
                verbose=verbose,
                log_cb=lambda msg: _job_log(job_id, msg),
                progress_cb=lambda done, total: _job_progress(job_id, done, total),
                stream_output=True,
                threads=threads
            )
            if results.get('success') and not dry_run:
                post_library_change()
            _job_finish(job_id, results)

    thread = threading.Thread(target=_run_job, daemon=True)
    thread.start()
    return jsonify({'success': True, 'job_id': job_id})

@app.post('/api/manage/convert-single-job')
@access_required('admin')
def manage_convert_single_job():
    data = request.json or {}
    file_id = data.get('file_id')
    dry_run = bool(data.get('dry_run', False))
    delete_original = bool(data.get('delete_original', True))
    verbose = bool(data.get('verbose', False))
    threads = data.get('threads')
    command = data.get('command')
    if not file_id:
        return jsonify({'success': False, 'errors': ['Missing file id.']})

    job_id = _create_job('convert-single', total=1)

    def _run_job():
        with app.app_context():
            results = convert_single_to_nsz(
                file_id=int(file_id),
                command_template=command,
                delete_original=delete_original,
                dry_run=dry_run,
                verbose=verbose,
                log_cb=lambda msg: _job_log(job_id, msg),
                progress_cb=lambda done, total: _job_progress(job_id, done, total),
                stream_output=True,
                threads=threads
            )
            if results.get('success') and not dry_run:
                post_library_change()
            _job_finish(job_id, results)

    thread = threading.Thread(target=_run_job, daemon=True)
    thread.start()
    return jsonify({'success': True, 'job_id': job_id})

@app.get('/api/manage/convert-job/<job_id>')
@access_required('admin')
def manage_convert_job_status(job_id):
    with conversion_jobs_lock:
        job = conversion_jobs.get(job_id)
        if not job:
            return jsonify({'success': False, 'error': 'Job not found.'}), 404
        return jsonify({'success': True, 'job': job})

@app.route('/api/settings/library/paths', methods=['GET', 'POST', 'DELETE'])
@access_required('admin')
def library_paths_api():
    global watcher
    if request.method == 'POST':
        data = request.json
        success, errors = add_library_complete(app, watcher, data['path'])
        if success:
            reload_conf()
            post_library_change()
        resp = {
            'success': success,
            'errors': errors
        }
    elif request.method == 'GET':
        reload_conf()
        resp = {
            'success': True,
            'errors': [],
            'paths': app_settings['library']['paths']
        }    
    elif request.method == 'DELETE':
        data = request.json
        success, errors = remove_library_complete(app, watcher, data['path'])
        if success:
            reload_conf()
            post_library_change()
        resp = {
            'success': success,
            'errors': errors
        }
    return jsonify(resp)

@app.post('/api/upload')
@access_required('admin')
def upload_file():
    errors = []
    success = False

    file = request.files['file']
    if file and allowed_file(file.filename):
        # filename = secure_filename(file.filename)
        file.save(KEYS_FILE + '.tmp')
        logger.info(f'Validating {file.filename}...')
        valid = load_keys(KEYS_FILE + '.tmp')
        if valid:
            os.rename(KEYS_FILE + '.tmp', KEYS_FILE)
            success = True
            logger.info('Successfully saved valid keys.txt')
            reload_conf()
            post_library_change()
        else:
            os.remove(KEYS_FILE + '.tmp')
            logger.error(f'Invalid keys from {file.filename}')

    resp = {
        'success': success,
        'errors': errors
    } 
    return jsonify(resp)


@app.route('/api/titles', methods=['GET'])
@access_required('shop')
def get_all_titles_api():
    titles_library = generate_library()

    return jsonify({
        'total': len(titles_library),
        'games': titles_library
    })

@app.route('/api/get_game/<int:id>')
@tinfoil_access
def serve_game(id):
    # TODO add download count increment
    filepath = db.session.query(Files.filepath).filter_by(id=id).first()[0]
    filedir, filename = os.path.split(filepath)
    return send_from_directory(filedir, filename)


@debounce(10)
def post_library_change():
    with app.app_context():
        titles.load_titledb()
        process_library_identification(app)
        add_missing_apps_to_db()
        update_titles() # Ensure titles are updated after identification
        # remove missing files
        remove_missing_files_from_db()
        # The process_library_identification already handles updating titles and generating library
        # So, we just need to ensure titles_library is updated from the generated library
        generate_library()
        titles.identification_in_progress_count -= 1
        titles.unload_titledb()

@app.post('/api/library/scan')
@access_required('admin')
def scan_library_api():
    data = request.json
    path = data['path']
    success = True
    errors = []

    global scan_in_progress
    with scan_lock:
        if scan_in_progress:
            logger.info('Skipping scan_library_api call: Scan already in progress')
            return {'success': False, 'errors': []}
    # Set the scan status to in progress
    scan_in_progress = True

    try:
        if path is None:
            scan_library()
        else:
            scan_library_path(path)
    except Exception as e:
        errors.append(e)
        success = False
        logger.error(f"Error during library scan: {e}")
    finally:
        with scan_lock:
            scan_in_progress = False

    post_library_change()
    resp = {
        'success': success,
        'errors': errors
    } 
    return jsonify(resp)

def scan_library():
    logger.info(f'Scanning whole library ...')
    libraries = get_libraries()
    for library in libraries:
        scan_library_path(library.path) # Only scan, identification will be done globally

if __name__ == '__main__':
    logger.info('Starting initialization of Ownfoil...')
    init_db(app)
    init_users(app)
    init()
    logger.info('Initialization steps done, starting server...')
    app.run(debug=False, use_reloader=False, host="0.0.0.0", port=8465)
    # Shutdown server
    logger.info('Shutting down server...')
    watcher.stop()
    watcher_thread.join()
    logger.debug('Watcher thread terminated.')
    # Shutdown scheduler
    app.scheduler.shutdown()
    logger.debug('Scheduler terminated.')
