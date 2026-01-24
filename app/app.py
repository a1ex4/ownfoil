from flask import Flask, render_template, request, redirect, url_for, jsonify, send_from_directory, Response
from flask_login import LoginManager
from sqlalchemy import func
from sqlalchemy.orm import joinedload
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
from downloads import ProwlarrClient, test_torrent_client, run_downloads_job, manual_search_update, queue_download_url, search_update_options, check_completed_downloads
from db import *
from shop import *
from auth import *
import titles
from utils import *
from library import *
from library import _get_nsz_exe
import titledb
import requests
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

    def downloads_job():
        run_downloads_job(scan_cb=scan_library, post_cb=post_library_change)

    # Automatic update downloader job
    app.scheduler.add_job(
        job_id='downloads_update_job',
        func=downloads_job,
        interval=timedelta(minutes=5)
    )
    
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
conversion_job_limit = 50

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
        'cancelled': False,
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
    message = _fix_mojibake(str(message)).strip()
    if not message:
        return
    with conversion_jobs_lock:
        job = conversion_jobs.get(job_id)
        if not job:
            return
        percent_match = re.search(r'Compressed\s+([0-9.]+)%', message)
        numeric_match = re.fullmatch(r'\d{1,3}', message)
        convert_match = re.search(r'^\[CONVERT\]\s+(.+?)\s+->\s+(.+)$', message)
        verify_match = re.search(r'^\[VERIFY\]\s+(.+)$', message)

        if numeric_match:
            try:
                percent_value = int(numeric_match.group(0))
                if 0 <= percent_value <= 100:
                    job['progress']['percent'] = float(percent_value)
                    stage = job['progress'].get('stage') or 'converting'
                    label = 'Verifying' if stage == 'verifying' else 'Converting'
                    job['progress']['message'] = f"{label}: {percent_value}%"
                    job['updated_at'] = time.time()
                    return
            except ValueError:
                pass
        if percent_match:
            try:
                job['progress']['percent'] = float(percent_match.group(1))
                stage = job['progress'].get('stage') or 'converting'
                label = 'Verifying' if stage == 'verifying' else 'Converting'
                job['progress']['message'] = f"{label}: {job['progress']['percent']:.0f}%"
                job['updated_at'] = time.time()
                return
            except ValueError:
                pass

        if convert_match:
            input_path = convert_match.group(1)
            display_name = os.path.basename(input_path)
            message = f"Converting {display_name}..."
            job['progress']['stage'] = 'converting'
            job['progress']['file'] = display_name
            job['progress']['message'] = message
        elif verify_match:
            input_path = verify_match.group(1)
            display_name = os.path.basename(input_path)
            message = f"Verifying {display_name}..."
            job['progress']['stage'] = 'verifying'
            job['progress']['file'] = display_name
            job['progress']['message'] = message
        elif message.startswith("Running:"):
            message = "Starting converter..."
            job['progress']['stage'] = 'converting'
            job['progress']['message'] = message

        if message:
            job['logs'].append(message)
        if len(job['logs']) > 500:
            job['logs'] = job['logs'][-500:]
        job['updated_at'] = time.time()

def _fix_mojibake(text):
    if not text:
        return text
    if "Ã" not in text and "â" not in text:
        return text
    try:
        fixed = text.encode("latin-1").decode("utf-8")
        return fixed if fixed else text
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text

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
        if job.get('cancelled'):
            job['status'] = 'cancelled'
        else:
            job['status'] = 'failed' if results.get('errors') else 'success'
        job['errors'] = results.get('errors', [])
        job['summary'] = {
            'converted': results.get('converted', 0),
            'skipped': results.get('skipped', 0),
            'deleted': results.get('deleted', 0),
            'moved': results.get('moved', 0)
        }
        job['updated_at'] = time.time()
        if len(conversion_jobs) > conversion_job_limit:
            oldest = sorted(conversion_jobs.values(), key=lambda item: item['created_at'])[:len(conversion_jobs) - conversion_job_limit]
            for item in oldest:
                conversion_jobs.pop(item['id'], None)

def _job_is_cancelled(job_id):
    with conversion_jobs_lock:
        job = conversion_jobs.get(job_id)
        return bool(job and job.get('cancelled'))

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
    return render_template(
        'index.html',
        title='Library',
        admin_account_created=admin_account_created(),
        valid_keys=app_settings['titles']['valid_keys'],
        identification_disabled=not app_settings['titles']['valid_keys'],
    )

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
        valid_keys=app_settings['titles']['valid_keys'],
        identification_disabled=not app_settings['titles']['valid_keys'])

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

