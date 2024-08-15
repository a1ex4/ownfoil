from constants import *
from db import *
from titles import *


def scan_library_path(app_settings, library_path):
    try:
        logger.info(f'Scanning library path {library_path} ...')
        if not os.path.isdir(library_path):
            logger.warning(f'Library path {library_path} does not exists.')
            return
        _, files = getDirsAndFiles(library_path)

        if app_settings['titles']['valid_keys']:
            current_identification = 'cnmt'
        else:
            logger.warning('Invalid or non existing keys.txt, title identification fallback to filename only.')
            current_identification = 'filename'

        all_files_with_current_identification = get_all_files_with_identification(current_identification)
        files_to_identify = [f for f in files if f not in all_files_with_current_identification]
        nb_to_identify = len(files_to_identify)
        for n, filepath in enumerate(files_to_identify):
            file = filepath.replace(library_path, "")
            logger.info(f'Identifiying file ({n+1}/{nb_to_identify}): {file}')

            file_info = identify_file(filepath)

            if file_info is None:
                logger.error(f'Failed to identify: {file} - file will be skipped.')
                continue
            logger.info(f'Identifiying file ({n+1}/{nb_to_identify}): {file} OK Title ID: {file_info["title_id"]} App ID : {file_info["app_id"]} Title Type: {file_info["type"]} Version: {file_info["version"]}')
            add_to_titles_db(library_path, file_info)
    finally:
        pass


def get_library_status(title_id):
    has_base = False
    has_latest_version = False

    title_files = get_all_title_files(title_id)
    if len(list(filter(lambda x: x.get('type') == APP_TYPE_BASE, title_files))):
        has_base = True

    available_versions = get_all_existing_versions(title_id)
    if available_versions is None:
        return {
            'has_base': has_base,
            'has_latest_version': True,
            'version': []
        }
    game_latest_version = get_game_latest_version(available_versions)
    for version in available_versions:
        if len(list(filter(lambda x: x.get('type') == APP_TYPE_UPD and str(x.get('version')) == str(version['version']), title_files))):
            version['owned'] = True
            if str(version['version'])  == str(game_latest_version):
                has_latest_version = True
        else:
            version['owned'] = False

    all_existing_dlcs = get_all_existing_dlc(title_id)
    owned_dlcs = [t['app_id'] for t in title_files if t['type'] == APP_TYPE_DLC]
    has_all_dlcs = all(dlc in owned_dlcs for dlc in all_existing_dlcs)

    library_status = {
        'has_base': has_base,
        'has_latest_version': has_latest_version,
        'version': available_versions,
        'has_all_dlcs': has_all_dlcs
    }
    return library_status


def generate_library():
    logger.info(f'Generating library ...')
    titles = get_all_titles_from_db()
    games_info = []
    for title in titles:
        if title['type'] == APP_TYPE_UPD:
            continue
        info_from_titledb = get_game_info(title['app_id'])
        if info_from_titledb is None:
            logger.warning(f'Info not found for game: {title}')
            continue
        title.update(info_from_titledb)
        if title['type'] == APP_TYPE_BASE:
            library_status = get_library_status(title['app_id'])
            title.update(library_status)
            title['title_id_name'] = title['name']
        if title['type'] == APP_TYPE_DLC:
            dlc_has_latest_version = None
            all_dlc_existing_versions = get_all_dlc_existing_versions(title['app_id'])

            if all_dlc_existing_versions is not None and len(all_dlc_existing_versions):
                if title['version'] == all_dlc_existing_versions[-1]:
                    dlc_has_latest_version = True
                else:
                    dlc_has_latest_version = False

            else:
                app_id_version_from_versions_txt = get_app_id_version_from_versions_txt(title['app_id'])
                if app_id_version_from_versions_txt is not None:
                    if int(title['version']) == int(app_id_version_from_versions_txt):
                        dlc_has_latest_version = True
                    else:
                        dlc_has_latest_version = False


            if dlc_has_latest_version is not None:
                title['has_latest_version'] = dlc_has_latest_version

            titleid_info = get_game_info(title['title_id'])
            title['title_id_name'] = titleid_info['name']
        games_info.append(title)
    titles_library = sorted(games_info, key=lambda x: (
        "title_id_name" not in x, 
        x.get("title_id_name", "Unrecognized") or "Unrecognized", 
        x.get('app_id', "") or ""
    ))
    logger.info(f'Generating library done.')

    return titles_library