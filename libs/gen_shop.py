# Usage:
# python gen_shop.py <directory to scan>
# Generate a 'shop.tfl' Tinfoil index file
# as well as 'shop.json', same content but viewable in the browser

import os, json, sys, time
from consts import *
from jsonc_parser.parser import JsoncParser
import logging

logging.basicConfig(format='%(asctime)s | %(levelname)s: %(message)s', level=logging.DEBUG)

path = sys.argv[1]

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
            if fullPath.split('.')[-1] in valid_ext:
                allFiles.append(fullPath)
    return allDirs, allFiles

while True:
    logging.info(f'Start scanning directory "{path}"')

    dirs = []
    games = []

    shop = default_shop
    template_file = os.path.join(path, template_name)

    if not os.path.isfile(template_file):
        logging.warning(f'Template file {template_file} not found, will use default shop template')
    else:
        try:
            shop = JsoncParser.parse_file(template_file)
        except Exception as e:
            logging.warning(f'Error parsing template file {template_file}, will use default shop template, error was:\n{e}')

    dirs, files = getDirsAndFiles(path)
    rel_dirs = [os.path.join('..', os.path.relpath(s, path)) for s in dirs]
    rel_files = [os.path.join('..', os.path.relpath(s, path)) for s in files]

    logging.info(f'Found {len(dirs)} directories, {len(files)} game files')

    for game, rel_path in zip(files, rel_files):
        size = round(os.path.getsize(game))
        games.append(
            {
                'url': rel_path,
                'size': size
            })

    shop['directories'] = rel_dirs
    shop['files'] = games

    for a in ['json', 'tfl']:
        out_file = os.path.join(path, f'shop.{a}')
        try:
            with open(out_file, 'w') as f:
                json.dump(shop, f, indent=4)
            logging.info(f'Successfully wrote {out_file}')

        except Exception as e:
            logging.error(f'Failed to write {out_file}, error was:\n{e}')

    time.sleep(scan_interval * 60)