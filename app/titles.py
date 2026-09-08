import os
import re

import titledb
from constants import *
from utils import *
from settings import *
import logging

from nsz.nut import Keys

from containers.cnmt import identify_file_from_cnmt

# Retrieve main logger
logger = logging.getLogger('main')

app_id_regex = r"\[([0-9A-Fa-f]{16})\]"
version_regex = r"\[v(\d+)\]"

# Re-export titledb.store query functions so existing callers
# (titles_lib.get_game_info, titles_lib.get_all_existing_versions, ...) keep working.
get_game_info = titledb.store.get_game_info
get_all_existing_versions = titledb.store.get_all_existing_versions
get_all_app_existing_versions = titledb.store.get_all_app_existing_versions
get_all_existing_dlc = titledb.store.get_all_existing_dlc
get_all_dlc_versions = titledb.store.get_all_dlc_versions

def get_dirs_and_files(path):
    entries = os.listdir(path)
    all_files = []
    all_dirs = []

    for entry in entries:
        full_path = os.path.join(path, entry)
        if os.path.isdir(full_path):
            all_dirs.append(full_path)
            dirs, files = get_dirs_and_files(full_path)
            all_dirs += dirs
            all_files += files
        elif is_library_file(entry):
            all_files.append(full_path)
    return all_dirs, all_files

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
        'mtime': os.path.getmtime(filepath),
    }

def identify_appId(app_id):
    app_id = app_id.lower()

    cnmt = titledb.store.get_cnmt_latest(app_id)
    if cnmt is None:
        logger.warning(f'{app_id} not in cnmts, fallback to default identification.')
        if app_id.endswith('000'):
            return app_id.upper(), APP_TYPE_BASE
        if app_id.endswith('800'):
            return get_title_id_from_app_id(app_id, APP_TYPE_UPD), APP_TYPE_UPD
        return get_title_id_from_app_id(app_id, APP_TYPE_DLC), APP_TYPE_DLC

    title_type = cnmt.get('titleType')
    other_app_id = cnmt.get('otherApplicationId')
    if title_type == 128:
        return app_id.upper(), APP_TYPE_BASE
    if title_type == 129:
        if other_app_id:
            return other_app_id.upper(), APP_TYPE_UPD
        return get_title_id_from_app_id(app_id, APP_TYPE_UPD), APP_TYPE_UPD
    if title_type == 130:
        if other_app_id:
            return other_app_id.upper(), APP_TYPE_DLC
        return get_title_id_from_app_id(app_id, APP_TYPE_DLC), APP_TYPE_DLC

    logger.warning(f'{app_id} has unknown titleType {title_type}, fallback to default identification.')
    if app_id.endswith('000'):
        return app_id.upper(), APP_TYPE_BASE
    if app_id.endswith('800'):
        return get_title_id_from_app_id(app_id, APP_TYPE_UPD), APP_TYPE_UPD
    return get_title_id_from_app_id(app_id, APP_TYPE_DLC), APP_TYPE_DLC

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

def identify_file(filepath):
    filename = os.path.split(filepath)[-1]
    contents = []
    success = True
    error = ''
    if Keys.keys_loaded:
        identification = 'cnmt'
        try:
            cnmt_contents = identify_file_from_cnmt(filepath)
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


def get_update_number(version):
    return int(version)//65536

def get_game_latest_version(all_existing_versions):
    return max(v['version'] for v in all_existing_versions)