@app.post('/api/settings/downloads')
@access_required('admin')
def set_download_settings_api():
    data = request.json or {}
    set_download_settings(data)
    reload_conf()
    resp = {
        'success': True,
        'errors': []
    }
    return jsonify(resp)

@app.post('/api/settings/downloads/test-prowlarr')
@access_required('admin')
def test_downloads_prowlarr_api():
    data = request.json or {}
    url = data.get('url', '')
    api_key = data.get('api_key', '')
    try:
        client = ProwlarrClient(url, api_key)
        status = client.system_status()
        indexer_ids = data.get('indexer_ids') or []
        warning = None
        if indexer_ids:
            indexers = client.list_indexers()
            available_ids = {item.get('id') for item in (indexers or [])}
            missing = [idx for idx in indexer_ids if idx not in available_ids]
            if missing:
                warning = f"Missing indexer IDs: {', '.join(str(x) for x in missing)}"
        return jsonify({
            'success': True,
            'message': f"Prowlarr OK ({status.get('version', 'unknown')})",
            'warning': warning
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.post('/api/settings/downloads/test-client')
@access_required('admin')
def test_downloads_client_api():
    data = request.json or {}
    ok, message = test_torrent_client(
        client_type=data.get('type', ''),
        url=data.get('url', ''),
        username=data.get('username', ''),
        password=data.get('password', '')
    )
    download_path = (data.get('download_path') or '').strip()
    warning = None
    if ok and download_path:
        if not os.path.isdir(download_path):
            warning = f"Download path not found: {download_path}"
        elif not os.access(download_path, os.W_OK):
            warning = f"Download path not writable: {download_path}"
    return jsonify({'success': ok, 'message': message, 'warning': warning})

@app.post('/api/downloads/manual')
@access_required('admin')
def manual_download_update():
    data = request.json or {}
    title_id = data.get('title_id')
    version = data.get('version')
    if not title_id or version is None:
        return jsonify({'success': False, 'message': 'Missing title ID or version.'})
    try:
        ok, message = manual_search_update(title_id=title_id, version=version)
        return jsonify({'success': ok, 'message': message})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.post('/api/downloads/manual-search')
@access_required('admin')
def manual_search_update_options():
    data = request.json or {}
    title_id = data.get('title_id')
    version = data.get('version')
    if not title_id or version is None:
        return jsonify({'success': False, 'message': 'Missing title ID or version.', 'results': []})
    try:
        ok, message, results = search_update_options(title_id=title_id, version=version)
        return jsonify({'success': ok, 'message': message, 'results': results})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e), 'results': []})

@app.get('/api/downloads/search')
@access_required('admin')
def downloads_search():
    query = request.args.get('query', '').strip()
    if not query:
        return jsonify({'success': False, 'message': 'Missing query.'})
    settings = load_settings()
    downloads = settings.get('downloads', {})
    prowlarr_cfg = downloads.get('prowlarr', {})
    if not prowlarr_cfg.get('url') or not prowlarr_cfg.get('api_key'):
        return jsonify({'success': False, 'message': 'Prowlarr is not configured.'})
    try:
        prefix = (downloads.get('search_prefix') or '').strip()
        suffix = (downloads.get('search_suffix') or '').strip()
        full_query = query
        if prefix and not full_query.lower().startswith(prefix.lower()):
            full_query = f"{prefix} {full_query}".strip()
        if suffix and not full_query.lower().endswith(suffix.lower()):
            full_query = f"{full_query} {suffix}".strip()
        client = ProwlarrClient(prowlarr_cfg['url'], prowlarr_cfg['api_key'])
        results = client.search(full_query, indexer_ids=prowlarr_cfg.get('indexer_ids') or [])
        trimmed = [
            {
                'title': r.get('title'),
                'size': r.get('size'),
                'seeders': r.get('seeders'),
                'leechers': r.get('leechers'),
                'download_url': r.get('download_url')
            }
            for r in (results or [])[:50]
        ]
        return jsonify({'success': True, 'results': trimmed})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.post('/api/downloads/queue')
