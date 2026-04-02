from flask import Flask, render_template, request, redirect, url_for, jsonify, send_from_directory, Response
from flask_login import LoginManager
from scheduler import init_scheduler, validate_interval_string
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
from utils import *
from library import *
import json
import tasks as tasks_mod
import os
from clients import CyberFoilClient, TinfoilClient, SphairaClient

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

    # Initialize and schedule jobs
    logger.info('Initializing Scheduler...')
    init_scheduler(app)
    scan_interval_str = app_settings.get('scheduler', {}).get('scan_interval', '12h')
    schedule_update_and_scan_job(app, scan_interval_str, run_first=True, run_once=True)

os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

## Global variables
app_settings = {}
watcher = None
watcher_thread = None

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
    """Enqueue individual tasks per file event."""
    with app.app_context():
        for event in events:
            if event.type == 'created' or event.type == 'modified':
                tasks_mod.enqueue_task('handle_file_added', {
                    'library_path': event.directory,
                    'filepath': event.src_path,
                })
            elif event.type == 'moved':
                tasks_mod.enqueue_task('handle_file_moved', {
                    'library_path': event.directory,
                    'src_path': event.src_path,
                    'dest_path': event.dest_path,
                })
            elif event.type == 'deleted':
                tasks_mod.enqueue_task('handle_file_deleted', {
                    'filepath': event.src_path,
                })

def create_app(db_uri=None):
    app = Flask(__name__)
    app.url_map.strict_slashes = False  # Disable automatic trailing slash redirects globally, needed for Sphaira
    app.config["SQLALCHEMY_DATABASE_URI"] = db_uri or OWNFOIL_DB
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

# List of supported client classes
SUPPORTED_CLIENTS = [CyberFoilClient, TinfoilClient, SphairaClient]


def get_client_for_request(request):
    """Identify and return the appropriate client for the request, or None if no client matches."""
    reload_conf()
    for client_class in SUPPORTED_CLIENTS:
        if client_class.identify_client(request):
            return client_class(app_settings)
    return None

def file_access(f):
    """Decorator for file serving endpoints with basic authentication (no client identification required)."""
    @wraps(f)
    def _file_access(*args, **kwargs):
        reload_conf()

        # Check if shop is private
        if not app_settings['shop']['public']:
            # Shop is private, require authentication
            auth_success, auth_error, user = basic_auth(request)
            if not auth_success:
                return jsonify({'error': auth_error}), 401
            elif not user.has_shop_access():
                return jsonify({'error': f'User "{user.user}" does not have access to the shop.'}), 403

        return f(*args, **kwargs)
    return _file_access

def access_shop():
    return render_template('index.html', title='Library', admin_account_created=admin_account_created())

@access_required('shop')
def access_shop_auth():
    return access_shop()

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def index(path=None):
    """Main shop endpoint routing to either client-specific shop or web browser UI."""
    # Check if this is a client request
    client = get_client_for_request(request)

    if client:
        # Check if client is enabled
        client_name = client.CLIENT_NAME.lower()
        client_settings = app_settings.get('shop', {}).get('clients', {}).get(client_name, {})
        if not client_settings.get('enabled', False):
            logger.warning(f"{client.CLIENT_NAME} connection from {request.remote_addr} - Client is disabled")
            return client.error_response(f"Shop access from {client.CLIENT_NAME} is disabled.")
        
        # Handle client request
        logger.info(f"{client.CLIENT_NAME} connection from {request.remote_addr}")
        return client.handle_request(request)

    # Browser request - serve web UI
    elif path:
        return redirect('/')

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
        admin_account_created=admin_account_created())

@app.route('/setup')
def setup_page():
    """Setup page showing client information and connection instructions."""
    reload_conf()
    
    # Check if user has access (must have shop access or shop must be public)
    if not app_settings['shop']['public'] and admin_account_created():
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        if not current_user.has_shop_access():
            return 'Forbidden', 403

    local_address = None
    local_port  = None
    
    # Get remote host from configuration
    remote_host = app_settings['shop'].get('host', '')
    
    # Check if we're accessing via the configured remote host
    # If so, hide the local tab since we're already remote
    show_local_tab = remote_host and (remote_host != request.host)
    if show_local_tab:
        local_address = request.host.split(':')[0]
        local_port = request.host.split(':')[1] if ':' in request.host else 80
    
    # Check if clients are enabled
    tinfoil_enabled = app_settings.get('shop', {}).get('clients', {}).get('tinfoil', {}).get('enabled', False)
    sphaira_enabled = app_settings.get('shop', {}).get('clients', {}).get('sphaira', {}).get('enabled', False)
    cyberfoil_enabled = app_settings.get('shop', {}).get('clients', {}).get('cyberfoil', {}).get('enabled', False)
    
    # Check if shop is public
    shop_public = app_settings['shop']['public']
    
    return render_template(
        'setup.html',
        title='Setup',
        local_address=local_address,
        local_port=local_port,
        remote_host=remote_host,
        show_local_tab=show_local_tab,
        tinfoil_enabled=tinfoil_enabled,
        sphaira_enabled=sphaira_enabled,
        cyberfoil_enabled=cyberfoil_enabled,
        shop_public=shop_public,
        admin_account_created=admin_account_created()
    )

