from flask import Flask, render_template, request, redirect, url_for, jsonify, send_from_directory
from flask_login import LoginManager
import yaml
from markupsafe import escape
from constants import *
from settings import *
from db import *
from shop import *
from auth import *
from titles import *
import titledb

def init():
    global app_settings
    reload_conf()
    titledb.update_titledb(app_settings)

os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = OWNFOIL_DB
# TODO: generate random secret_key
app.config['SECRET_KEY'] = '8accb915665f11dfa15c2db1a4e8026905f57716'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

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

def serve_tinfoil_shop():
    shop = gen_shop(db, app_settings)
    return jsonify(shop)

def access_tinfoil_shop(request):
    if not app_settings['shop']['public']:
        # Shop is private
        success, error = basic_auth(request)
        if not success:
            return tinfoil_error(error)

    return serve_tinfoil_shop()

def access_shop():
    return render_template('index.html', title='Library', games=get_all_titles(), admin_account_created=admin_account_created(), valid_keys=app_settings['valid_keys'])

@access_required('shop')
def access_shop_auth():
    return access_shop()

@app.route('/')
def index():
    scan_library()

    request_headers = request.headers
    if all(header in request_headers for header in TINFOIL_HEADERS):
    # if True:
        print(f"Tinfoil connection from {request.remote_addr}")
        return access_tinfoil_shop(request)
    
    if not app_settings['shop']['public']:
        return access_shop_auth()
    return access_shop()

@app.route('/settings')
@access_required('admin')
def settings_page():
    with open(os.path.join(TITLEDB_DIR, 'languages.json')) as f:
        languages = json.load(f)
        languages = dict(sorted(languages.items()))
    return render_template('settings.html', title='Settings', languages_from_titledb=languages, admin_account_created=admin_account_created(), valid_keys=app_settings['valid_keys'])

@app.get('/api/settings')
@access_required('admin')
def get_settings_api():
    reload_conf()
    return jsonify(app_settings)

@app.post('/api/settings/<string:section>')
@access_required('admin')
def set_settings_api(section=None):
    data = request.json
    settings_valid, errors = verify_settings(section, data)
    if settings_valid:
        set_settings(section, data)
        reload_conf()
        if section == 'library':
            titledb.update_titledb(app_settings)
            load_titledb(app_settings)
    resp = {
        'success': settings_valid,
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

@app.route('/api/titles', methods=['GET'])
@access_required('shop')
def get_all_titles():
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
    return sorted(games_info, key=lambda x: ("title_id_name" not in x, x.get("title_id_name", None), x['app_id']))

@app.route('/api/get_game/<int:id>')
# TODO
# @access_required('shop')
def serve_game(id):
    filepath = db.session.query(Files.filepath).filter_by(id=id).first()[0]
    filedir, filename = os.path.split(filepath)
    return send_from_directory(filedir, filename)


def scan_library():
    library = app_settings['library']['path']
    load_titledb(app_settings)
    print('Scanning library...')
    if not os.path.isdir(library):
        print(f'Library path {library} does not exists.')
        return
    _, files = getDirsAndFiles(library)

    if app_settings['valid_keys']:
        current_identification = 'cnmt'
    else:
        print('Invalid or non existing keys.txt, title identification fallback to filename only.')
        current_identification = 'filename'

    all_files_with_current_identification = get_all_files_with_identification(current_identification)
    files_to_identify = [f for f in files if f not in all_files_with_current_identification]
    nb_to_identify = len(files_to_identify)
    for n, filepath in enumerate(files_to_identify):
        file = filepath.replace(library, "")
        print(f'Identifiying file ({n+1}/{nb_to_identify}): {file}')

        file_info = identify_file(filepath)

        if file_info is None:
            print(f'Failed to identify: {file} - file will be skipped.')
            continue
        add_to_titles_db(library, file_info)


def reload_conf():
    global app_settings
    app_settings = load_settings()

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


if __name__ == '__main__':
    init()
    app.run(debug=True, host="0.0.0.0", port=8465)

    # with app.app_context():
    #     get_library_status('0100646009FBE000')
    #     title_id='01007EF00011E000'
    #     title_files = get_all_title_files(title_id)
    #     available_versions = get_all_existing_versions(title_id)
    #     # get_library_status(title_id)
    #     print(json.dumps(get_library_status(title_id), indent=4))
