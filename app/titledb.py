import unzip_http
import requests
import os, re
import logging

from constants import *


# Retrieve main logger
logger = logging.getLogger('main')

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
        logger.info('Retrieving titledb for the first time...')
        update_available = True
    else: 
        with open(local_commit_file, 'r') as f:
            current_commit = f.read()
            
        if current_commit == latest_remote_commit:
            logger.info(f'Titledb already up to date, commit: {current_commit}')
            update_available = False
        else:
            logger.info(f'Titledb update available, current commit: {current_commit}, latest commit: {latest_remote_commit}')
            update_available = True
    
    if update_available:
        with open(local_commit_file, 'w') as f:
            f.write(latest_remote_commit)
    
    return update_available


def download_titledb_files(rzf, files):
    for file in files:
        store_path = os.path.join(TITLEDB_DIR, file)
        rel_store_path = os.path.relpath(store_path, start=APP_DIR)
        logger.info(f'Downloading {file} from remote titledb to {rel_store_path}')
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
        old_region_titles_files = [f for f in os.listdir(TITLEDB_DIR) if re.match(r"titles\.[A-Z]{2}\.[a-z]{2}\.json", f) and f not in files_to_update]
        files_to_update += old_region_titles_files

    elif not region_titles_file_present:
        files_to_update.append(region_titles_file)

    if len(files_to_update):
        download_titledb_files(rzf, files_to_update)


def update_titledb(app_settings):
    logger.info('Updating titledb...')
    if not os.path.isdir(TITLEDB_DIR):
        os.makedirs(TITLEDB_DIR, exist_ok=True)

    update_titledb_files(app_settings)
    logger.info('titledb update done.')
