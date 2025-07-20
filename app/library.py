import hashlib
from constants import *
from db import *
from titles import *

def identify_files_and_add_to_db(library_path, files):
    nb_to_identify = len(files)
    for n, filepath in enumerate(files):
        file = filepath.replace(library_path, "")
        logger.info(f'Identifying file ({n+1}/{nb_to_identify}): {file}')

        file_info = identify_file(filepath)

        if file_info is None:
            logger.error(f'Failed to identify: {file} - file will be skipped.')
            # in the future save identification error to be displayed and inspected in the UI
            continue

        logger.info(f'Identifying file ({n+1}/{nb_to_identify}): {file} OK Title ID: {file_info["title_id"]} App ID : {file_info["app_id"]} Title Type: {file_info["type"]} Version: {file_info["version"]}')
        add_to_titles_db(library_path, file_info)


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
        identify_files_and_add_to_db(library_path, files_to_identify)
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

def compute_library_hash(library_path):
    """
    Computes a hash of all file paths + last modified times in the library.
    """
    hash_md5 = hashlib.md5()
    for root, _, files in os.walk(library_path):
        for f in sorted(files):
            full_path = os.path.join(root, f)
            try:
                stat = os.stat(full_path)
                hash_md5.update(full_path.encode())
                hash_md5.update(str(stat.st_mtime).encode())
            except FileNotFoundError:
                continue  # Skip files that disappeared mid-scan
    return hash_md5.hexdigest()

def is_library_unchanged(library_paths):
    hash_path = Path("config/library_hash.txt")
    if not hash_path.exists():
        return False

    with hash_path.open() as f:
        saved_hash = f.read().strip()

    combined_hash = ''.join(compute_library_hash(lp) for lp in library_paths)
    combined_hash = hashlib.sha256(combined_hash.encode()).hexdigest()

    return saved_hash == combined_hash

def save_library_to_disk(titles_library, library_paths):
    cache_path = Path("config/library_cache.json")
    hash_path = Path("config/library_hash.txt")
    with cache_path.open("w", encoding="utf-8") as f:
        json.dump(titles_library, f, ensure_ascii=False, indent=2)

    # Combine hashes of all paths into one hash string
    combined_hash = ''.join(compute_library_hash(lp) for lp in library_paths)
    combined_hash = hashlib.sha256(combined_hash.encode()).hexdigest()

    with hash_path.open("w") as f:
        f.write(combined_hash)

def load_library_from_disk():
    cache_path = Path("config/library_cache.json")

    # If cache exists, load and return it
    if cache_path.exists():
        with cache_path.open("r", encoding="utf-8") as f:
            return json.load(f)

def generate_library(app_settings):
    library_paths = app_settings['library']['paths']
    if is_library_unchanged(library_paths):
        saved_library = load_library_from_disk()
        if saved_library:
            logger.info("Library hasn't changed since last generate. Returning saved library.")
            return saved_library
    
    logger.info(f'Generating library ...')
    titles = get_all_titles_from_db()
    games_info = []
    for title in titles:
        has_none_value = any(value is None for value in title.values())
        if has_none_value:
            logger.warning(f'File contains None value, it will be skipped: {title}')
            continue
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

    save_library_to_disk(titles_library, library_paths)

    logger.info(f'Generating library done.')

    return titles_library
