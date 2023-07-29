import os
from pathlib import Path
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
import logging

logger = logging
logger.basicConfig(format='%(asctime)s - %(levelname)s %(name)s: %(message)s', level=logging.INFO)

# Set environment variables to override properties from configuration file
CONFIG_KEYS = {
    "SCAN_INTERVAL": "shop.scan_interval",
    "SHOP_TEMPLATE": "shop.template",
    "SAVE_ENABLED": "saves.enabled",
    "SAVE_INTERVAL": "saves.interval"
}

def toml_path_to_dict_access(key):
    return '["' +'"]["'.join(key.split('.')) + '"]'

def update_conf_from_env(CONFIG_KEYS, config):
    for env, toml_path in CONFIG_KEYS.items():
        if env in os.environ:
            dict_access = toml_path_to_dict_access(toml_path)
            exec(f'config{dict_access}=os.environ[env]')

def read_config(toml_file):
    with open(toml_file, mode="rb") as fp:
        config = tomllib.load(fp)
    return config

config_path = os.environ["OWNFOIL_CONFIG"] 
config = read_config(config_path)
update_conf_from_env(CONFIG_KEYS, config)