@access_required('admin')
def downloads_queue():
    data = request.json or {}
    download_url = data.get('download_url')
    expected_name = data.get('title')
    update_only = bool(data.get('update_only', False))
    expected_version = data.get('expected_version')
    if not download_url:
        return jsonify({'success': False, 'message': 'Missing download URL.'})
    ok, message = queue_download_url(
        download_url,
        expected_name=expected_name,
        update_only=update_only,
        expected_version=expected_version
    )
    return jsonify({'success': ok, 'message': message})

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

@app.post('/api/manage/check-downloads')
@access_required('admin')
def manage_check_downloads():
    ok, message = check_completed_downloads(scan_cb=scan_library, post_cb=post_library_change)
    return jsonify({'success': ok, 'message': message})

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
    library_id = request.args.get('library_id')
    files = list_convertible_files(library_id=int(library_id)) if library_id else list_convertible_files()
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
    library_id = data.get('library_id')
    timeout_seconds = data.get('timeout_seconds')
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
                threads=threads,
                library_id=library_id,
                cancel_cb=lambda: _job_is_cancelled(job_id),
                timeout_seconds=timeout_seconds,
                min_size_bytes=200 * 1024 * 1024
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
    timeout_seconds = data.get('timeout_seconds')
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
                threads=threads,
                cancel_cb=lambda: _job_is_cancelled(job_id),
                timeout_seconds=timeout_seconds
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

@app.post('/api/manage/convert-job/<job_id>/cancel')
@access_required('admin')
def manage_convert_job_cancel(job_id):
    with conversion_jobs_lock:
        job = conversion_jobs.get(job_id)
        if not job:
            return jsonify({'success': False, 'error': 'Job not found.'}), 404
        job['cancelled'] = True
        job['status'] = 'cancelled'
        job['updated_at'] = time.time()
    return jsonify({'success': True})

@app.get('/api/manage/jobs')
@access_required('admin')
def manage_jobs_list():
    limit = request.args.get('limit', 20)
    try:
        limit = int(limit)
    except ValueError:
        limit = 20
    with conversion_jobs_lock:
        jobs = sorted(conversion_jobs.values(), key=lambda item: item['created_at'], reverse=True)[:limit]
        return jsonify({'success': True, 'jobs': jobs})

@app.get('/api/manage/health')
@access_required('admin')
def manage_health():
    nsz_path = None
    try:
        nsz_path = _get_nsz_exe()
    except NameError:
        nsz_path = None
    keys_file = KEYS_FILE
    keys_ok = os.path.exists(KEYS_FILE)
    return jsonify({
        'success': True,
        'nsz_exe': nsz_path,
        'keys_file': keys_file,
        'keys_present': keys_ok
    })

@app.get('/api/manage/libraries')
@access_required('admin')
def manage_libraries_list():
    libraries = get_libraries()
    return jsonify({
        'success': True,
        'libraries': [{'id': lib.id, 'path': lib.path} for lib in libraries]
    })

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

@app.get('/api/library/size')
@access_required('shop')
def get_library_size_api():
    total = db.session.query(func.sum(Files.size)).scalar() or 0
    return jsonify({'success': True, 'total_bytes': int(total)})

@app.route('/api/get_game/<int:id>')
@tinfoil_access
def serve_game(id):
    # TODO add download count increment
    try:
        Files.query.filter_by(id=id).update({Files.download_count: Files.download_count + 1})
        db.session.commit()
    except Exception:
        db.session.rollback()
    filepath = db.session.query(Files.filepath).filter_by(id=id).first()[0]
    filedir, filename = os.path.split(filepath)
    return send_from_directory(filedir, filename)


