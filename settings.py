from constants import *
import yaml
import os



CONFIG_FILE = os.path.join(CONFIG_DIR, 'settings.yaml')

def load_settings():
    if os.path.exists(CONFIG_FILE):
        print('reading conf file')
        with open(CONFIG_FILE, 'r') as yaml_file:
            settings = yaml.safe_load(yaml_file)

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