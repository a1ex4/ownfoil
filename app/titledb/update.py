"""Drive a titledb refresh: download the upstream JSON, rebuild titles.db from it."""
import logging
import os

from constants import TITLEDB_DIR
from titledb import source, store

logger = logging.getLogger('main')


def update_titledb(app_settings):
    """Download titledb JSON updates and (re)build titles.db if anything changed."""
    logger.info('Updating titledb...')
    if not os.path.isdir(TITLEDB_DIR):
        os.makedirs(TITLEDB_DIR, exist_ok=True)

    downloaded = source.update_titledb_files(app_settings)
    current_locale = f"{app_settings['titles']['region']}.{app_settings['titles']['language']}"
    imported_locale = store.get_imported_locale()
    if downloaded or imported_locale != current_locale:
        locale_changed = imported_locale != current_locale
        store.import_from_json(app_settings)
        if locale_changed:
            from db import reset_files_organized
            reset_files_organized()
    logger.info('titledb update done.')
