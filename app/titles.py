import os
import sys
import re
import json
import hashlib
import unzip_http
import requests
from constants import *
from pathlib import Path
from binascii import hexlify as hx, unhexlify as uhx

sys.path.append(APP_DIR + '/NSTools/py')
from Fs import Pfs0, Nca, Type, factory
from lib import FsTools
from nut import Keys

app_id_regex = r"\[([0-9A-Fa-f]{16})\]"
version_regex = r"\[v(\d+)\]"

def validate_keys(key_file=KEYS_FILE):
    valid = False
    try:
        if os.path.isfile(key_file):
            valid = Keys.load(key_file)
            return valid

    except:
        print('Provided keys.txt invalid.')
    return valid

# valid_keys = validate_keys()

def getDirsAndFiles(path):
    entries = os.listdir(path)
    allFiles = []
    allDirs = []

    for entry in entries:
        fullPath = os.path.join(path, entry)
        if os.path.isdir(fullPath):
            allDirs.append(fullPath)
            dirs, files = getDirsAndFiles(fullPath)
            allDirs += dirs
            allFiles += files
        elif fullPath.split('.')[-1] in ALLOWED_EXTENSIONS:
            allFiles.append(fullPath)
    return allDirs, allFiles

def get_app_id_from_filename(filename):
    app_id_match = re.search(app_id_regex, filename)
    return app_id_match[1] if app_id_match is not None else None

def get_version_from_filename(filename):
    version_match = re.search(version_regex, filename)
    return version_match[1] if version_match is not None else None

def get_file_size(filepath):
    return os.path.getsize(filepath)

def identify_appId(app_id):
    app_id = app_id.lower()

    if app_id in cnmts_db:
        app_id_keys = list(cnmts_db[app_id].keys())
        if len(app_id_keys):
            app = cnmts_db[app_id][app_id_keys[-1]]
            
            if app['titleType'] == 128:
                app_type = APP_TYPE_BASE
                title_id = app_id.upper()
            elif app['titleType'] == 129:
                app_type = APP_TYPE_UPD
                if 'otherApplicationId' in app:
                    title_id = app['otherApplicationId'].upper()
                else:
                    base_id = app_id[:-3]
                    title_id = [t for t in list(cnmts_db.keys()) if t.startswith(base_id)][0].upper()
            elif app['titleType'] == 130:
                app_type = APP_TYPE_DLC
                if 'otherApplicationId' in app:
                    title_id = app['otherApplicationId'].upper()
                else:
                    base_id = app_id[:-4]
                    title_id = [t for t in list(cnmts_db.keys()) if t.startswith(base_id)][0].upper()

    else:
        print(f'WARNING {app_id} not in cnmts_db, fallback to default identification.')
        if app_id.endswith('000'):
            app_type = APP_TYPE_BASE
            title_id = app_id
        elif app_id.endswith('800'):
            app_type = APP_TYPE_UPD
            title_id = app_id[:-3] + '000'
        else:
            app_type = APP_TYPE_DLC
            title_id = app_id[:-3] + '000'
    
    return title_id.upper(), app_type

def load_titledb(app_settings):
    global cnmts_db
    global titles_db
    global versions_db
    global versions_txt_db
    with open(os.path.join(TITLEDB_DIR, 'cnmts.json')) as f:
        cnmts_db = json.load(f)

    with open(os.path.join(TITLEDB_DIR, get_region_titles_file(app_settings))) as f:
        titles_db = json.load(f)

    with open(os.path.join(TITLEDB_DIR, 'versions.json')) as f:
        versions_db = json.load(f)

    versions_txt_db = {}
    with open(os.path.join(TITLEDB_DIR, 'versions.txt')) as f:
        for line in f:
            line_strip = line.rstrip("\n")
            app_id, rightsId, version = line_strip.split('|')
            versions_txt_db[app_id] = version

def identify_file_from_filename(filename):
    version = get_version_from_filename(filename)
    if version is None:
        print(f'Unable to extract version from filename: {filename}')

    app_id = get_app_id_from_filename(filename)
    if app_id is None:
        print(f'Unable to extract Title ID from filename: {filename}')
        return None, None, None, None
    
    title_id, app_type = identify_appId(app_id)
    return app_id, title_id, app_type, version
    
