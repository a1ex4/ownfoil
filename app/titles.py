import os
import sys
import re
import json
import requests

import titledb
from constants import *
from utils import *
from settings import *
from pathlib import Path
from binascii import hexlify as hx, unhexlify as uhx
import logging

from nstools.Fs import Pfs0, Nca, Type, factory
from nstools.lib import FsTools
from nstools.nut import Keys

# Retrieve main logger
logger = logging.getLogger('main')

Pfs0.Print.silent = True

app_id_regex = r"\[([0-9A-Fa-f]{16})\]"
version_regex = r"\[v(\d+)\]"

# Global variables for TitleDB data
identification_in_progress_count = 0
_titles_db_loaded = False
_cnmts_db = None
_titles_db = None
_titles_desc_db = None
_titles_desc_by_title_id = None
_titles_images_by_title_id = None
_versions_db = None
_versions_txt_db = None


def _ensure_titledb_descriptions_file(app_settings):
    """Ensure the descriptions index file exists locally."""
    try:
        desc_url, desc_filename = titledb.get_descriptions_url(app_settings)
        desc_path = os.path.join(TITLEDB_DIR, desc_filename)
        if os.path.isfile(desc_path):
            return desc_path

        os.makedirs(TITLEDB_DIR, exist_ok=True)
        tmp_path = desc_path + '.tmp'
        try:
            logger.info(f"Downloading {desc_filename} from {desc_url}...")
            r = requests.get(desc_url, stream=True, timeout=120)
            r.raise_for_status()
            with open(tmp_path, 'wb') as fp:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        fp.write(chunk)
            os.replace(tmp_path, desc_path)
            return desc_path
        finally:
            try:
                if os.path.isfile(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Failed to ensure TitleDB descriptions: {e}")
        return None

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

def get_title_id_from_app_id(app_id, app_type):
    base_id = app_id[:-3]
    if app_type == APP_TYPE_UPD:
        title_id = base_id + '000'
    elif app_type == APP_TYPE_DLC:
        title_id = hex(int(base_id, base=16) - 1)[2:].rjust(len(base_id), '0') + '000'
    return title_id.upper()

def get_file_size(filepath):
    return os.path.getsize(filepath)

def get_file_info(filepath):
    filedir, filename = os.path.split(filepath)
    extension = filename.split('.')[-1]
    
    compressed = False
    if extension in ['nsz', 'xcz']:
        compressed = True

    return {
        'filepath': filepath,
        'filedir': filedir,
        'filename': filename,
        'extension': extension,
        'compressed': compressed,
        'size': get_file_size(filepath),
    }

def identify_appId(app_id):
    app_id = app_id.lower()
    
    global _cnmts_db
    if _cnmts_db is None:
        logger.warning("cnmts_db is not loaded. Call load_titledb first.")
        return None, None

    if app_id in _cnmts_db:
        app_id_keys = list(_cnmts_db[app_id].keys())
        if len(app_id_keys):
            app = _cnmts_db[app_id][app_id_keys[-1]]
            
            if app['titleType'] == 128:
                app_type = APP_TYPE_BASE
                title_id = app_id.upper()
            elif app['titleType'] == 129:
                app_type = APP_TYPE_UPD
                if 'otherApplicationId' in app:
                    title_id = app['otherApplicationId'].upper()
                else:
                    title_id = get_title_id_from_app_id(app_id, app_type)
            elif app['titleType'] == 130:
                app_type = APP_TYPE_DLC
                if 'otherApplicationId' in app:
                    title_id = app['otherApplicationId'].upper()
                else:
                    title_id = get_title_id_from_app_id(app_id, app_type)
        else:
            logger.warning(f'{app_id} has no keys in cnmts_db, fallback to default identification.')
            if app_id.endswith('000'):
                app_type = APP_TYPE_BASE
                title_id = app_id
            elif app_id.endswith('800'):
                app_type = APP_TYPE_UPD
                title_id = get_title_id_from_app_id(app_id, app_type)
            else:
                app_type = APP_TYPE_DLC
                title_id = get_title_id_from_app_id(app_id, app_type)
    else:
        logger.warning(f'{app_id} not in cnmts_db, fallback to default identification.')
        if app_id.endswith('000'):
            app_type = APP_TYPE_BASE
            title_id = app_id
        elif app_id.endswith('800'):
            app_type = APP_TYPE_UPD
            title_id = get_title_id_from_app_id(app_id, app_type)
        else:
            app_type = APP_TYPE_DLC
            title_id = get_title_id_from_app_id(app_id, app_type)
    
    return title_id.upper(), app_type

def load_titledb():
    global _cnmts_db
    global _titles_db
    global _versions_db
    global _versions_txt_db
    global _titles_desc_db
    global _titles_desc_by_title_id
    global _titles_images_by_title_id
    global identification_in_progress_count
    global _titles_db_loaded

    identification_in_progress_count += 1
    if not _titles_db_loaded:
        logger.info("Loading TitleDBs into memory...")
        app_settings = load_settings()
        
        # Check if TitleDB directory exists and has required files
        if not os.path.isdir(TITLEDB_DIR):
            logger.warning(f"TitleDB directory {TITLEDB_DIR} does not exist. TitleDB files need to be downloaded first.")
            return
        
        cnmts_file = os.path.join(TITLEDB_DIR, 'cnmts.json')
        if not os.path.isfile(cnmts_file):
            logger.warning(f"TitleDB file {cnmts_file} does not exist. TitleDB files need to be downloaded first.")
            return
        
        region_titles_file = os.path.join(TITLEDB_DIR, titledb.get_region_titles_file(app_settings))
        if not os.path.isfile(region_titles_file):
            logger.warning(f"TitleDB file {region_titles_file} does not exist. TitleDB files need to be downloaded first.")
            return
        
        versions_file = os.path.join(TITLEDB_DIR, 'versions.json')
        if not os.path.isfile(versions_file):
            logger.warning(f"TitleDB file {versions_file} does not exist. TitleDB files need to be downloaded first.")
            return
        
        versions_txt_file = os.path.join(TITLEDB_DIR, 'versions.txt')
        if not os.path.isfile(versions_txt_file):
            logger.warning(f"TitleDB file {versions_txt_file} does not exist. TitleDB files need to be downloaded first.")
            return
        
        try:
            with open(cnmts_file, encoding="utf-8") as f:
                _cnmts_db = json.load(f)

            with open(region_titles_file, encoding="utf-8") as f:
                _titles_db = json.load(f)

            _titles_desc_db = None
            _titles_desc_by_title_id = None
            _titles_images_by_title_id = None
            try:
                desc_url, desc_filename = titledb.get_descriptions_url(app_settings)
                desc_path = os.path.join(TITLEDB_DIR, desc_filename)
                if not os.path.isfile(desc_path):
                    desc_path = _ensure_titledb_descriptions_file(app_settings)
                if desc_path and os.path.isfile(desc_path):
                    with open(desc_path, encoding="utf-8") as f:
                        _titles_desc_db = json.load(f)

                    # Build a fast lookup by the actual Nintendo Title ID (the "id" field).
                    # The descriptions file keys are NOT the title id; they are internal ids like 7001...
                    # Keeping only descriptions limits memory usage.
                    by_id = {}
                    images_by_id = {}
                    if isinstance(_titles_desc_db, dict):
                        for _, item in _titles_desc_db.items():
                            if not isinstance(item, dict):
                                continue
                            tid = (item.get('id') or '').strip().upper()
                            if not tid:
                                continue
                            desc = (item.get('description') or '').strip()
                            if desc:
                                by_id[tid] = desc

                            screenshots = item.get('screenshots')
                            if isinstance(screenshots, list):
                                urls = [str(u).strip() for u in screenshots if str(u or '').strip()]
                                if urls:
                                    images_by_id[tid] = urls[:12]
                    _titles_desc_by_title_id = by_id
                    _titles_images_by_title_id = images_by_id
            except Exception as e:
                logger.warning(f"Failed to load TitleDB descriptions: {e}")

            with open(versions_file, encoding="utf-8") as f:
                _versions_db = json.load(f)

            _versions_txt_db = {}
            with open(versions_txt_file, encoding="utf-8") as f:
                for line in f:
                    line_strip = line.rstrip("\n")
                    app_id, rightsId, version = line_strip.split('|')
                    if not version:
                        version = "0"
                    _versions_txt_db[app_id] = version
            _titles_db_loaded = True
            logger.info("TitleDBs loaded.")
        except Exception as e:
            logger.error(f"Failed to load TitleDB files: {e}")
            raise

@debounce(30)
def unload_titledb():
    global _cnmts_db
    global _titles_db
    global _versions_db
    global _versions_txt_db
    global _titles_desc_db
    global _titles_desc_by_title_id
    global _titles_images_by_title_id
    global identification_in_progress_count
    global _titles_db_loaded

    if identification_in_progress_count:
        logger.debug('Identification still in progress, not unloading TitleDB.')
        return

    logger.info("Unloading TitleDBs from memory...")
    _cnmts_db = None
    _titles_db = None
    _versions_db = None
    _versions_txt_db = None
    _titles_desc_db = None
    _titles_desc_by_title_id = None
    _titles_images_by_title_id = None
    _titles_db_loaded = False
    logger.info("TitleDBs unloaded.")

def identify_file_from_filename(filename):
    title_id = None
    app_id = None
    app_type = None
    version = None
    errors = []

    app_id = get_app_id_from_filename(filename)
    if app_id is None:
        errors.append('Could not determine App ID from filename, pattern [APPID] not found. Title ID and Type cannot be derived.')
    else:
        title_id, app_type = identify_appId(app_id)

    version = get_version_from_filename(filename)
    if version is None:
        errors.append('Could not determine version from filename, pattern [vVERSION] not found.')
    
    error = ' '.join(errors)
    return app_id, title_id, app_type, version, error
    
def identify_file_from_cnmt(filepath):
    contents = []
    titleId = None
    version = None
    titleType = None
    container = factory(Path(filepath).resolve())
    container.open(filepath, 'rb')
    if filepath.lower().endswith(('.xci', '.xcz')):
        container = container.hfs0['secure']
    try:
        for nspf in container:
            if isinstance(nspf, Nca.Nca) and nspf.header.contentType == Type.Content.META:
                for section in nspf:
                    if isinstance(section, Pfs0.Pfs0):
                        Cnmt = section.getCnmt()
                        
                        titleType = FsTools.parse_cnmt_type_n(hx(Cnmt.titleType.to_bytes(length=(min(Cnmt.titleType.bit_length(), 1) + 7) // 8, byteorder = 'big')))
                        if titleType == 'GAME':
                            titleType = APP_TYPE_BASE
                        titleId = Cnmt.titleId.upper()
                        version = Cnmt.version
                        contents.append((titleType, titleId, version))
                        # print(f'\n:: CNMT: {Cnmt._path}\n')
                        # print(f'Title ID: {titleId}')
                        # print(f'Version: {version}')
                        # print(f'Title Type: {titleType}')
                        # print(f'Title ID: {titleId} Title Type: {titleType} Version: {version} ')

    finally:
        container.close()

    return contents

def identify_file(filepath):
    filename = os.path.split(filepath)[-1]
    contents = []
    success = True
    error = ''
    if Keys.keys_loaded:
        identification = 'cnmt'
        try:
            cnmt_contents = identify_file_from_cnmt(filepath)
            if not cnmt_contents:
                error = 'No content found in NCA containers.'
                success = False
            else:
                for content in cnmt_contents:
                    app_type, app_id, version = content
                    if app_type != APP_TYPE_BASE:
                        # need to get the title ID from cnmts
                        title_id, app_type = identify_appId(app_id)
                    else:
                        title_id = app_id
                    contents.append((title_id, app_type, app_id, version))
        except Exception as e:
            logger.error(f'Could not identify file {filepath} from metadata: {e}')
            error = str(e)
            success = False

    else:
        identification = 'filename'
        app_id, title_id, app_type, version, error = identify_file_from_filename(filename)
        if not error:
            contents.append((title_id, app_type, app_id, version))
        else:
            success = False

    if contents:
        contents = [{
            'title_id': c[0],
            'app_id': c[2],
            'type': c[1],
            'version': c[3],
            } for c in contents]
    return identification, success, contents, error


def get_game_info(title_id):
    global _titles_db
    global _titles_desc_db
    global _titles_desc_by_title_id
    global _titles_images_by_title_id
    if _titles_db is None:
        logger.warning("titles_db is not loaded. Call load_titledb first.")
        # Return default structure so games can still be displayed
        return {
            'name': 'Unrecognized',
            'bannerUrl': '//placehold.it/400x200',
            'iconUrl': '',
            'id': title_id,
            'category': '',
            'nsuId': None,
            'description': None,
            'screenshots': [],
        }

    try:
        title_info = [_titles_db[t] for t in list(_titles_db.keys()) if _titles_db[t]['id'] == title_id][0]

        description = (title_info.get('description') or '').strip() or None
        if not description and isinstance(_titles_desc_by_title_id, dict):
            try:
                description = (_titles_desc_by_title_id.get(str(title_id).strip().upper()) or '').strip() or None
            except Exception:
                pass

        screenshots = []
        if isinstance(_titles_images_by_title_id, dict):
            try:
                screenshots = _titles_images_by_title_id.get(str(title_id).strip().upper()) or []
            except Exception:
                screenshots = []
        return {
            'name': title_info['name'],
            'bannerUrl': title_info['bannerUrl'],
            'iconUrl': title_info['iconUrl'],
            'id': title_info['id'],
            'category': title_info['category'],
            'nsuId': title_info.get('nsuId'),
            'description': description,
            'screenshots': screenshots,
        }
    except Exception:
        logger.error(f"Title ID not found in titledb: {title_id}")
        return {
            'name': 'Unrecognized',
            'bannerUrl': '//placehold.it/400x200',
            'iconUrl': '',
            'id': title_id + ' not found in titledb',
            'category': '',
            'nsuId': None,
        }


def search_titles(query, limit=20):
    """Search the loaded TitleDB by name or title id.

    Returns a list of lightweight title dicts suitable for UI autocomplete.
    """
    global _titles_db
    if _titles_db is None:
        logger.warning("titles_db is not loaded. Call load_titledb first.")
        return []

    q = (query or '').strip().lower()
    if not q:
        return []

    try:
        limit = int(limit)
    except Exception:
        limit = 20
    limit = max(1, min(limit, 100))

    out = []
    seen_ids = set()
    for _, item in (_titles_db or {}).items():
        try:
            tid = (item.get('id') or '').upper()
            name = (item.get('name') or '').strip()
        except Exception:
            continue
        if not tid or tid in seen_ids:
            continue

        hay = f"{tid} {name}".lower()
        if q not in hay:
            continue

        out.append({
            'id': tid,
            'name': name or 'Unrecognized',
            'category': item.get('category') or '',
            'iconUrl': item.get('iconUrl') or '',
            'bannerUrl': item.get('bannerUrl') or '',
        })
        seen_ids.add(tid)
        if len(out) >= limit:
            break
    return out

def get_update_number(version):
    return int(version)//65536

def get_game_latest_version(all_existing_versions):
    return max(v['version'] for v in all_existing_versions)

def get_all_existing_versions(titleid):
    global _versions_db
    if _versions_db is None:
        logger.warning("versions_db is not loaded. Call load_titledb first.")
        return []

    if not titleid:
        logger.warning("get_all_existing_versions called with None or empty titleid")
        return []

    titleid = titleid.lower()
    if titleid not in _versions_db:
        # print(f'Title ID not in versions.json: {titleid.upper()}')
        return []

    versions_from_db = _versions_db[titleid].keys()
    return [
        {
            'version': int(version_from_db),
            'update_number': get_update_number(version_from_db),
            'release_date': _versions_db[titleid][str(version_from_db)],
        }
        for version_from_db in versions_from_db
    ]

def get_all_app_existing_versions(app_id):
    global _cnmts_db
    if _cnmts_db is None:
        logger.warning("cnmts_db is not loaded. Call load_titledb first.")
        return None

    if not app_id:
        logger.warning("get_all_app_existing_versions called with None or empty app_id")
        return None

    app_id = app_id.lower()
    if app_id in _cnmts_db:
        versions_from_cnmts_db = _cnmts_db[app_id].keys()
        if len(versions_from_cnmts_db):
            return sorted(versions_from_cnmts_db)
        else:
            logger.warning(f'No keys in cnmts.json for app ID: {app_id.upper()}')
            return None
    else:
        # print(f'DLC app ID not in cnmts.json: {app_id.upper()}')
        return None
    
def get_app_id_version_from_versions_txt(app_id):
    global _versions_txt_db
    if _versions_txt_db is None:
        logger.warning("versions_txt_db is not loaded. Call load_titledb first.")
        return None
    if not app_id:
        logger.warning("get_app_id_version_from_versions_txt called with None or empty app_id")
        return None
    return _versions_txt_db.get(app_id, None)
    
def get_all_existing_dlc(title_id):
    global _cnmts_db
    if _cnmts_db is None:
        logger.warning("cnmts_db is not loaded. Call load_titledb first.")
        return []

    if not title_id:
        logger.warning("get_all_existing_dlc called with None or empty title_id")
        return []

    title_id = title_id.lower()
    dlcs = []
    for app_id in _cnmts_db.keys():
        for version, version_description in _cnmts_db[app_id].items():
            if version_description.get('titleType') == 130 and version_description.get('otherApplicationId') == title_id:
                if app_id.upper() not in dlcs:
                    dlcs.append(app_id.upper())
    return dlcs
