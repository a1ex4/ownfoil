from constants import *
from utils import *
import yaml
import os, sys
import threading
import hashlib

from nsz.nut import Keys

import logging

settings_lock = threading.Lock()
keys_lock = threading.Lock()

# Retrieve main logger
logger = logging.getLogger('main')

def load_keys(key_file=KEYS_FILE):
    with keys_lock:
        valid = None
        missing = Keys.getExistingMasterKeys()
        corrupt = []

        if not os.path.isfile(key_file):
            logger.debug(f'Keys file {key_file} does not exist.')
            return valid, missing, corrupt
        
        with open(key_file, 'rb') as f:
            key_file_checksum = hashlib.sha256(f.read()).hexdigest()
        
        try:
            if Keys.keys_loaded == None or key_file_checksum != Keys.getLoadedKeysChecksum():
                valid = Keys.load(key_file)
                missing = Keys.getMissingMasterKeys()
                corrupt = Keys.getIncorrectKeysRevisions()
            else:
                valid = Keys.keys_loaded
                missing = Keys.getMissingMasterKeys()
                corrupt = Keys.getIncorrectKeysRevisions()
        except:
            logger.error(f'Provided keys file {key_file} is invalid.')
        return valid, missing, corrupt

def remove_obsolete_keys(target, defaults):
    removed = False
    keys_to_remove = [key for key in target if key not in defaults]
    for key in keys_to_remove:
        logger.debug(f"Removing obsolete key: {key}")
        del target[key]
        removed = True

    for key, value in target.items():
        if isinstance(value, dict) and key in defaults and isinstance(defaults[key], dict):
            if remove_obsolete_keys(value, defaults[key]):
                removed = True
    return removed

def load_settings():
    settings_updated = False
    with settings_lock:
        if os.path.exists(CONFIG_FILE):
            logger.debug('Reading configuration file.')
            with open(CONFIG_FILE, 'r') as yaml_file:
                settings = yaml.safe_load(yaml_file)

            # Remove obsolete keys from loaded settings
            if remove_obsolete_keys(settings, DEFAULT_SETTINGS):
                settings_updated = True

            # Merge default settings into loaded settings
            if merge_dicts_recursive(DEFAULT_SETTINGS, settings):
                settings_updated = True

        else:
            settings = DEFAULT_SETTINGS
            settings_updated = True

        if settings_updated:
            with open(CONFIG_FILE, 'w') as yaml_file:
                yaml.dump(settings, yaml_file)
        
        # Get Keys informations
        valid_keys, missing_keys, corrupt_keys = load_keys()
        settings['titles']['valid_keys'] = valid_keys
        settings['titles']['missing_keys'] = missing_keys
        settings['titles']['corrupt_keys'] = corrupt_keys
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
        return success, errors

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
    with settings_lock:
        with open(CONFIG_FILE, 'w') as yaml_file:
            yaml.dump(settings, yaml_file)
    return success, errors

def set_library_management_settings(data):
    settings = load_settings()
    settings['library']['management'].update(data)
    with settings_lock:
        with open(CONFIG_FILE, 'w') as yaml_file:
            yaml.dump(settings, yaml_file)

def delete_library_path_from_settings(path):
    success = True
    errors = []
    settings = load_settings()
    library_paths = settings['library']['paths']
    if library_paths:
        if path in library_paths:
            library_paths.remove(path)
            settings['library']['paths'] = library_paths
            with settings_lock:
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
    with settings_lock:
        with open(CONFIG_FILE, 'w') as yaml_file:
            yaml.dump(settings, yaml_file)

def set_shop_settings(data):
    settings = load_settings()
    shop_host = data['host']
    if '://' in shop_host:
        data['host'] = shop_host.split('://')[-1]
    settings['shop'].update(data)
    with settings_lock:
        with open(CONFIG_FILE, 'w') as yaml_file:
            yaml.dump(settings, yaml_file)
