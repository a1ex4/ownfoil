# Usage:
# python app.py <configuration file>
# Generate a 'shop.tfl' Tinfoil index file
# as well as 'shop.json', same content but viewable in the browser

import os, sys

from utils import *
from gen_shop import *

import logging
logger = logging.getLogger("main")

# Set environment variables to override properties from configuration file
CONFIG_KEYS = {
    "ROOT_DIR": "root_dir",
    "SCAN_INTERVAL": "shop.scan_interval",
    "SHOP_TEMPLATE": "shop.template"
}


config_path = sys.argv[1]

config = read_config(config_path)
update_conf_from_env(CONFIG_KEYS, config)

# main loop
root_dir = config["root_dir"]
scan_interval = int(config["shop"]["scan_interval"])
valid_ext = config["shop"]["valid_ext"]

while True:
    logger.info(f'Start scanning directory "{root_dir}"')
    gen_shop(root_dir, valid_ext)
    time.sleep(scan_interval * 60)

