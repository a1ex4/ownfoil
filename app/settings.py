from constants import *
import yaml
import os, sys

sys.path.append(APP_DIR + '/NSTools/py')
from nstools.nut import Keys


def validate_keys(key_file=KEYS_FILE):
    valid = False
    try:
        if os.path.isfile(key_file):
            valid = Keys.load(key_file)
            return valid
        else:
            print(f'Keys file {key_file} does not exist.')

    except:
        print(f'Provided keys file {key_file} is invalid.')
    return valid

def load_settings():
    if os.path.exists(CONFIG_FILE):
        print('reading conf file')
        with open(CONFIG_FILE, 'r') as yaml_file:
            settings = yaml.safe_load(yaml_file)

        valid_keys = validate_keys()
        settings['valid_keys'] = valid_keys

    else:
        settings = DEFAULT_SETTINGS
        with open(CONFIG_FILE, 'w') as yaml_file:
            yaml.dump(settings, yaml_file)
    return settings

def verify_settings(section, data):
    success = True
    errors = []
    if section == 'library':
        # Check that path exists
        if not os.path.exists(data['path']):
            success = False
            errors.append({
                'path': 'library/path',
                'error': f"Path {data['path']} does not exists."
            })
    return success, errors

def set_settings(section, data):
    settings = load_settings()
    settings[section] = data

    with open(CONFIG_FILE, 'w') as yaml_file:
        yaml.dump(settings, yaml_file)
    app_settings = settings