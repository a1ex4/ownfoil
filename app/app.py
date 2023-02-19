# Usage:
# python app.py <configuration file>
# Generate a 'shop.tfl' Tinfoil index file
# as well as 'shop.json', same content but viewable in the browser

import os, sys
from apscheduler.schedulers.blocking import BlockingScheduler

os.environ["OWNFOIL_CONFIG"] = sys.argv[1]
from utils import *
from gen_shop import *

import logging
logger = logging.getLogger("main")

if __name__ == "__main__":
    scheduler = BlockingScheduler()
    # Get config
    root_dir = config["root_dir"]
    scan_interval = int(config["shop"]["scan_interval"])
    valid_ext = config["shop"]["valid_ext"]
    # Add gen_shop scheduled job
    job = scheduler.add_job(gen_shop, 'interval', args=(root_dir, valid_ext), minutes=1, id='gen_shop', name='Generate shop')
    scheduler.start()