@app.get('/api/settings')
@access_required('admin')
def get_settings_api():
    reload_conf()
    settings = copy.deepcopy(app_settings)
    # Strip hauth values for privacy (don't send to client)
    if 'clients' in settings['shop']:
        for client_name, client_settings in settings['shop']['clients'].items():
            if 'hauth' in client_settings:
                # Replace hauth dict with empty dict to keep it private
                settings['shop']['clients'][client_name]['hauth'] = {}
    return jsonify(settings)

@app.post('/api/settings/titles')
@access_required('admin')
def set_titles_settings_api():
    reload_conf()
    title_settings = request.json
    region = title_settings['region']
    language = title_settings['language']
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

    if region != app_settings['titles']['region'] or language != app_settings['titles']['language']:
        set_titles_settings(region, language)
        reload_conf()
        tasks_mod.enqueue_task('update_titledb')

    resp = {
        'success': True,
        'errors': []
    } 
    return jsonify(resp)

@app.post('/api/settings/shop')
@access_required('admin')
def set_shop_settings_api():
    data = request.json
    set_shop_settings(data)
    reload_conf()
    resp = {
        'success': True,
        'errors': []
    } 
    return jsonify(resp)

@app.route('/api/settings/library/paths', methods=['GET', 'POST', 'DELETE'])
@access_required('admin')
def library_paths_api():
    global watcher
    if request.method == 'POST':
        data = request.json
        success, errors = add_library_complete(app, watcher, data['path'])
        if success:
            reload_conf()
            tasks_mod.enqueue_task('scan_library', {'library_path': data['path']})
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
            tasks_mod.enqueue_task('update_titles')
        resp = {
            'success': success,
            'errors': errors
        }
    return jsonify(resp)

@app.post('/api/settings/library/management')
@access_required('admin')
def set_library_management_settings_api():
    data = request.json
    set_library_management_settings(data)
    reload_conf()
    tasks_mod.enqueue_task('organize_library')
    resp = {
        'success': True,
        'errors': []
    }
    return jsonify(resp)

@app.post('/api/settings/scheduler')
@access_required('admin')
def set_scheduler_settings_api():
    data = request.json
    scan_interval_str = data.get('scan_interval')

    if scan_interval_str is not None:
        is_valid, error_msg = validate_interval_string(scan_interval_str)
        if not is_valid:
            return jsonify({
                'success': False,
                'errors': [{'path': 'scheduler/scan_interval', 'error': error_msg}]
            })

    set_scheduler_settings(data)
    reload_conf()

    if scan_interval_str is not None:
        try:
            current_interval_str = app_settings.get('scheduler', {}).get('scan_interval', '12h')
            schedule_update_and_scan_job(app, current_interval_str, run_first=False)
        except Exception as e:
            logger.error(f"Error updating scheduler: {e}")
            return jsonify({
                'success': False,
                'errors': [{'path': 'scheduler', 'error': str(e)}]
            })

    return jsonify({'success': True, 'errors': []})

@app.post('/api/upload')
@access_required('admin')
def upload_file():
    errors = []
    success = False
    valid_keys = None
    try:
        file = request.files['file']
        if file and allowed_file(file.filename):
            # filename = secure_filename(file.filename)
            file.save(KEYS_FILE)
            logger.info(f'Validating {file.filename}...')
            valid_keys, missing_keys, corrupt_keys = load_keys(KEYS_FILE)
            if valid_keys:
                for lib in get_libraries():
                    tasks_mod.enqueue_task('identify_library', {'library_path': lib.path})
            else:
                logger.warning(f'Invalid keys from {file.filename}')
            success = True
            logger.info('Successfully saved keys.txt')

    except Exception as e:
        logger.error(f'Failed to upload console keys file: {e}')
        os.remove(KEYS_FILE)
        success = False
        errors.append(str(e))

    resp = {
        'success': success,
        'errors': errors,
        'data': {}
    }

    if valid_keys is not None:
        resp['data']['valid_keys'] = valid_keys
        resp['data']['missing_keys'] = missing_keys
        resp['data']['corrupt_keys'] = corrupt_keys

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
@file_access
def serve_game(id):
    """Serve a game file to authenticated clients."""
    filepath = db.session.query(Files.filepath).filter_by(id=id).first()[0]
    filedir, filename = os.path.split(filepath)
    increment_download_count_throttled(filepath, request.remote_addr)
    return send_from_directory(filedir, filename)



