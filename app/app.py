from flask import Flask, render_template, request, redirect, url_for, jsonify, send_from_directory
from flask_login import LoginManager
from functools import wraps
import yaml
from file_watcher import Watcher
import threading
from markupsafe import escape
from constants import *
from settings import *
from db import *
from shop import *
from auth import *
from titles import *
import titledb

def init():
    global watcher
    # Create and start the file watcher
    watcher = Watcher([], on_library_change)
    watcher_thread = threading.Thread(target=watcher.run)
    watcher_thread.daemon = True
    watcher_thread.start()

    global app_settings
    # load initial configuration
    reload_conf()

    # Update titledb
    titledb.update_titledb(app_settings)
    load_titledb(app_settings)

os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = OWNFOIL_DB
# TODO: generate random secret_key
app.config['SECRET_KEY'] = '8accb915665f11dfa15c2db1a4e8026905f57716'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Create a global variable and lock
scan_in_progress = False
scan_lock = threading.Lock()

titles_library = []

db.init_app(app)

login_manager.init_app(app)

@login_manager.user_loader
def load_user(user_id):
    # since the user_id is just the primary key of our user table, use it in the query for the user
    return User.query.filter_by(id=user_id).first()

app.register_blueprint(auth_blueprint)

with app.app_context():
    db.create_all()

def tinfoil_error(error):
    return jsonify({
        'error': error
    })

def tinfoil_access(f):
    @wraps(f)
    def _tinfoil_access(*args, **kwargs):
        if not app_settings['shop']['public']:
            # Shop is private
            success, error = basic_auth(request)
            if not success:
                return tinfoil_error(error)
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
        shop = gen_shop(db, app_settings)
        return jsonify(shop)
    
    if all(header in request.headers for header in TINFOIL_HEADERS):
    # if True:
        print(f"Tinfoil connection from {request.remote_addr}")
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
    return render_template('settings.html', title='Settings', languages_from_titledb=languages, admin_account_created=admin_account_created(), valid_keys=app_settings['titles']['valid_keys'])

@app.get('/api/settings')
@access_required('admin')
def get_settings_api():
    reload_conf()
    return jsonify(app_settings)

@app.post('/api/settings/titles')
@access_required('admin')
def set_titles_api():
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
    load_titledb(app_settings)
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
        success, errors = add_library_path_to_settings(data['path'])
        reload_conf()
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
        if watcher.remove_directory(data['path']):
            print(f"Removed {data['path']} from watchdog monitoring")
        else:
            print(f"Failed to remove {data['path']} from watchdog monitoring")
        success, errors = delete_library_path_from_settings(data['path'])
        if success:
            reload_conf()
            success, errors = delete_files_by_library(data['path'])
            generate_library()
        resp = {
            'success': success,
            'errors': errors
        }
    return jsonify(resp)

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ['keys', 'txt']

@app.post('/api/upload')
@access_required('admin')
def upload_file():
    errors = []
    success = False

    file = request.files['file']
    if file and allowed_file(file.filename):
        # filename = secure_filename(file.filename)
        file.save(KEYS_FILE + '.tmp')
        print(f'Validating {file.filename}...')
        valid = load_keys(KEYS_FILE + '.tmp')
        if valid:
            os.rename(KEYS_FILE + '.tmp', KEYS_FILE)
            success = True
            print('Successfully saved valid keys.txt')
            reload_conf()
        else:
            os.remove(KEYS_FILE + '.tmp')
            print(f'Invalid keys from {file.filename}')

    resp = {
        'success': success,
        'errors': errors
    } 
    return jsonify(resp)

def generate_library():
    global titles_library
    titles = get_all_titles_from_db()
    games_info = []
    for title in titles:
        if title['type'] == APP_TYPE_UPD:
            continue
        info_from_titledb = get_game_info(title['app_id'])
        if info_from_titledb is None:
            print(f'Info not found for game:')
            print(title)
            continue
        title.update(info_from_titledb)
        if title['type'] == APP_TYPE_BASE:
            library_status = get_library_status(title['app_id'])
            title.update(library_status)
            title['title_id_name'] = title['name']
        if title['type'] == APP_TYPE_DLC:
            dlc_has_latest_version = None
            all_dlc_existing_versions = get_all_dlc_existing_versions(title['app_id'])

            if all_dlc_existing_versions is not None and len(all_dlc_existing_versions):
                if title['version'] == all_dlc_existing_versions[-1]:
                    dlc_has_latest_version = True
                else:
                    dlc_has_latest_version = False

            else:
                app_id_version_from_versions_txt = get_app_id_version_from_versions_txt(title['app_id'])
                if app_id_version_from_versions_txt is not None:
                    if title['version'] == int(app_id_version_from_versions_txt):
                        dlc_has_latest_version = True
                    else:
                        dlc_has_latest_version = False


            if dlc_has_latest_version is not None:
                title['has_latest_version'] = dlc_has_latest_version

            titleid_info = get_game_info(title['title_id'])
            title['title_id_name'] = titleid_info['name']
        games_info.append(title)
    titles_library = sorted(games_info, key=lambda x: (
        "title_id_name" not in x, 
        x.get("title_id_name", "Unrecognized") or "Unrecognized", 
        x.get('app_id', "") or ""
    ))

@app.route('/api/titles', methods=['GET'])
@access_required('shop')
def get_all_titles():
    global titles_library
    if not titles_library:
        generate_library()

    return jsonify({
        'total': len(titles_library),
        'games': titles_library
    })

