# Usage:
# python app.py <configuration file>
# Generate a 'shop.tfl' Tinfoil index file
# as well as 'shop.json', same content but viewable in the browser

import os, sys
from apscheduler.schedulers.blocking import BlockingScheduler
from datetime import datetime
import warnings
warnings.filterwarnings("ignore")

os.environ["OWNFOIL_CONFIG"] = sys.argv[1]
from utils import *
from gen_shop import *
from backup_saves import *

import logging
logger = logging.getLogger("main")
# logging.getLogger('apscheduler.executors.default').setLevel(logging.WARNING)

if __name__ == "__main__":
    scheduler = BlockingScheduler()
    # Get config
    root_dir = config["root_dir"]
    scan_interval = int(config["shop"]["scan_interval"])

    # Add scheduled jobs
    job_gen_shop = scheduler.add_job(gen_shop, 'interval', args=[root_dir], minutes=scan_interval, id='gen_shop', name='Generate shop', next_run_time=datetime.now())
    try:
        save_interval = int(config["saves"]["interval"])
        if config['saves']['enabled']:
            job_backup_saves = scheduler.add_job(backup_saves, 'interval', minutes=save_interval, id='backup_saves', name='Backup saves', next_run_time=datetime.now())
    except KeyError:
        logger.error('Error getting Saves manager configuration, check configuration file.')
    scheduler.start()

