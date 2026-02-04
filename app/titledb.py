import unzip_http
import requests
from requests.exceptions import RequestException, ConnectionError, Timeout
import os, re
import json
import email.utils
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


def _get_with_retry(url, max_retries=5, backoff_factor=2, headers=None, method="GET", allow_redirects=False):
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
            r = session.request(method, url, allow_redirects=allow_redirects, timeout=60, headers=headers)
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

def _is_valid_json_file(path):
    try:
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            return False
        with open(path, 'r', encoding='utf-8') as fp:
            json.load(fp)
        return True
    except Exception:
        return False

def _get_json_meta_path(store_path):
    return f"{store_path}.meta"

def _get_descriptions_filename(app_settings):
    try:
        region = str(app_settings['titles']['region']).strip()
        language = str(app_settings['titles']['language']).strip()
        if region and language:
            return f"{region}.{language}.json"
    except Exception:
        pass
    return TITLEDB_DESCRIPTIONS_DEFAULT_FILE

def _get_descriptions_url(app_settings):
    filename = _get_descriptions_filename(app_settings)
    return f"{TITLEDB_DESCRIPTIONS_BASE_URL}/{filename}", filename

def get_descriptions_url(app_settings):
    return _get_descriptions_url(app_settings)

def _load_json_meta(meta_path):
    try:
        if not os.path.isfile(meta_path):
            return {}
        with open(meta_path, 'r', encoding='utf-8') as fp:
            return json.load(fp) or {}
    except Exception:
        return {}

def _save_json_meta(meta_path, headers):
    try:
        meta = {
            "etag": headers.get('ETag'),
            "last_modified": headers.get('Last-Modified'),
            "content_length": headers.get('Content-Length'),
        }
        with open(meta_path, 'w', encoding='utf-8') as fp:
            json.dump(meta, fp)
    except Exception:
        pass

def _build_conditional_headers(store_path, meta):
    headers = {}
    if meta.get('etag'):
        headers['If-None-Match'] = meta['etag']
    if meta.get('last_modified'):
        headers['If-Modified-Since'] = meta['last_modified']
    elif os.path.isfile(store_path):
        mtime = os.path.getmtime(store_path)
        headers['If-Modified-Since'] = email.utils.formatdate(mtime, usegmt=True)
    return headers

def _download_json_file(url, store_path, conditional=False):
    temp_path = f"{store_path}.tmp"
    meta_path = _get_json_meta_path(store_path)
    headers = {}
    if conditional:
        meta = _load_json_meta(meta_path)
        headers = _build_conditional_headers(store_path, meta)

    r = _get_with_retry(url, headers=headers, allow_redirects=True)
    if r is None:
        raise ValueError("No response received for JSON download")
    if r.status_code == 304:
        return False
    r.raise_for_status()
    with open(temp_path, 'wb') as fp:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            if chunk:
                fp.write(chunk)
    if not _is_valid_json_file(temp_path):
        try:
            os.remove(temp_path)
        except Exception:
            pass
        raise ValueError("Downloaded JSON file is invalid or empty")
    os.replace(temp_path, store_path)
    _save_json_meta(meta_path, r.headers)
    return True

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
        r = _get_with_retry(TITLEDB_ARTEFACTS_URL, allow_redirects=False)
        if r is None:
            raise ValueError("No response received for TitleDB artefacts")
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
    desc_url, desc_filename = _get_descriptions_url(app_settings)
    descriptions_path = os.path.join(TITLEDB_DIR, desc_filename)
    descriptions_valid = _is_valid_json_file(descriptions_path)
    need_descriptions = True

    if len(files_to_update):
        download_titledb_files(rzf, files_to_update)

    # Description index is not part of the nightly artefacts zip; download it directly.
    if need_descriptions:
        try:
            store_path = descriptions_path
            rel_store_path = os.path.relpath(store_path, start=APP_DIR)
            logger.info(f'Downloading {desc_filename} from {desc_url} to {rel_store_path}')
            _download_json_file(desc_url, store_path, conditional=descriptions_valid)
        except Exception as e:
            logger.warning(f'Failed to download {desc_filename}: {e}')


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
