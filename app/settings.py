from constants import *
from utils import *
import yaml
import os, sys, tempfile
import threading
import hashlib
from contextlib import contextmanager

from nsz.nut import Keys

import logging

# Reentrant: load_settings holds this lock and calls _dump_settings, which re-acquires it.
settings_lock = threading.RLock()
keys_lock = threading.Lock()


def _dump_settings(settings):
    """Persist settings atomically so a concurrent reader never sees a truncated file."""
    with settings_lock:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(CONFIG_FILE), prefix='.settings-', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w') as yaml_file:
                yaml.dump(settings, yaml_file)
                yaml_file.flush()
                os.fsync(yaml_file.fileno())
            os.replace(tmp, CONFIG_FILE)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise


@contextmanager
def settings_transaction():
    """Hold settings_lock across a read-modify-write so two concurrent saves don't collide."""
    with settings_lock:
        settings = load_settings()
        yield settings
        _dump_settings(settings)

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

def normalize_library_paths(settings):
    """Flatten library.paths from legacy [{path, watcher}] objects back to a plain string list
    (per-path watcher config is dropped; the watcher is now global). Returns True if changed."""
    library = settings.setdefault('library', {})
    changed = False
    normalized = []
    for entry in library.get('paths') or []:
        if isinstance(entry, str):
            path = entry
        else:
            path = entry.get('path')
            changed = True
        if path:
            normalized.append(path)
    library['paths'] = normalized
    return changed

def load_settings():
    settings_updated = False
    with settings_lock:
        if os.path.exists(CONFIG_FILE):
            logger.debug('Reading configuration file.')
            with open(CONFIG_FILE, 'r') as yaml_file:
                settings = yaml.safe_load(yaml_file)
            if settings is None:
                settings = {}
                settings_updated = True

            # Migrate old shop settings format
            if migrate_shop_settings(settings):
                settings_updated = True

            # Flatten legacy per-path watcher objects back to plain path strings
            if normalize_library_paths(settings):
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
            _dump_settings(settings)

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

def get_library_paths():
    """Return the configured library paths as a plain list of path strings."""
    return list(get_settings()['library']['paths'])

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

    with settings_lock:
        settings = load_settings()
        library_paths = settings['library']['paths']
        if path in library_paths:
            success = False
            errors.append({
                'path': 'library/paths',
                'error': f"Path {path} already configured."
            })
            return success, errors
        library_paths.append(path)
        _dump_settings(settings)
    return success, errors

def set_library_management_settings(data):
    with settings_transaction() as settings:
        settings['library']['management'].update(data)

def get_watcher_config():
    """Return the global file watcher config, merged over defaults."""
    return {**DEFAULT_WATCHER, **(get_settings()['library'].get('watcher') or {})}

def set_watcher_settings(data):
    """Validate and persist the global file watcher config."""
    with settings_lock:
        settings = load_settings()
        config = {**DEFAULT_WATCHER, **(settings['library'].get('watcher') or {})}
        if 'enabled' in data:
            config['enabled'] = bool(data['enabled'])
        if 'polling_interval' in data:
            try:
                interval = int(data['polling_interval'])
            except (TypeError, ValueError):
                return False, [{'path': 'library/watcher', 'error': 'Polling interval must be an integer.'}]
            if interval < 1:
                return False, [{'path': 'library/watcher', 'error': 'Polling interval must be at least 1 second.'}]
            config['polling_interval'] = interval
        settings['library']['watcher'] = config
        _dump_settings(settings)
    return True, []

def delete_library_path_from_settings(path):
    success = True
    errors = []
    with settings_lock:
        settings = load_settings()
        library_paths = settings['library']['paths']
        if path in library_paths:
            library_paths.remove(path)
            _dump_settings(settings)
        else:
            success = False
            errors.append({
                    'path': 'library/paths',
                    'error': f"Path {path} not configured."
                })
    return success, errors

def set_titles_settings(region, language):
    with settings_transaction() as settings:
        settings['titles']['region'] = region
        settings['titles']['language'] = language

def set_shop_settings(data):
    with settings_transaction() as settings:
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

def set_scheduler_settings(data):
    with settings_transaction() as settings:
        settings['scheduler'].update(data)

def set_worker_settings(data):
    with settings_transaction() as settings:
        settings['worker'].update(data)
