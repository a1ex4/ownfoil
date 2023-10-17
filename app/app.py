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

def init():
    global app_settings
    reload_conf()
    update_titledb(app_settings)

os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

from titles import *

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = OWNFOIL_DB
app.config['SECRET_KEY'] = '8accb915665f11dfa15c2db1a4e8026905f57716'

db.init_app(app)

login_manager.init_app(app)

@login_manager.user_loader
def load_user(user_id):
    # since the user_id is just the primary key of our user table, use it in the query for the user
    return User.query.get(int(user_id))

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

@app.route('/')
def index():
    scan_library()

    request_headers = request.headers
    if all(header in request_headers for header in tinfoil_headers):
    # if True:
        print(f"Tinfoil connection from {request.remote_addr}")
        return access_tinfoil_shop(request) 

    return render_template('index.html', games=get_all_titles())

@app.route('/settings')
@access_required('admin')
def settings_page():
    with open(os.path.join(TITLEDB_DIR, 'languages.json')) as f:
        languages = json.load(f)
        languages = dict(sorted(languages.items()))
    return render_template('settings.html', languages_from_titledb=languages)

@app.get('/api/settings')
def get_settings_api():
    reload_conf()
    return jsonify(app_settings)

@app.post('/api/settings/<string:section>')
def set_settings_api(section=None):
    data = request.json
    settings_valid, errors = verify_settings(section, data)
    if settings_valid:
        set_settings(section, data)
        reload_conf()
        if section == 'library':
            update_titledb_files(app_settings)
            load_titledb(app_settings)
    resp = {
        'success': settings_valid,
        'errors': errors
    } 
    return jsonify(resp)

@app.route('/api/titles', methods=['GET'])
def get_all_titles():
    titles = get_all_titles_from_db()
    games_info = []
    for title_id in titles:
        info = get_game_info(title_id)
        if info is None:
            continue
        library_status = get_library_status(title_id)
        info.update(library_status)
        games_info.append(info)

    return sorted(games_info, key=lambda x: (x['name']) )

@app.route('/api/get_game/<int:id>')
def serve_game(id):
    filepath = db.session.query(Files.filepath).filter_by(id=id).first()[0]
    filedir, filename = os.path.split(filepath)
    return send_from_directory(filedir, filename)


def scan_library():
    library = app_settings['library']['path']
    print('Scanning library...')
    if not os.path.isdir(library):
        print(f'Library path {library} does not exists.')
        return
    _, files = getDirsAndFiles(library)
    for filepath in files:
        file_info = identify_file(filepath)

        if file_info is None:
            # TODO add warning
            continue
        add_to_titles_db(library, file_info)


def reload_conf():
    global app_settings
    app_settings = load_settings()

def get_library_status(title_id):
    has_base = False
    has_latest_version = False

    title_files = get_all_title_files(title_id)
    if len(list(filter(lambda x: x.get('type') == 'base', title_files))):
        has_base = True

    available_versions = get_all_existing_versions(title_id)
    if available_versions is None:
        return {
            'has_base': has_base,
            }
    game_latest_version = get_game_latest_version(available_versions)

    for version in available_versions:
        if len(list(filter(lambda x: x.get('type') == 'patch' and str(x.get('version')) == str(version['version']), title_files))):
            version['has_version'] = True
            if str(version['version'])  == str(game_latest_version):
                has_latest_version = True
        else:
            version['has_version'] = False

    library_status = {
        'has_base': has_base,
        'has_latest_version': has_latest_version,
        'version': available_versions
    }
    return library_status
    
    print(json.dumps(available_versions, indent=4))


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