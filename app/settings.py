from constants import *
import yaml
import os, sys

sys.path.append(APP_DIR + '/NSTools/py')
from nstools.nut import Keys

import logging

# Retrieve main logger
logger = logging.getLogger('main')

def load_keys(key_file=KEYS_FILE):
    valid = False
    try:
        if os.path.isfile(key_file):
            valid = Keys.load(key_file)
            return valid
        else:
            logger.debug(f'Keys file {key_file} does not exist.')

    except:
        logger.error(f'Provided keys file {key_file} is invalid.')
    return valid

def load_settings():
    if os.path.exists(CONFIG_FILE):
        logger.debug('Reading configuration file.')
        with open(CONFIG_FILE, 'r') as yaml_file:
            settings = yaml.safe_load(yaml_file)

        valid_keys = load_keys()
        settings['titles']['valid_keys'] = valid_keys

    else:
        settings = DEFAULT_SETTINGS
        with open(CONFIG_FILE, 'w') as yaml_file:
            yaml.dump(settings, yaml_file)
    return settings

def verify_settings(section, data):
    success = True
    errors = []
    if section == 'library':
        # Check that paths exist
        for dir in data['paths']:
            if not os.path.exists(dir):
                success = False
                errors.append({
                    'path': 'library/path',
                    'error': f"Path {dir} does not exists."
                })
                break
    return success, errors

def add_library_path_to_settings(path):
    success = True
    errors = []
    if not os.path.exists(path):
        success = False
        errors.append({
            'path': 'library/paths',
            'error': f"Path {path} does not exists."
        })
    else:
        settings = load_settings()
        library_paths = settings['library']['paths']
        if library_paths:
            if path in library_paths:
                success = False
                errors.append({
                    'path': 'library/paths',
                    'error': f"Path {path} already configured."
                })
                return success, errors
            library_paths.append(path)
        else:
            library_paths = [path]
        settings['library']['paths'] = library_paths
        with open(CONFIG_FILE, 'w') as yaml_file:
            yaml.dump(settings, yaml_file)
    return success, errors

def delete_library_path_from_settings(path):
    success = True
    errors = []
    settings = load_settings()
    library_paths = settings['library']['paths']
    if library_paths:
        if path in library_paths:
            library_paths.remove(path)
            settings['library']['paths'] = library_paths
            with open(CONFIG_FILE, 'w') as yaml_file:
                yaml.dump(settings, yaml_file)
        else:
            success = False
            errors.append({
                    'path': 'library/paths',
                    'error': f"Path {path} not configured."
                })
    return success, errors

def set_titles_settings(region, language):
    settings = load_settings()
    settings['titles']['region'] = region
    settings['titles']['language'] = language
    with open(CONFIG_FILE, 'w') as yaml_file:
        yaml.dump(settings, yaml_file)

def set_shop_settings(data):
    settings = load_settings()
    settings['shop'] = data
    with open(CONFIG_FILE, 'w') as yaml_file:
        yaml.dump(settings, yaml_file)
