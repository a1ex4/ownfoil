from constants import *
import yaml
import os, sys
import time

from nstools.nut import Keys

import logging

# Retrieve main logger
logger = logging.getLogger('main')

# Cache for settings
_settings_cache = None
_settings_cache_time = 0
_settings_cache_ttl = 5  # Cache for 5 seconds

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

def load_settings(force_reload=False):
    global _settings_cache, _settings_cache_time
    
    current_time = time.time()
    
    # Return cached settings if still valid and not forcing reload
    if not force_reload and _settings_cache is not None and (current_time - _settings_cache_time) < _settings_cache_ttl:
        return _settings_cache
    
    if os.path.exists(CONFIG_FILE):
        logger.debug('Reading configuration file.')
        with open(CONFIG_FILE, 'r') as yaml_file:
            settings = yaml.safe_load(yaml_file)

        if 'security' not in settings:
            settings['security'] = DEFAULT_SETTINGS.get('security', {})
        else:
            defaults = DEFAULT_SETTINGS.get('security', {})
            merged = defaults.copy()
            merged.update(settings.get('security', {}))
            settings['security'] = merged

        if 'downloads' not in settings:
            settings['downloads'] = DEFAULT_SETTINGS.get('downloads', {})
        else:
            defaults = DEFAULT_SETTINGS.get('downloads', {})
            merged = defaults.copy()
            merged.update(settings.get('downloads', {}))
            settings['downloads'] = merged

        valid_keys = load_keys()
        settings['titles']['valid_keys'] = valid_keys

    else:
        settings = DEFAULT_SETTINGS
        with open(CONFIG_FILE, 'w') as yaml_file:
            yaml.dump(settings, yaml_file)
    
    # Update cache
    _settings_cache = settings
    _settings_cache_time = current_time
    
    return settings


def set_security_settings(data):
    settings = load_settings(force_reload=True)
    settings.setdefault('security', {})
    settings['security'].update(data or {})
    with open(CONFIG_FILE, 'w') as yaml_file:
        yaml.dump(settings, yaml_file)
    # Invalidate cache
    global _settings_cache
    _settings_cache = None

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

    settings = load_settings(force_reload=True)
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
    # Invalidate cache
    global _settings_cache
    _settings_cache = None
    return success, errors

def delete_library_path_from_settings(path):
    success = True
    errors = []
    settings = load_settings(force_reload=True)
    library_paths = settings['library']['paths']
    if library_paths:
        if path in library_paths:
            library_paths.remove(path)
            settings['library']['paths'] = library_paths
            with open(CONFIG_FILE, 'w') as yaml_file:
                yaml.dump(settings, yaml_file)
            # Invalidate cache
            global _settings_cache
            _settings_cache = None
        else:
            success = False
            errors.append({
                    'path': 'library/paths',
                    'error': f"Path {path} not configured."
                })
    return success, errors

def set_titles_settings(region, language):
    settings = load_settings(force_reload=True)
    settings['titles']['region'] = region
    settings['titles']['language'] = language
    with open(CONFIG_FILE, 'w') as yaml_file:
        yaml.dump(settings, yaml_file)
    # Invalidate cache
    global _settings_cache
    _settings_cache = None

def set_shop_settings(data):
    settings = load_settings(force_reload=True)
    shop_host = data['host']
    if '://' in shop_host:
        data['host'] = shop_host.split('://')[-1]
    settings['shop'].update(data)
    with open(CONFIG_FILE, 'w') as yaml_file:
        yaml.dump(settings, yaml_file)
    # Invalidate cache
    global _settings_cache
    _settings_cache = None

def set_download_settings(data):
    settings = load_settings(force_reload=True)
    settings.setdefault('downloads', {})
    settings['downloads'].update(data)
    with open(CONFIG_FILE, 'w') as yaml_file:
        yaml.dump(settings, yaml_file)
    # Invalidate cache
    global _settings_cache
    _settings_cache = None
