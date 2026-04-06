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

_cached_settings = None
_cached_mtimes = (None, None)

def _safe_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None

def get_settings():
    """Return settings, re-reading when settings.yaml or keys.txt mtime changes."""
    global _cached_settings, _cached_mtimes
    mtimes = (_safe_mtime(CONFIG_FILE), _safe_mtime(KEYS_FILE))
    if _cached_settings is None or mtimes != _cached_mtimes:
        _cached_settings = load_settings()
        _cached_mtimes = mtimes
    return _cached_settings

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

def remove_obsolete_keys(target, defaults, path=''):
    removed = False
    keys_to_remove = [key for key in target if key not in defaults]
    for key in keys_to_remove:
        logger.debug(f"Removing obsolete key: {key}")
        del target[key]
        removed = True

    for key, value in target.items():
        if isinstance(value, dict) and key in defaults and isinstance(defaults[key], dict):
            # Skip removing keys from hauth dict as it contains dynamic per-host entries
            current_path = f"{path}/{key}" if path else key
            if current_path.endswith('/hauth'):
                continue
            if remove_obsolete_keys(value, defaults[key], current_path):
                removed = True
    return removed

def migrate_shop_settings(settings):
    """Migrate old shop settings format to new client-based structure."""
    migrated = False
    shop = settings.get('shop', {})
    
    # Check if we have old format (client settings at shop level)
    old_client_keys = ['encrypt', 'hauth', 'clientCertKey', 'clientCertPub']
    has_old_format = any(key in shop for key in old_client_keys)
    has_new_format = 'clients' in shop and 'tinfoil' in shop.get('clients', {})
    
    if has_old_format and not has_new_format:
        logger.info('Migrating shop settings to new client-based format...')
        # Ensure clients structure exists
        if 'clients' not in shop:
            shop['clients'] = {}
        if 'tinfoil' not in shop['clients']:
            shop['clients']['tinfoil'] = {}
        
        # Migrate client-specific settings to tinfoil client
        for key in old_client_keys:
            if key in shop:
                shop['clients']['tinfoil'][key] = shop[key]
                del shop[key]
        
        migrated = True
        logger.info('Shop settings migration completed.')
    
    # Migrate hauth from string to dict format (per-host)
    if 'clients' in shop and 'tinfoil' in shop['clients']:
        tinfoil = shop['clients']['tinfoil']
        hauth = tinfoil.get('hauth')
        
        # If hauth is a non-empty string, convert it to dict format with current host as key
        if isinstance(hauth, str) and hauth:
            logger.info('Migrating Tinfoil hauth from string to per-host dict format...')
            current_host = shop.get('host', '')
            if current_host:
                # Store the old hauth value with the current host as key
                tinfoil['hauth'] = {current_host: hauth}
            else:
                # No host configured, reset to empty dict
                tinfoil['hauth'] = {}
            migrated = True
            logger.info('Hauth migration completed.')
        elif hauth == '':
            # Empty string, convert to empty dict
            tinfoil['hauth'] = {}
            migrated = True
    return migrated

def load_settings():
    settings_updated = False
    with settings_lock:
        if os.path.exists(CONFIG_FILE):
            logger.debug('Reading configuration file.')
            with open(CONFIG_FILE, 'r') as yaml_file:
                settings = yaml.safe_load(yaml_file)

            # Migrate old shop settings format
            if migrate_shop_settings(settings):
                settings_updated = True

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
        
        # Prime Keys.keys_loaded for this process (used by identification code)
        load_keys()
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
    # Clean host URL if present
    if 'host' in data and '://' in data['host']:
        data['host'] = data['host'].split('://')[-1]
    # Update shop-level settings
    for key in ['host', 'motd', 'public']:
        if key in data:
            settings['shop'][key] = data[key]
    # Update client-specific settings
    if 'clients' in data:
        for client_name, client_data in data['clients'].items():
            settings['shop']['clients'][client_name].update(client_data)

    with settings_lock:
        with open(CONFIG_FILE, 'w') as yaml_file:
            yaml.dump(settings, yaml_file)

def set_scheduler_settings(data):
    settings = load_settings()
    settings['scheduler'].update(data)
    with settings_lock:
        with open(CONFIG_FILE, 'w') as yaml_file:
            yaml.dump(settings, yaml_file)

def set_worker_settings(data):
    settings = load_settings()
    settings['worker'].update(data)
    with settings_lock:
        with open(CONFIG_FILE, 'w') as yaml_file:
            yaml.dump(settings, yaml_file)
