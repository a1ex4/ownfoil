import os
import sys
import re
import json

import titledb
from constants import *
from pathlib import Path
from binascii import hexlify as hx, unhexlify as uhx
import logging

sys.path.append(APP_DIR + '/NSTools/py')
from nstools.Fs import Pfs0, Nca, Type, factory
from nstools.lib import FsTools
from nstools.nut import Keys

# Retrieve main logger
logger = logging.getLogger('main')

Pfs0.Print.silent = True

app_id_regex = r"\[([0-9A-Fa-f]{16})\]"
version_regex = r"\[v(\d+)\]"

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
                    title_id = get_title_id_from_app_id(app_id, app_type)
            elif app['titleType'] == 130:
                app_type = APP_TYPE_DLC
                if 'otherApplicationId' in app:
                    title_id = app['otherApplicationId'].upper()
                else:
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

def load_titledb(app_settings):
    global cnmts_db
    global titles_db
    global versions_db
    global versions_txt_db
    with open(os.path.join(TITLEDB_DIR, 'cnmts.json')) as f:
        cnmts_db = json.load(f)

    with open(os.path.join(TITLEDB_DIR, titledb.get_region_titles_file(app_settings))) as f:
        titles_db = json.load(f)

    with open(os.path.join(TITLEDB_DIR, 'versions.json')) as f:
        versions_db = json.load(f)

    versions_txt_db = {}
    with open(os.path.join(TITLEDB_DIR, 'versions.txt')) as f:
        for line in f:
            line_strip = line.rstrip("\n")
            app_id, rightsId, version = line_strip.split('|')
            if not version:
                version = "0"
            versions_txt_db[app_id] = version

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
        logger.error(f"Title ID not found in titledb: {title_id}")
        return {
            'name': 'Unrecognized',
            'bannerUrl': '//placehold.it/400x200',
            'iconUrl': '',
            'id': title_id + ' not found in titledb',
            'category': '',
        }

def get_update_number(version):
    return int(version)//65536

def get_game_latest_version(all_existing_versions):
    return max(v['version'] for v in all_existing_versions)

def get_all_existing_versions(titleid):
    titleid = titleid.lower()
    if titleid not in versions_db:
        # print(f'Title ID not in versions.json: {titleid.upper()}')
        return []

    versions_from_db = versions_db[titleid].keys()
    return [
        {
            'version': int(version_from_db),
            'update_number': get_update_number(version_from_db),
            'release_date': versions_db[titleid][str(version_from_db)],
        }
        for version_from_db in versions_from_db
    ]

def get_all_app_existing_versions(app_id):
    app_id = app_id.lower()
    if app_id in cnmts_db:
        versions_from_cnmts_db = cnmts_db[app_id].keys()
        if len(versions_from_cnmts_db):
            return sorted(versions_from_cnmts_db)
        else:
            logger.warning(f'No keys in cnmts.json for app ID: {app_id.upper()}')
            return None
    else:
        # print(f'DLC app ID not in cnmts.json: {app_id.upper()}')
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