@app.get('/api/shop/sections')
@tinfoil_access
def shop_sections_api():
    limit = request.args.get('limit', 50)
    try:
        limit = int(limit)
    except ValueError:
        limit = 50

    titles.load_titledb()

    apps = Apps.query.options(
        joinedload(Apps.files),
        joinedload(Apps.title)
    ).filter_by(owned=True).all()

    def _safe_int(value, default=0):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _select_file(app):
        if not app.files:
            return None
        return max(app.files, key=lambda f: f.size or 0)

    def _build_item(app):
        file_obj = _select_file(app)
        if not file_obj:
            return None
        title_id = app.title.title_id if app.title else None
        base_info = titles.get_game_info(title_id) if title_id else None
        app_info = None
        if app.app_type == APP_TYPE_DLC:
            app_info = titles.get_game_info(app.app_id)
        name = (app_info or base_info or {}).get('name') or app.app_id
        title_name = (base_info or {}).get('name') or name
        icon_url = f'/api/shop/icon/{title_id}' if title_id else ''
        return {
            'name': name,
            'title_name': title_name,
            'title_id': title_id,
            'app_id': app.app_id,
            'app_version': app.app_version,
            'app_type': app.app_type,
            'category': (base_info or {}).get('category', ''),
            'icon_url': icon_url,
            'url': f'/api/get_game/{file_obj.id}#{file_obj.filename}',
            'size': file_obj.size or 0,
            'file_id': file_obj.id,
            'filename': file_obj.filename,
            'download_count': file_obj.download_count or 0
        }

    base_apps = [app for app in apps if app.app_type == APP_TYPE_BASE]
    update_apps = [app for app in apps if app.app_type == APP_TYPE_UPD]
    dlc_apps = [app for app in apps if app.app_type == APP_TYPE_DLC]

    base_items = [item for item in (_build_item(app) for app in base_apps) if item]
    base_items.sort(key=lambda item: item['file_id'], reverse=True)
    new_items = base_items[:limit]

    recommended_items = sorted(base_items, key=lambda item: item['download_count'], reverse=True)[:limit]
    if not any(item['download_count'] for item in recommended_items):
        recommended_items = new_items[:limit]

    latest_available_update_by_title = {}
    for app in update_apps:
        title_id = app.title.title_id if app.title else None
        if not title_id:
            continue
        version = _safe_int(app.app_version)
        current_available = latest_available_update_by_title.get(title_id)
        if not current_available or version > current_available['version']:
            latest_available_update_by_title[title_id] = {'version': version, 'app': app}

    update_items_full = []
    for title_id, available in latest_available_update_by_title.items():
        item = _build_item(available['app'])
        if item:
            update_items_full.append(item)
    update_items_full.sort(key=lambda item: _safe_int(item['app_version']), reverse=True)
    update_items = update_items_full

    dlc_by_id = {}
    for app in dlc_apps:
        version = _safe_int(app.app_version)
        current = dlc_by_id.get(app.app_id)
        if not current or version > current['version']:
            dlc_by_id[app.app_id] = {'version': version, 'app': app}
    dlc_items_full = [item for item in (_build_item(entry['app']) for entry in dlc_by_id.values()) if item]
    dlc_items_full.sort(key=lambda item: _safe_int(item['app_version']), reverse=True)
    dlc_items = dlc_items_full[:limit]

    all_items = sorted(base_items + update_items_full + dlc_items_full, key=lambda item: item['name'].lower())

    titles.unload_titledb()

    return jsonify({
        'sections': [
            {'id': 'new', 'title': 'New', 'items': new_items},
            {'id': 'recommended', 'title': 'Recommended', 'items': recommended_items},
            {'id': 'updates', 'title': 'Updates', 'items': update_items},
            {'id': 'dlc', 'title': 'DLC', 'items': dlc_items},
            {'id': 'all', 'title': 'All', 'items': all_items}
        ]
    })


@app.get('/api/shop/icon/<title_id>')
@tinfoil_access
def shop_icon_api(title_id):
    title_id = (title_id or '').upper()
    if not title_id:
        return Response(status=404)

    titles.load_titledb()
    info = titles.get_game_info(title_id)
    titles.unload_titledb()
    icon_url = info.get('iconUrl') if info else ''
    if not icon_url:
        return Response(status=404)
    if icon_url.startswith('//'):
        icon_url = 'https:' + icon_url

    cache_dir = os.path.join(CACHE_DIR, 'icons')
    os.makedirs(cache_dir, exist_ok=True)
    clean_url = icon_url.split('?', 1)[0]
    _, ext = os.path.splitext(clean_url)
    if not ext:
        ext = '.jpg'
    cache_name = f"{title_id}{ext}"
    cache_path = os.path.join(cache_dir, cache_name)
    if not os.path.exists(cache_path):
        try:
            response = requests.get(icon_url, timeout=10)
            if response.status_code == 200:
                with open(cache_path, 'wb') as handle:
                    handle.write(response.content)
        except Exception:
            return Response(status=404)

    if not os.path.exists(cache_path):
        return Response(status=404)
    return send_from_directory(cache_dir, cache_name)


@debounce(10)
def post_library_change():
    with app.app_context():
        titles.load_titledb()
        process_library_identification(app)
        add_missing_apps_to_db()
        update_titles() # Ensure titles are updated after identification
        # remove missing files
        remove_missing_files_from_db()
        organize_pending_downloads()
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
