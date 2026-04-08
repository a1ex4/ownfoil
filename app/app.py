import datetime
from flask import Flask, render_template, request, redirect, url_for, jsonify, send_from_directory, Response
from flask_login import LoginManager
from functools import wraps
from file_watcher import Watcher
import threading
import logging
import sys
import copy
import flask.cli
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
    # Create the file watcher and register callbacks BEFORE starting the observer
    logger.info('Initializing File Watcher...')
    watcher = Watcher(on_library_change)
    watcher.add_file_callback(CONFIG_FILE, on_settings_change)
    watcher_thread = threading.Thread(target=watcher.run)
    watcher_thread.daemon = True
    watcher_thread.start()

    # init libraries
    library_paths = get_settings()['library']['paths']
    init_libraries(app, watcher, library_paths)

    # Enqueue initial titledb update (re-enqueues itself on completion)
    with app.app_context():
        tasks_mod.enqueue_task('update_titledb')

os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

## Global variables
watcher = None
watcher_thread = None
pool = None  # Set by entrypoint after WorkerPool is created

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

def on_settings_change():
    """Settings file changed: refresh cache in this process and scale worker pool if needed."""
    settings = get_settings()
    if pool is not None:
        desired = max(1, settings.get('worker', {}).get('count', 1))
        if desired != pool.count:
            logger.info(f'Settings changed: scaling workers from {pool.count} to {desired}')
            pool.scale(desired)

def on_library_change(events):
    """Enqueue individual tasks per file event, skipping ignored events."""
    with app.app_context():
        for event in events:
            if event.type == 'moved':
                if pop_ignored_event(src_path=event.src_path, dest_path=event.dest_path):
                    continue
                tasks_mod.enqueue_task('handle_file_moved', {
                    'library_path': event.directory,
                    'src_path': event.src_path,
                    'dest_path': event.dest_path,
                })
            elif event.type == 'deleted':
                if pop_ignored_event(src_path=event.src_path, dest_path=''):
                    continue
                # Also check if this delete is part of a move (dest_path != '')
                if pop_ignored_event(src_path=event.src_path):
                    continue
                tasks_mod.enqueue_task('handle_file_deleted', {
                    'filepath': event.src_path,
                })
            elif event.type == 'created':
                if pop_ignored_event(dest_path=event.src_path):
                    continue
                tasks_mod.enqueue_task('handle_file_added', {
                    'library_path': event.directory,
                    'filepath': event.src_path,
                })
            elif event.type == 'modified':
                tasks_mod.enqueue_task('handle_file_added', {
                    'library_path': event.directory,
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
    for client_class in SUPPORTED_CLIENTS:
        if client_class.identify_client(request):
            return client_class(get_settings())
    return None

def file_access(f):
    """Decorator for file serving endpoints with basic authentication (no client identification required)."""
    @wraps(f)
    def _file_access(*args, **kwargs):
        # Check if shop is private
        if not get_settings()['shop']['public']:
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
        client_settings = get_settings().get('shop', {}).get('clients', {}).get(client_name, {})
        if not client_settings.get('enabled', False):
            logger.warning(f"{client.CLIENT_NAME} connection from {request.remote_addr} - Client is disabled")
            return client.error_response(f"Shop access from {client.CLIENT_NAME} is disabled.")

        # Handle client request
        logger.info(f"{client.CLIENT_NAME} connection from {request.remote_addr}")
        return client.handle_request(request)

    # Browser request - serve web UI
    elif path:
        return redirect('/')

    if not get_settings()['shop']['public']:
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
    settings = get_settings()

    # Check if user has access (must have shop access or shop must be public)
    if not settings['shop']['public'] and admin_account_created():
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        if not current_user.has_shop_access():
            return 'Forbidden', 403

    local_address = None
    local_port  = None

    # Get remote host from configuration
    remote_host = settings['shop'].get('host', '')

    # Check if we're accessing via the configured remote host
    # If so, hide the local tab since we're already remote
    show_local_tab = remote_host and (remote_host != request.host)
    if show_local_tab:
        local_address = request.host.split(':')[0]
        local_port = request.host.split(':')[1] if ':' in request.host else 80

    # Check if clients are enabled
    tinfoil_enabled = settings.get('shop', {}).get('clients', {}).get('tinfoil', {}).get('enabled', False)
    sphaira_enabled = settings.get('shop', {}).get('clients', {}).get('sphaira', {}).get('enabled', False)
    cyberfoil_enabled = settings.get('shop', {}).get('clients', {}).get('cyberfoil', {}).get('enabled', False)

    # Check if shop is public
    shop_public = settings['shop']['public']
    
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
    settings = copy.deepcopy(get_settings())
    # Inject runtime key status for the UI
    valid, missing, corrupt = load_keys()
    settings['titles']['valid_keys'] = valid
    settings['titles']['missing_keys'] = missing
    settings['titles']['corrupt_keys'] = corrupt
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

    current = get_settings()
    if region != current['titles']['region'] or language != current['titles']['language']:
        set_titles_settings(region, language)
        tasks_mod.update_scheduled_task('update_titledb', datetime.datetime.utcnow())

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
            tasks_mod.enqueue_task('scan_library', {'library_path': data['path']})
        resp = {
            'success': success,
            'errors': errors
        }
    elif request.method == 'GET':
        resp = {
            'success': True,
            'errors': [],
            'paths': get_settings()['library']['paths']
        }
    elif request.method == 'DELETE':
        data = request.json
        success, errors = remove_library_complete(app, watcher, data['path'])
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
    tasks_mod.enqueue_task('organize_library')
    resp = {
        'success': True,
        'errors': []
    }
    return jsonify(resp)

@app.post('/api/settings/scheduler')
@access_required('admin')
def set_scheduler_settings_api():
    from utils import interval_string_to_timedelta
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

    if scan_interval_str is not None:
        delta = interval_string_to_timedelta(scan_interval_str)
        run_after = datetime.datetime.utcnow() + delta if delta else None
        tasks_mod.update_scheduled_task('update_titledb', run_after)

    return jsonify({'success': True, 'errors': []})

@app.post('/api/settings/worker')
@access_required('admin')
def set_worker_settings_api():
    data = request.json
    count = data.get('count')
    if count is not None:
        try:
            count = int(count)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'errors': [{'path': 'worker/count', 'error': 'Must be an integer'}]})
        if count < 1:
            return jsonify({'success': False, 'errors': [{'path': 'worker/count', 'error': 'Must be at least 1'}]})
        data['count'] = count
    set_worker_settings(data)
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
                tasks_mod.enqueue_task('identify_library')
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

