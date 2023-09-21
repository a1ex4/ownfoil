import requests
import os
import re
import json
import hashlib

title_db_url_template = "https://github.com/blawar/titledb/blob/master/{region}.{language}.json"
version_db_url = "https://github.com/blawar/titledb/blob/master/versions.json"

cnmts_url = "https://github.com/blawar/titledb/raw/master/cnmts.json"
data_dir = './data'

app_id_regex = r"\[([0-9A-Fa-f]{16})\]"
version_regex = r"\[v(\d+)\]"

def getDirsAndFiles(path):
    entries = os.listdir(path)
    allFiles = list()
    allDirs = list()

    for entry in entries:
        fullPath = os.path.join(path, entry)
        if os.path.isdir(fullPath):
            allDirs.append(fullPath)
            dirs, files = getDirsAndFiles(fullPath)
            allDirs += dirs
            allFiles += files
        else:
            if fullPath.split('.')[-1] in ["nsp", "nsz"]:
                allFiles.append(fullPath)
    return allDirs, allFiles

def get_app_id_from_filename(filename):
    app_id = None
    app_id_match = re.search(app_id_regex, filename)
    if app_id_match is not None:
        app_id = app_id_match.group(1)
    return app_id

def get_version_from_filename(filename):
    version = None
    version_match = re.search(version_regex, filename)
    if version_match is not None:
        version = version_match.group(1)
    return version

def get_file_size(filepath):
    return os.path.getsize(filepath)

def identify_appId(app_id):
    app_id = app_id.lower()

    if app_id in cnmts_db:
        app_id_keys = list(cnmts_db[app_id].keys())
        if len(app_id_keys):
            app = cnmts_db[app_id][app_id_keys[-1]]
            
            if app['titleType'] == 128:
                app_type = 'base'
                title_id = app_id.upper()
            elif app['titleType'] == 129:
                app_type = 'patch'
                if 'otherApplicationId' in app:
                    title_id = app['otherApplicationId'].upper()
                else:
                    base_id = app_id[:-3]
                    title_id = [t for t in list(cnmts_db.keys()) if t.startswith(base_id)][0].upper()
            elif app['titleType'] == 130:
                app_type = 'dlc'
                if 'otherApplicationId' in app:
                    title_id = app['otherApplicationId'].upper()
                else:
                    base_id = app_id[:-4]
                    title_id = [t for t in list(cnmts_db.keys()) if t.startswith(base_id)][0].upper()
    
    return title_id, app_type


with open('./data/cnmts.json') as f:
    cnmts_db = json.load(f)

with open('./data/US.en.json') as f:
    titles_db = json.load(f)

with open('./data/versions.json') as f:
    versions_db = json.load(f)

def identify_file(filepath):
    filedir, filename = os.path.split(filepath)
    app_id = get_app_id_from_filename(filename)
    version = get_version_from_filename(filename)
    extension = filename.split('.')[-1]
    try:
        title_id, app_type = identify_appId(app_id)
    except:
        print(filename, app_id)
        return None

    file_app = {
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

    return file_app


def get_game_info(title_id):
    try:
        title_info = [titles_db[t] for t in list(titles_db.keys()) if titles_db[t]['id'] == title_id][0]
        d = {
            'name': title_info['name'],
            'bannerUrl': title_info['bannerUrl'],
            'iconUrl': title_info['iconUrl'],
            'id': title_info['id'],
            'category': title_info['category'],
        }
        return d
    except:
        print("Title ID not found in titledb: " + title_id)
        return None

def convert_nin_version(version):
    return int(version)//65536

def get_game_latest_version(all_existing_versions):
    return max([v['version'] for v in all_existing_versions])

def get_all_existing_versions(titleid):
    titleid = titleid.lower()
    if titleid not in versions_db:
        print('Title ID not in versions.json: ' + titleid.upper())
        return None
    
    all_existing_versions = []
    versions_from_db = versions_db[titleid].keys()
    for version_from_db in versions_from_db:
        all_existing_versions.append(
            {
                'version': int(version_from_db),
                'human_version': convert_nin_version(version_from_db),
                'release_date': versions_db[titleid][str(version_from_db)]
            }
        )

    return all_existing_versions