def identify_file_from_cnmt(filepath):
    container = factory(Path(filepath).resolve())
    container.open(filepath, 'rb')
    if filepath.lower().endswith(('.xci', '.xcz')):
        container = container.hfs0['secure']
    for nspf in container:
        if isinstance(nspf, Nca.Nca) and nspf.header.contentType == Type.Content.META:
            for section in nspf:
                if isinstance(section, Pfs0.Pfs0):
                    Cnmt = section.getCnmt()
                    
                    titleType = FsTools.parse_cnmt_type_n(hx(Cnmt.titleType.to_bytes(length=(min(Cnmt.titleType.bit_length(), 1) + 7) // 8, byteorder = 'big')))
                    if titleType == 'GAME':
                        titleType = APP_TYPE_BASE
                    
                    # print(f'\n:: CNMT: {Cnmt._path}\n')
                    # print(f'Title ID: {Cnmt.titleId.upper()}')
                    # print(f'Version: {Cnmt.version}')
                    # print(f'Title Type: {titleType}')
                    return Cnmt.titleId.upper(), Cnmt.version, titleType

def identify_file(filepath, valid_keys=False):
    filedir, filename = os.path.split(filepath)
    extension = filename.split('.')[-1]
    if valid_keys:
        try:
            app_id, version, app_type = identify_file_from_cnmt(filepath)
            if app_type != APP_TYPE_BASE:
                # need to get the title ID from cnmts
                title_id, app_type = identify_appId(app_id)
            else:
                title_id = app_id
        except Exception as e:
            print(f'Could not identify file {filepath} from metadata: {e}. Trying identification with filename...')
            app_id, title_id, app_type, version = identify_file_from_filename(filename)
            if app_id is None:
                print(f'Unable to extract title from filename: {filename}')
                return None

    else:
        app_id, title_id, app_type, version = identify_file_from_filename(filename)
        if app_id is None:
            print(f'Unable to extract title from filename: {filename}')
            return None

    return {
        'filepath': filepath,
        'filedir': filedir,
        'filename': filename,
        'title_id': title_id,
        'app_id': app_id,
        'type': app_type,
        'version': version,
        'extension': extension,
        'size': get_file_size(filepath),
    }


def get_game_info(title_id):
    try:
        title_info = [titles_db[t] for t in list(titles_db.keys()) if titles_db[t]['id'] == title_id][0]
        return {
            'name': title_info['name'],
            'bannerUrl': title_info['bannerUrl'],
            'iconUrl': title_info['iconUrl'],
            'id': title_info['id'],
            'category': title_info['category'],
        }
    except Exception:
        print(f"Title ID not found in titledb: {title_id}")
        return {
            'name': 'Unrecognized',
            'bannerUrl': '//placehold.it/400x200',
            'iconUrl': '',
            'id': title_id + ' not found in titledb',
            'category': '',
        }

def convert_nin_version(version):
    return int(version)//65536

def get_game_latest_version(all_existing_versions):
    return max(v['version'] for v in all_existing_versions)

def get_all_existing_versions(titleid):
    titleid = titleid.lower()
    if titleid not in versions_db:
        # print(f'Title ID not in versions.json: {titleid.upper()}')
        return None

    versions_from_db = versions_db[titleid].keys()
    return [
        {
            'version': int(version_from_db),
            'human_version': convert_nin_version(version_from_db),
            'release_date': versions_db[titleid][str(version_from_db)],
        }
        for version_from_db in versions_from_db
    ]

def get_all_dlc_existing_versions(app_id):
    app_id = app_id.lower()
    if app_id in cnmts_db:
        versions_from_cnmts_db = cnmts_db[app_id].keys()
        if len(versions_from_cnmts_db):
            return sorted(versions_from_cnmts_db)
        else:
            print(f'No keys in cnmts.json for DLC app ID: {app_id.upper()}')
            return None
    else:
        print(f'DLC app ID not in cnmts.json: {app_id.upper()}')
        return None
    
def get_app_id_version_from_versions_txt(app_id):
        return versions_txt_db.get(app_id, None)
    
def get_all_existing_dlc(title_id):
    title_id = title_id.lower()
    dlcs = []
    for app_id in cnmts_db.keys():
        for version, version_description in cnmts_db[app_id].items():
            if version_description.get('titleType') == 130 and version_description.get('otherApplicationId') == title_id:
                if app_id.upper() not in dlcs:
                    dlcs.append(app_id.upper())
    return dlcs

def get_region_titles_file(app_settings):
    return f"titles.{app_settings['library']['region']}.{app_settings['library']['language']}.json"

def download_from_remote_zip(rzf, path, store_path):
    with rzf.open(path) as fpin:
        with open(store_path, mode='wb') as fpout:
            while True:
                r = fpin.read(65536)
                if not r:
                    break
                fpout.write(r)

def is_titledb_update_available(rzf):
    update_available = False
    local_commit_file = os.path.join(TITLEDB_DIR, '.latest')
    remote_latest_commit_file = [ f.filename for f in rzf.infolist() if 'latest_' in f.filename ][0]
    latest_remote_commit = remote_latest_commit_file.split('_')[-1]

    if not os.path.isfile(local_commit_file):
        print('Retrieving titledb for the first time...')
        update_available = True
    else: 
        with open(local_commit_file, 'r') as f:
            current_commit = f.read()
            
        if current_commit == latest_remote_commit:
            print(f'Titledb already up to date, commit: {current_commit}')
            update_available = False
        else:
            print(f'Titledb update available, current commit: {current_commit}, latest commit: {latest_remote_commit}')
            update_available = True
    
    if update_available:
        with open(local_commit_file, 'w') as f:
            f.write(latest_remote_commit)
    
    return update_available

def download_titledb_files(rzf, files):
    for file in files:
        store_path = os.path.join(TITLEDB_DIR, file)
        print(f'Downloading {file} from remote titledb to {store_path}')
        download_from_remote_zip(rzf, file, store_path)


def update_titledb_files(app_settings):
    files_to_update = TITLEDB_DEFAULT_FILES
    region_titles_file = get_region_titles_file(app_settings)
    files_to_update.append(region_titles_file)

    region_titles_file_present = region_titles_file in os.listdir(TITLEDB_DIR)

    r = requests.get(TITLEDB_ARTEFACTS_URL, allow_redirects = False)
    direct_url = r.next.url
    rzf = unzip_http.RemoteZipFile(direct_url)
    if is_titledb_update_available(rzf) or not region_titles_file_present:
        download_titledb_files(rzf, files_to_update)

def update_titledb(app_settings):
    print('Updating titledb...')
    if not os.path.isdir(TITLEDB_DIR):
        os.makedirs(TITLEDB_DIR, exist_ok=True)

    update_titledb_files(app_settings)
    load_titledb(app_settings)
    print('titledb update done.')