def _serialize_task(t, detail=False):
    iso = lambda d: d.isoformat() if d else None
    data = {
        'id': t.id,
        'task_name': t.task_name,
        'status': t.status,
        'completion_pct': t.completion_pct,
        'exit_code': t.exit_code,
        'error_message': t.error_message,
        'created_at': iso(t.created_at),
        'started_at': iso(t.started_at),
        'completed_at': iso(t.completed_at),
    }
    if detail:
        data['input'] = json.loads(t.input_json) if t.input_json else {}
        data['output'] = json.loads(t.output_json) if t.output_json else None
    return data


@app.get('/api/tasks')
@access_required('admin')
def list_tasks_api():
    """List top-level tasks (excludes children unless ?include_children=true)."""
    status = request.args.get('status')
    limit = request.args.get('limit', 50, type=int)
    include_children = request.args.get('include_children', 'false').lower() == 'true'
    query = Task.query.order_by(Task.created_at.desc())
    if not include_children:
        query = query.filter(Task.parent_id.is_(None))
    if status:
        query = query.filter_by(status=status)
    return jsonify({'tasks': [_serialize_task(t) for t in query.limit(limit).all()]})


@app.delete('/api/tasks/failed')
@access_required('admin')
def clear_failed_tasks_api():
    deleted = Task.query.filter_by(status='failed').delete()
    db.session.commit()
    return jsonify({'success': True, 'deleted': deleted})


@app.get('/api/tasks/<int:task_id>')
@access_required('admin')
def get_task_api(task_id):
    task = tasks_mod.get_task(task_id)
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    data = _serialize_task(task, detail=True)
    data['children'] = [_serialize_task(c, detail=True) for c in task.children.all()]
    return jsonify(data)