@app.post('/api/library/scan')
@access_required('admin')
def scan_library_api():
    data = request.json
    path = data.get('path')
    if path:
        tasks_mod.enqueue_task('scan_library', {'library_path': path})
    else:
        for lib in get_libraries():
            tasks_mod.enqueue_task('scan_library', {'library_path': lib.path})
    return jsonify({'success': True, 'errors': []})


# --- Task Queue API ---

@app.post('/api/tasks')
@access_required('admin')
def enqueue_task_api():
    data = request.json
    task_name = data.get('task_name')
    input_data = data.get('input', {})
    try:
        task, created = tasks_mod.enqueue_task(task_name, input_data)
        return jsonify({
            'success': True,
            'task_id': task.id,
            'created': created,
            'status': task.status,
        }), 201 if created else 200
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.get('/api/tasks')
@access_required('admin')
def list_tasks_api():
    """List top-level tasks (excludes children unless ?include_children=true)."""
    status = request.args.get('status')
    limit = request.args.get('limit', 50, type=int)
    include_children = request.args.get('include_children', 'false').lower() == 'true'
    query = tasks_mod.Task.query.order_by(tasks_mod.Task.created_at.desc())
    if not include_children:
        query = query.filter(tasks_mod.Task.parent_id.is_(None))
    if status:
        query = query.filter_by(status=status)
    task_list = query.limit(limit).all()
    return jsonify({
        'tasks': [{
            'id': t.id,
            'task_name': t.task_name,
            'status': t.status,
            'completion_pct': t.completion_pct,
            'exit_code': t.exit_code,
            'error_message': t.error_message,
            'created_at': t.created_at.isoformat() if t.created_at else None,
            'started_at': t.started_at.isoformat() if t.started_at else None,
            'completed_at': t.completed_at.isoformat() if t.completed_at else None,
        } for t in task_list]
    })

@app.get('/api/tasks/<int:task_id>')
@access_required('admin')
def get_task_api(task_id):
    task = tasks_mod.get_task(task_id)
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    children = [{
        'id': c.id,
        'task_name': c.task_name,
        'status': c.status,
        'input': json.loads(c.input_json) if c.input_json else {},
        'exit_code': c.exit_code,
        'error_message': c.error_message,
        'completed_at': c.completed_at.isoformat() if c.completed_at else None,
    } for c in task.children.all()]

    return jsonify({
        'id': task.id,
        'task_name': task.task_name,
        'status': task.status,
        'completion_pct': task.completion_pct,
        'input': json.loads(task.input_json) if task.input_json else {},
        'output': json.loads(task.output_json) if task.output_json else None,
        'exit_code': task.exit_code,
        'error_message': task.error_message,
        'created_at': task.created_at.isoformat() if task.created_at else None,
        'started_at': task.started_at.isoformat() if task.started_at else None,
        'completed_at': task.completed_at.isoformat() if task.completed_at else None,
        'children': children,
    })

def _enqueue_update_titledb():
    """Scheduler callback — enqueues an update_titledb task."""
    with app.app_context():
        tasks_mod.enqueue_task('update_titledb')

def schedule_update_and_scan_job(app: Flask, interval_str: str, run_first: bool = True, run_once: bool = False):
    """Schedule or update the update_and_scan job."""
    app.scheduler.update_job_interval(
        job_id='update_db_and_scan',
        interval_str=interval_str,
        func=_enqueue_update_titledb,
        run_first=run_first,
        run_once=run_once
    )


if __name__ == '__main__':
    from multiprocessing import Process, Event as MPEvent
    from worker import start_worker_process

    logger.info('Starting initialization of Ownfoil...')
    init_db(app)
    init_users(app)
    init()

    # Start worker process
    worker_stop_event = MPEvent()
    worker_process = Process(target=start_worker_process, args=(worker_stop_event,), daemon=True)
    worker_process.start()
    logger.info('Worker process started.')

    logger.info('Initialization steps done, starting server...')
    app.run(debug=False, use_reloader=False, host="0.0.0.0", port=8465)

    # Shutdown
    logger.info('Shutting down server...')
    worker_stop_event.set()
    worker_process.join(timeout=10)
    if worker_process.is_alive():
        worker_process.terminate()
    logger.debug('Worker process terminated.')
    watcher.stop()
    watcher_thread.join()
    logger.debug('Watcher thread terminated.')
    app.scheduler.shutdown()
    logger.debug('Scheduler terminated.')
