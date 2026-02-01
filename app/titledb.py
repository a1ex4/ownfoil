import unzip_http
import requests
from requests.exceptions import RequestException, ConnectionError, Timeout
import os, re
import logging
import time

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


def _get_with_retry(url, max_retries=5, backoff_factor=2):
    """Get URL with retry logic for DNS and network errors."""
    # Disable requests' built-in retries to use our own retry logic
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    
    session = requests.Session()
    # Disable automatic retries - we'll handle retries ourselves
    adapter = HTTPAdapter(max_retries=0)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    
    for attempt in range(max_retries):
        try:
            r = session.get(url, allow_redirects=False, timeout=60)
            return r
        except Exception as e:
            error_msg = str(e)
            error_type = type(e).__name__
            
            # Check if it's a DNS/network error
            is_dns_error = (
                'Failed to resolve' in error_msg or 
                'NameResolutionError' in error_type or
                'NameResolutionError' in error_msg or
                isinstance(e, (ConnectionError, RequestException))
            )
            
            if attempt < max_retries - 1:
                wait_time = backoff_factor ** attempt
                if is_dns_error:
                    logger.warning(f'DNS resolution failed for {url}, retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})...')
                elif 'Timeout' in error_type or isinstance(e, Timeout):
                    logger.warning(f'Timeout connecting to {url}, retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})...')
                else:
                    logger.warning(f'Network error connecting to {url}: {error_type}, retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})...')
                time.sleep(wait_time)
            else:
                if is_dns_error:
                    logger.error(f'DNS resolution failed for {url} after {max_retries} attempts. Check your network connection and DNS configuration.')
                else:
                    logger.error(f'Failed to connect to {url} after {max_retries} attempts: {e}')
                raise

def update_titledb_files(app_settings):
    files_to_update = []
    need_descriptions = False
    
    region_titles_file = get_region_titles_file(app_settings)
    try:
        region_titles_file_present = region_titles_file in os.listdir(TITLEDB_DIR)
    except FileNotFoundError:
        # Directory doesn't exist yet
        region_titles_file_present = False

    try:
        r = _get_with_retry(TITLEDB_ARTEFACTS_URL)
        direct_url = r.headers.get('Location') if (300 <= r.status_code < 400) else str(TITLEDB_ARTEFACTS_URL)
        rzf = unzip_http.RemoteZipFile(direct_url)
    except Exception as e:
        logger.error(f'Failed to fetch TitleDB artefacts: {e}')
        raise
    
    if is_titledb_update_available(rzf):
        files_to_update = TITLEDB_DEFAULT_FILES + [region_titles_file]
        need_descriptions = True
        old_region_titles_files = [f for f in os.listdir(TITLEDB_DIR) if re.match(r"titles\.[A-Z]{2}\.[a-z]{2}\.json", f) and f not in files_to_update]
        files_to_update += old_region_titles_files

    elif not region_titles_file_present:
        files_to_update.append(region_titles_file)

    # Ensure we have a local description index (used for game info descriptions).
    if TITLEDB_DESCRIPTIONS_FILE not in os.listdir(TITLEDB_DIR):
        need_descriptions = True

    if len(files_to_update):
        download_titledb_files(rzf, files_to_update)

    # Description index is not part of the nightly artefacts zip; download it directly.
    if need_descriptions:
        try:
            desc_url = TITLEDB_DESCRIPTIONS_URL
            store_path = os.path.join(TITLEDB_DIR, TITLEDB_DESCRIPTIONS_FILE)
            rel_store_path = os.path.relpath(store_path, start=APP_DIR)
            logger.info(f'Downloading {TITLEDB_DESCRIPTIONS_FILE} from {desc_url} to {rel_store_path}')
            r = _get_with_retry(desc_url)
            r.raise_for_status()
            with open(store_path, 'wb') as fp:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        fp.write(chunk)
        except Exception as e:
            logger.warning(f'Failed to download {TITLEDB_DESCRIPTIONS_FILE}: {e}')


def update_titledb(app_settings):
    logger.info('Updating titledb...')
    if not os.path.isdir(TITLEDB_DIR):
        os.makedirs(TITLEDB_DIR, exist_ok=True)

    try:
        update_titledb_files(app_settings)
        logger.info('titledb update done.')
    except Exception as e:
        logger.error(f'TitleDB update failed: {e}')
        logger.warning('TitleDB files may be missing. Some features may not work until TitleDB is successfully downloaded.')
        raise
