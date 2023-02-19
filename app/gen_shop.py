import os, json, sys, time
from utils import *
import logging
logger = logging.getLogger(__name__)

def getDirsAndFiles(path):
    entries = os.listdir(path)
    allFiles = list()
    allDirs = list()

    for entry in entries:
        fullPath = os.path.join(path, entry)
        if os.path.isdir(fullPath):
            allDirs.append(fullPath)
            dirs, files = getDirsAndFiles(fullPath)
            allDirs += dirs
            allFiles += files
        else:
            if fullPath.split('.')[-1] in config["shop"]["valid_ext"]:
                allFiles.append(fullPath)
    return allDirs, allFiles

def get_shop_dirs_and_file(path):
    dirs = []
    games = []

    dirs, files = getDirsAndFiles(path)
    rel_dirs = [os.path.join('..', os.path.relpath(s, path)) for s in dirs]
    rel_files = [os.path.join('..', os.path.relpath(s, path)) for s in files]

    logger.info(f'Found {len(dirs)} directories, {len(files)} game/save files')

    for game, rel_path in zip(files, rel_files):
        size = round(os.path.getsize(game))
        games.append(
            {
                'url': rel_path,
                'size': size
            })
    return rel_dirs, games

def init_shop(path):
    return read_config(path + "/shop_template.toml")

def gen_shop(path):

    shop = init_shop(path)

    shop_dir, shop_file = get_shop_dirs_and_file(path)
    shop['directories'] = shop_dir
    shop['files'] = shop_file

    for a in ['json', 'tfl']:
        out_file = os.path.join(path, f'shop.{a}')
        try:
            with open(out_file, 'w') as f:
                json.dump(shop, f, indent=4)
            logger.info(f'Successfully wrote {out_file}')

        except Exception as e:
            logger.error(f'Failed to write {out_file}, error was:\n{e}')
