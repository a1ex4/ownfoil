import unzip_http
import requests
import os

from constants import *


def get_region_titles_file(app_settings):
    return f"titles.{app_settings['titles']['region']}.{app_settings['titles']['language']}.json"

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
    files_to_update = []
    
    region_titles_file = get_region_titles_file(app_settings)
    region_titles_file_present = region_titles_file in os.listdir(TITLEDB_DIR)

    r = requests.get(TITLEDB_ARTEFACTS_URL, allow_redirects = False)
    direct_url = r.next.url
    rzf = unzip_http.RemoteZipFile(direct_url)
    
    if is_titledb_update_available(rzf):
        files_to_update = TITLEDB_DEFAULT_FILES + [region_titles_file]
    elif not region_titles_file_present:
        files_to_update.append(region_titles_file)

    if len(files_to_update):
        download_titledb_files(rzf, files_to_update)


def update_titledb(app_settings):
    print('Updating titledb...')
    if not os.path.isdir(TITLEDB_DIR):
        os.makedirs(TITLEDB_DIR, exist_ok=True)

    update_titledb_files(app_settings)
    print('titledb update done.')