@app.route('/api/get_game/<int:id>')
@tinfoil_access
def serve_game(id):
    filepath = db.session.query(Files.filepath).filter_by(id=id).first()[0]
    filedir, filename = os.path.split(filepath)
    return send_from_directory(filedir, filename)


def scan_library():
    print(f'Scanning whole library ...')
    library_paths = app_settings['library']['paths']
    
    if not library_paths:
        print('No library paths configured, nothing to do.')
        return

    for library_path in library_paths:
        scan_library_path(library_path, update_library=False)
    
    # remove missing files
    remove_missing_files()

    # update library
    generate_library()


def scan_library_path(library_path, update_library=True):
    global scan_in_progress
    # Acquire the lock before checking and updating the scan status
    with scan_lock:
        if scan_in_progress:
            print('Scan already in progress')
            return
        # Set the scan status to in progress
        scan_in_progress = True

    try:
        print(f'Scanning library path {library_path} ...')
        if not os.path.isdir(library_path):
            print(f'Library path {library_path} does not exists.')
            return
        _, files = getDirsAndFiles(library_path)

        if app_settings['titles']['valid_keys']:
            current_identification = 'cnmt'
        else:
            print('Invalid or non existing keys.txt, title identification fallback to filename only.')
            current_identification = 'filename'

        all_files_with_current_identification = get_all_files_with_identification(current_identification)
        files_to_identify = [f for f in files if f not in all_files_with_current_identification]
        nb_to_identify = len(files_to_identify)
        for n, filepath in enumerate(files_to_identify):
            file = filepath.replace(library_path, "")
            print(f'Identifiying file ({n+1}/{nb_to_identify}): {file}')

            file_info = identify_file(filepath)

            if file_info is None:
                print(f'Failed to identify: {file} - file will be skipped.')
                continue
            add_to_titles_db(library_path, file_info)
    finally:
        # Ensure the scan status is reset to not in progress, even if an error occurs
        with scan_lock:
            scan_in_progress = False

        if update_library:
            # remove missing files
            remove_missing_files()
            # update library
            generate_library()

@app.post('/api/library/scan')
@access_required('admin')
def scan_library_api():
    global scan_in_progress
    # Acquire the lock before checking and updating the scan status
    if scan_in_progress:
        print('Scan already in progress')
        resp = {
            'success': False,
            'errors': []
        } 
        return resp
    
    data = request.json
    path = data['path']

    if path is None:
        scan_library()
    else:
        scan_library_path(path)

    resp = {
        'success': True,
        'errors': []
    } 
    return jsonify(resp)

def reload_conf():
    global app_settings
    global watcher
    app_settings = load_settings()
    # add library paths to watchdog if necessary
    library_paths = app_settings['library']['paths']
    if library_paths:
        for dir in library_paths:
            if os.path.exists(dir):
                watcher.add_directory(dir)

def get_library_status(title_id):
    has_base = False
    has_latest_version = False

    title_files = get_all_title_files(title_id)
    if len(list(filter(lambda x: x.get('type') == APP_TYPE_BASE, title_files))):
        has_base = True

    available_versions = get_all_existing_versions(title_id)
    if available_versions is None:
        return {
            'has_base': has_base,
            'has_latest_version': True,
            'version': []
        }
    game_latest_version = get_game_latest_version(available_versions)
    for version in available_versions:
        if len(list(filter(lambda x: x.get('type') == APP_TYPE_UPD and str(x.get('version')) == str(version['version']), title_files))):
            version['owned'] = True
            if str(version['version'])  == str(game_latest_version):
                has_latest_version = True
        else:
            version['owned'] = False

    all_existing_dlcs = get_all_existing_dlc(title_id)
    owned_dlcs = [t['app_id'] for t in title_files if t['type'] == APP_TYPE_DLC]
    has_all_dlcs = all(dlc in owned_dlcs for dlc in all_existing_dlcs)

    library_status = {
        'has_base': has_base,
        'has_latest_version': has_latest_version,
        'version': available_versions,
        'has_all_dlcs': has_all_dlcs
    }
    return library_status

def on_library_change(events):
    libraries_changed = set()
    with app.app_context():
        # handle moved files
        for moved_event in events['moved']:
            # if the file has been moved outside of the library
            if not moved_event["dest_path"].startswith(moved_event["directory"]):
                # remove it from the db
                print(delete_file_by_filepath(moved_event["src_path"]))
            else:
                # update the paths
                print(update_file_path(moved_event["src_path"], moved_event["dest_path"]))

        for deleted_event in events['deleted']:
            # delete the file from library if it exists
            print(delete_file_by_filepath(deleted_event["src_path"]))
        
        for created_event in events['created']:
            libraries_changed.add(created_event["directory"])
    
        for library_to_scan in libraries_changed:
            scan_library_path(library_to_scan, update_library=False)
        
        # remove missing files
        remove_missing_files()
        generate_library()


if __name__ == '__main__':
    init()
    app.run(debug=False, host="0.0.0.0", port=8465)

    # with app.app_context():
    #     get_library_status('0100646009FBE000')
    #     title_id='01007EF00011E000'
    #     title_files = get_all_title_files(title_id)
    #     available_versions = get_all_existing_versions(title_id)
    #     # get_library_status(title_id)
    #     print(json.dumps(get_library_status(title_id), indent=4))
