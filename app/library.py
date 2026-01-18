import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from constants import *
from db import *
import titles as titles_lib
import datetime
from pathlib import Path
from utils import *

def add_library_complete(app, watcher, path):
    """Add a library to settings, database, and watchdog"""
    from settings import add_library_path_to_settings
    
    with app.app_context():
        # Add to settings
        success, errors = add_library_path_to_settings(path)
        if not success:
            return success, errors
        
        # Add to database
        add_library(path)
        
        # Add to watchdog
        watcher.add_directory(path)
        
        logger.info(f"Successfully added library: {path}")
        return True, []

def remove_library_complete(app, watcher, path):
    """Remove a library from settings, database, and watchdog with proper cleanup"""
    from settings import delete_library_path_from_settings
    
    with app.app_context():
        # Remove from watchdog first
        watcher.remove_directory(path)
        
        # Get library object before deletion
        library = Libraries.query.filter_by(path=path).first()
        if library:
            # Get all file IDs from this library before deletion
            file_ids = [f.id for f in library.files]
            
            # Update Apps table to remove file references and update ownership
            total_apps_updated = 0
            for file_id in file_ids:
                apps_updated = remove_file_from_apps(file_id)
                total_apps_updated += apps_updated
            
            # Remove titles that no longer have any owned apps
            titles_removed = remove_titles_without_owned_apps()
            
            # Delete library (cascade will delete files automatically)
            db.session.delete(library)
            db.session.commit()
            
            logger.info(f"Removed library: {path}")
            if total_apps_updated > 0:
                logger.info(f"Updated {total_apps_updated} app entries to remove library file references.")
            if titles_removed > 0:
                logger.info(f"Removed {titles_removed} titles with no owned apps.")
        
        # Remove from settings
        success, errors = delete_library_path_from_settings(path)
        return success, errors

def init_libraries(app, watcher, paths):
    with app.app_context():
        # delete non existing libraries
        for library in get_libraries():
            path = library.path
            if not os.path.exists(path):
                logger.warning(f"Library {path} no longer exists, deleting from database.")
                # Use the complete removal function for consistency
                remove_library_complete(app, watcher, path)

        # add libraries and start watchdog
        for path in paths:
            # Check if library already exists in database
            existing_library = Libraries.query.filter_by(path=path).first()
            if not existing_library:
                # add library paths to watchdog if necessary
                watcher.add_directory(path)
                add_library(path)
            else:
                # Ensure watchdog is monitoring existing library
                watcher.add_directory(path)

def add_files_to_library(library, files):
    nb_to_identify = len(files)
    if isinstance(library, int) or library.isdigit():
        library_id = library
        library_path = get_library_path(library_id)
    else:
        library_path = library
        library_id = get_library_id(library_path)

    library_path = get_library_path(library_id)
    for n, filepath in enumerate(files):
        if file_exists_in_db(filepath):
            logger.debug(f'File already in database, skipping: {filepath}')
            continue
        file = filepath.replace(library_path, "")
        logger.info(f'Getting file info ({n+1}/{nb_to_identify}): {file}')

        file_info = titles_lib.get_file_info(filepath)

        if file_info is None:
            logger.error(f'Failed to get info for file: {file} - file will be skipped.')
            # in the future save identification error to be displayed and inspected in the UI
            continue

        new_file = Files(
            filepath = filepath,
            library_id = library_id,
            folder = file_info["filedir"],
            filename = file_info["filename"],
            extension = file_info["extension"],
            size = file_info["size"],
        )
        db.session.add(new_file)

        # Commit every 100 files to avoid excessive memory use
        if (n + 1) % 100 == 0:
            db.session.commit()

    # Final commit
    db.session.commit()

def scan_library_path(library_path):
    library_id = get_library_id(library_path)
    logger.info(f'Scanning library path {library_path} ...')
    if not os.path.isdir(library_path):
        logger.warning(f'Library path {library_path} does not exists.')
        return
    _, files = titles_lib.getDirsAndFiles(library_path)

    filepaths_in_library = get_library_file_paths(library_id)
    new_files = [f for f in files if f not in filepaths_in_library]
    add_files_to_library(library_id, new_files)
    set_library_scan_time(library_id)

def get_files_to_identify(library_id):
    non_identified_files = get_all_non_identified_files_from_library(library_id)
    if titles_lib.Keys.keys_loaded:
        files_to_identify_with_cnmt = get_files_with_identification_from_library(library_id, 'filename')
        non_identified_files = list(set(non_identified_files).union(files_to_identify_with_cnmt))
    return non_identified_files

def identify_library_files(library):
    if isinstance(library, int) or library.isdigit():
        library_id = library
        library_path = get_library_path(library_id)
    else:
        library_path = library
        library_id = get_library_id(library_path)
    files_to_identify = get_files_to_identify(library_id)
    nb_to_identify = len(files_to_identify)
    for n, file in enumerate(files_to_identify):
        try:
            file_id = file.id
            filepath = file.filepath
            filename = file.filename

            if not os.path.exists(filepath):
                logger.warning(f'Identifying file ({n+1}/{nb_to_identify}): {filename} no longer exists, deleting from database.')
                Files.query.filter_by(id=file_id).delete(synchronize_session=False)
                continue

            logger.info(f'Identifying file ({n+1}/{nb_to_identify}): {filename}')
            identification, success, file_contents, error = titles_lib.identify_file(filepath)
            if success and file_contents and not error:
                # find all unique Titles ID to add to the Titles db
                title_ids = list(dict.fromkeys([c['title_id'] for c in file_contents]))

                for title_id in title_ids:
                    add_title_id_in_db(title_id)

                nb_content = 0
                for file_content in file_contents:
                    logger.info(f'Identifying file ({n+1}/{nb_to_identify}) - Found content Title ID: {file_content["title_id"]} App ID : {file_content["app_id"]} Title Type: {file_content["type"]} Version: {file_content["version"]}')
                    # now add the content to Apps
                    title_id_in_db = get_title_id_db_id(file_content["title_id"])
                    
                    # Check if app already exists
                    existing_app = get_app_by_id_and_version(
                        file_content["app_id"],
                        file_content["version"]
                    )
                    
                    if existing_app:
                        # Add file to existing app using many-to-many relationship
                        add_file_to_app(file_content["app_id"], file_content["version"], file_id)
                    else:
                        # Create new app entry and add file using many-to-many relationship
                        new_app = Apps(
                            app_id=file_content["app_id"],
                            app_version=file_content["version"],
                            app_type=file_content["type"],
                            owned=True,
                            title_id=title_id_in_db
                        )
                        db.session.add(new_app)
                        db.session.flush()  # Flush to get the app ID
                        
                        # Add the file to the new app
                        file_obj = get_file_from_db(file_id)
                        if file_obj:
                            new_app.files.append(file_obj)
                    
                    nb_content += 1

                if nb_content > 1:
                    file.multicontent = True
                file.nb_content = nb_content
                file.identified = True
            else:
                logger.warning(f"Error identifying file {filename}: {error}")
                file.identification_error = error
                file.identified = False

            file.identification_type = identification

        except Exception as e:
            logger.warning(f"Error identifying file {filename}: {e}")
            file.identification_error = str(e)
            file.identified = False

        # and finally update the File with identification info
        file.identification_attempts += 1
        file.last_attempt = datetime.datetime.now()

        # Commit every 100 files to avoid excessive memory use
        if (n + 1) % 100 == 0:
            db.session.commit()

    # Final commit
    db.session.commit()

def add_missing_apps_to_db():
    logger.info('Adding missing apps to database...')
    titles = get_all_titles()
    apps_added = 0
    
    for n, title in enumerate(titles):
        title_id = title.title_id
        title_db_id = get_title_id_db_id(title_id)
        
        # Add base game if not present
        existing_base = get_app_by_id_and_version(title_id, "0")
        
        if not existing_base:
            new_base_app = Apps(
                app_id=title_id,
                app_version="0",
                app_type=APP_TYPE_BASE,
                owned=False,
                title_id=title_db_id
            )
            db.session.add(new_base_app)
            apps_added += 1
            logger.debug(f'Added missing base app: {title_id}')
        
        # Add missing update versions
        title_versions = titles_lib.get_all_existing_versions(title_id)
        for version_info in title_versions:
            version = str(version_info['version'])
            update_app_id = title_id[:-3] + '800'  # Convert base ID to update ID
            
            existing_update = get_app_by_id_and_version(update_app_id, version)
            
            if not existing_update:
                new_update_app = Apps(
                    app_id=update_app_id,
                    app_version=version,
                    app_type=APP_TYPE_UPD,
                    owned=False,
                    title_id=title_db_id
                )
                db.session.add(new_update_app)
                apps_added += 1
                logger.debug(f'Added missing update app: {update_app_id} v{version}')
        
        # Add missing DLC
        title_dlc_ids = titles_lib.get_all_existing_dlc(title_id)
        for dlc_app_id in title_dlc_ids:
            dlc_versions = titles_lib.get_all_app_existing_versions(dlc_app_id)
            if dlc_versions:
                for dlc_version in dlc_versions:
                    existing_dlc = get_app_by_id_and_version(dlc_app_id, str(dlc_version))
                    
                    if not existing_dlc:
                        new_dlc_app = Apps(
                            app_id=dlc_app_id,
                            app_version=str(dlc_version),
                            app_type=APP_TYPE_DLC,
                            owned=False,
                            title_id=title_db_id
                        )
                        db.session.add(new_dlc_app)
                        apps_added += 1
                        logger.debug(f'Added missing DLC app: {dlc_app_id} v{dlc_version}')
        
        # Commit every 100 titles to avoid excessive memory use
        if (n + 1) % 100 == 0:
            db.session.commit()
            logger.info(f'Processed {n + 1}/{len(titles)} titles, added {apps_added} missing apps so far')
    
    # Final commit
    db.session.commit()
    logger.info(f'Finished adding missing apps to database. Total apps added: {apps_added}')

def process_library_identification(app):
    logger.info(f"Starting library identification process for all libraries...")
    try:
        with app.app_context():
            libraries = get_libraries()
            for library in libraries:
                identify_library_files(library.path)

    except Exception as e:
        logger.error(f"Error during library identification process: {e}")
    logger.info(f"Library identification process for all libraries completed.")

def update_titles():
    # Remove titles that no longer have any owned apps
    titles_removed = remove_titles_without_owned_apps()
    if titles_removed > 0:
            logger.info(f"Removed {titles_removed} titles with no owned apps.")

    titles = get_all_titles()
    for n, title in enumerate(titles):
        have_base = False
        up_to_date = False
        complete = False

        title_id = title.title_id
        title_apps = get_all_title_apps(title_id)

        # check have_base - look for owned base apps
        owned_base_apps = [app for app in title_apps if app.get('app_type') == APP_TYPE_BASE and app.get('owned')]
        have_base = len(owned_base_apps) > 0

        # check up_to_date - find highest owned update version
        owned_update_apps = [app for app in title_apps if app.get('app_type') == APP_TYPE_UPD and app.get('owned')]
        available_update_apps = [app for app in title_apps if app.get('app_type') == APP_TYPE_UPD]
        
        if not available_update_apps:
            # No updates available, consider up to date
            up_to_date = True
        elif not owned_update_apps:
            # Updates available but none owned
            up_to_date = False
        else:
            # Find highest available version and highest owned version
            highest_available_version = max(int(app['app_version']) for app in available_update_apps)
            highest_owned_version = max(int(app['app_version']) for app in owned_update_apps)
            up_to_date = highest_owned_version >= highest_available_version

        # check complete - latest version of all available DLC are owned
        available_dlc_apps = [app for app in title_apps if app.get('app_type') == APP_TYPE_DLC]
        
        if not available_dlc_apps:
            # No DLC available, consider complete
            complete = True
        else:
            # Group DLC by app_id and find latest version for each
            dlc_by_id = {}
            for app in available_dlc_apps:
                app_id = app['app_id']
                version = int(app['app_version'])
                if app_id not in dlc_by_id or version > dlc_by_id[app_id]['version']:
                    dlc_by_id[app_id] = {
                        'version': version,
                        'owned': app.get('owned', False)
                    }
            
            # Check if latest version of each DLC is owned
            complete = all(dlc_info['owned'] for dlc_info in dlc_by_id.values())

        title.have_base = have_base
        title.up_to_date = up_to_date
        title.complete = complete

        # Commit every 100 titles to avoid excessive memory use
        if (n + 1) % 100 == 0:
            db.session.commit()

    db.session.commit()

def get_library_status(title_id):
    title = get_title(title_id)
    title_apps = get_all_title_apps(title_id)

    available_versions = titles_lib.get_all_existing_versions(title_id)
    for version in available_versions:
        if len(list(filter(lambda x: x.get('app_type') == APP_TYPE_UPD and str(x.get('app_version')) == str(version['version']), title_apps))):
            version['owned'] = True
        else:
            version['owned'] = False

    library_status = {
        'has_base': title.have_base,
        'has_latest_version': title.up_to_date,
        'version': available_versions,
        'has_all_dlcs': title.complete
    }
    return library_status

def compute_apps_hash():
    """
    Computes a hash of all Apps table content to detect changes in library state.
    """
    hash_md5 = hashlib.md5()
    apps = get_all_apps()
    
    # Sort apps with safe handling of None values
    for app in sorted(apps, key=lambda x: (x['app_id'] or '', x['app_version'] or '')):
        hash_md5.update((app['app_id'] or '').encode())
        hash_md5.update((app['app_version'] or '').encode())
        hash_md5.update((app['app_type'] or '').encode())
        hash_md5.update(str(app['owned'] or False).encode())
        hash_md5.update((app['title_id'] or '').encode())
    return hash_md5.hexdigest()

def is_library_unchanged():
    cache_path = Path(LIBRARY_CACHE_FILE)
    if not cache_path.exists():
        return False

    saved_library = load_library_from_disk()
    if not saved_library:
        return False

    if not saved_library.get('hash'):
        return False

    current_hash = compute_apps_hash()
    return saved_library['hash'] == current_hash

def save_library_to_disk(library_data):
    cache_path = Path(LIBRARY_CACHE_FILE)
    # Ensure cache directory exists
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    safe_write_json(cache_path, library_data)

def load_library_from_disk():
    cache_path = Path(LIBRARY_CACHE_FILE)
    if not cache_path.exists():
        return None

    try:
        with cache_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return None

def generate_library():
    """Generate the game library from Apps table, using cached version if unchanged"""
    if is_library_unchanged():
        saved_library = load_library_from_disk()
        if saved_library:
            return saved_library['library']
    
    logger.info(f'Generating library ...')
    titles_lib.load_titledb()
    titles = get_all_apps()
    games_info = []
    processed_dlc_apps = set()  # Track processed DLC app_ids to avoid duplicates

    for title in titles:
        has_none_value = any(value is None for value in title.values())
        if has_none_value:
            logger.warning(f'File contains None value, it will be skipped: {title}')
            continue
        if title['app_type'] == APP_TYPE_UPD:
            continue
            
        # Get title info from titledb
        info_from_titledb = titles_lib.get_game_info(title['app_id'])
        if info_from_titledb is None:
            logger.warning(f'Info not found for game: {title}')
            continue
        title.update(info_from_titledb)
        
        if title['app_type'] == APP_TYPE_BASE:
            # Get title status from Titles table (already calculated by update_titles)
            title_obj = get_title(title['title_id'])
            if title_obj:
                title['has_base'] = title_obj.have_base
                title['has_latest_version'] = title_obj.up_to_date
                title['has_all_dlcs'] = title_obj.complete
            else:
                title['has_base'] = False
                title['has_latest_version'] = False
                title['has_all_dlcs'] = False
            
            # Get version info from Apps table and add release dates from versions_db
            title_apps = get_all_title_apps(title['title_id'])
            update_apps = [app for app in title_apps if app.get('app_type') == APP_TYPE_UPD]
            
            # Get release date information from external source
            available_versions = titles_lib.get_all_existing_versions(title['title_id'])
            version_release_dates = {v['version']: v['release_date'] for v in available_versions}
            
            version_list = []
            for update_app in update_apps:
                app_version = int(update_app['app_version'])
                version_list.append({
                    'version': app_version,
                    'owned': update_app.get('owned', False),
                    'release_date': version_release_dates.get(app_version, 'Unknown')
                })
            
            title['version'] = sorted(version_list, key=lambda x: x['version'])
            title['title_id_name'] = title['name']
            
        elif title['app_type'] == APP_TYPE_DLC:
            # Skip if we've already processed this DLC app_id
            if title['app_id'] in processed_dlc_apps:
                continue
            processed_dlc_apps.add(title['app_id'])
            
            # Get all versions for this DLC app_id
            title_apps = get_all_title_apps(title['title_id'])
            dlc_apps = [app for app in title_apps if app.get('app_type') == APP_TYPE_DLC and app['app_id'] == title['app_id']]
            
            # Create version list for this DLC
            version_list = []
            for dlc_app in dlc_apps:
                app_version = int(dlc_app['app_version'])
                version_list.append({
                    'version': app_version,
                    'owned': dlc_app.get('owned', False),
                    'release_date': 'Unknown'  # DLC release dates not available in versions_db
                })
            
            title['version'] = sorted(version_list, key=lambda x: x['version'])
            
            # Check if this DLC has latest version
            if dlc_apps:
                highest_version = max(int(app['app_version']) for app in dlc_apps)
                highest_owned_version = max((int(app['app_version']) for app in dlc_apps if app.get('owned')), default=0)
                title['has_latest_version'] = highest_owned_version >= highest_version
            else:
                title['has_latest_version'] = True
            
            # Get title name for DLC
            titleid_info = titles_lib.get_game_info(title['title_id'])
            title['title_id_name'] = titleid_info['name'] if titleid_info else 'Unrecognized'
            
        games_info.append(title)
    
    library_data = {
        'hash': compute_apps_hash(),
        'library': sorted(games_info, key=lambda x: (
            "title_id_name" not in x, 
            x.get("title_id_name", "Unrecognized") or "Unrecognized", 
            x.get('app_id', "") or ""
        ))
    }

    save_library_to_disk(library_data)

    titles_lib.identification_in_progress_count -= 1
    titles_lib.unload_titledb()

    logger.info(f'Generating library done.')

    return library_data['library']

def _sanitize_component(value, fallback='Unknown'):
    value = str(value or '').strip()
    value = re.sub(r'[<>:"/\\\\|?*]', '', value)
    value = value.rstrip('. ')
    return value if value else fallback

def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

def _ensure_unique_path(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    counter = 1
    while True:
        candidate = f"{base} ({counter}){ext}"
        if not os.path.exists(candidate):
            return candidate
        counter += 1

def _get_nsz_keys_file():
    return KEYS_FILE if os.path.exists(KEYS_FILE) else None

def _ensure_nsz_keys():
    key_source = _get_nsz_keys_file()
    if not key_source:
        return False, f"Keys file not found at {KEYS_FILE}."
    dest_dir = os.path.join(os.path.expanduser('~'), '.switch')
    dest_files = [
        os.path.join(dest_dir, 'keys.txt'),
        os.path.join(dest_dir, 'prod.keys')
    ]
    scripts_dir = os.path.join(os.path.dirname(sys.executable), 'Scripts')
    dest_files.append(os.path.join(scripts_dir, 'keys.txt'))
    try:
        os.makedirs(dest_dir, exist_ok=True)
        os.makedirs(scripts_dir, exist_ok=True)
        for dest_file in dest_files:
            if not os.path.exists(dest_file) or os.path.getmtime(key_source) > os.path.getmtime(dest_file):
                shutil.copy2(key_source, dest_file)
        return True, None
    except Exception as e:
        return False, f"Failed to copy keys to {dest_dir}: {e}."

def _quote_arg(value):
    value = str(value)
    if value.startswith('"') and value.endswith('"'):
        return value
    return f"\"{value}\""

def _get_nsz_exe():
    if os.path.exists(NSZ_SCRIPT):
        return f"{_quote_arg(sys.executable)} {_quote_arg(NSZ_SCRIPT)}"
    scripts_dir = os.path.join(os.path.dirname(sys.executable), 'Scripts')
    nsz_exe = os.path.join(scripts_dir, 'nsz.exe')
    return _quote_arg(nsz_exe) if os.path.exists(nsz_exe) else None

def _format_nsz_command(command_template, input_file, output_file, threads=None):
    nsz_exe = _get_nsz_exe() or 'nsz'
    nsz_keys = _get_nsz_keys_file() or KEYS_FILE
    if not command_template:
        command_template = '{nsz_exe} --keys-file "{nsz_keys}" -C -o "{output_dir}" "{input_file}" --verify --low-verbose'
    command = command_template.format(
        nsz_exe=nsz_exe,
        nsz_keys=nsz_keys,
        input_file=input_file,
        output_file=output_file,
        output_dir=os.path.dirname(output_file),
        threads=threads or ''
    )
    if threads and re.search(r'(^|\\s)(-t|--threads)\\s', command) is None:
        command = f"{command} -t {threads}"
    return command

def _run_command(command, log_cb=None, stream_output=False, cancel_cb=None, timeout_seconds=None):
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8'] = '1'
    if not stream_output:
        return subprocess.run(command, shell=True, capture_output=True, text=True, env=env)

    process = subprocess.Popen(
        command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env
    )
    start_time = time.time()
    if process.stdout:
        while True:
            if cancel_cb and cancel_cb():
                process.terminate()
                break
            if timeout_seconds and (time.time() - start_time) > timeout_seconds:
                if log_cb:
                    log_cb(f"Timeout after {timeout_seconds}s, terminating process.")
                process.terminate()
                break
            line = process.stdout.readline()
            if line:
                if log_cb:
                    log_cb(line.rstrip())
            elif process.poll() is not None:
                break
            else:
                time.sleep(0.2)
    returncode = process.wait()
    result = subprocess.CompletedProcess(command, returncode, '', '')
    return result

def _choose_primary_app(apps):
    if not apps:
        return None
    priority = {
        APP_TYPE_BASE: 0,
        APP_TYPE_UPD: 1,
        APP_TYPE_DLC: 2
    }
    return sorted(
        apps,
        key=lambda app: (
            priority.get(app.app_type, 99),
            app.app_id or '',
            _safe_int(app.app_version)
        )
    )[0]

def _compute_relative_folder(library_path, full_path):
    folder = os.path.dirname(full_path)
    if os.path.normpath(library_path) == os.path.normpath(folder):
        return ''
    normalized = folder.replace(library_path, '')
    return normalized if normalized.startswith(os.sep) else os.sep + normalized

def _build_destination(library_path, file_entry, app, title_name, dlc_name):
    title_id = app.title.title_id if app.title else None
    safe_title = _sanitize_component(title_name or title_id or app.app_id)
    safe_title_id = _sanitize_component(title_id or app.app_id)
    version = app.app_version or '0'
    extension = file_entry.extension or os.path.splitext(file_entry.filename or '')[1].lstrip('.')

    if app.app_type == APP_TYPE_BASE:
        subdir = 'Base'
        filename = f"{safe_title} [{safe_title_id}] [BASE][v{version}].{extension}"
    elif app.app_type == APP_TYPE_UPD:
        subdir = os.path.join('Updates', f"v{version}")
        filename = f"{safe_title} [{safe_title_id}] [UPDATE][v{version}].{extension}"
    elif app.app_type == APP_TYPE_DLC:
        dlc_display = _sanitize_component(dlc_name or app.app_id)
        subdir = os.path.join('DLC', f"{dlc_display} [{app.app_id}]")
        filename = f"{safe_title} - {dlc_display} [{app.app_id}] [DLC][v{version}].{extension}"
    else:
        subdir = 'Other'
        filename = file_entry.filename or f"{safe_title} [{safe_title_id}] [UNKNOWN].{extension}"

    folder = os.path.join(library_path, _sanitize_component(f"{safe_title} [{safe_title_id}]"), subdir)
    filename = _sanitize_component(filename)
    return folder, filename

def organize_library(dry_run=False, verbose=False, detail_limit=200):
    results = {
        'success': True,
        'moved': 0,
        'skipped': 0,
        'errors': [],
        'details': []
    }
    detail_count = 0

    def add_detail(message):
        nonlocal detail_count
        if (verbose or dry_run) and detail_count < detail_limit:
            results['details'].append(message)
            detail_count += 1

    titles_lib.load_titledb()
    title_name_cache = {}
    app_name_cache = {}

    files = Files.query.filter_by(identified=True).all()
    for file_entry in files:
        if not file_entry.filepath or not os.path.exists(file_entry.filepath):
            results['skipped'] += 1
            add_detail('Skip missing file path.')
            continue
        library_path = get_library_path(file_entry.library_id)
        if not library_path:
            results['skipped'] += 1
            add_detail(f"Skip missing library for {file_entry.filepath}.")
            continue
        primary_app = _choose_primary_app(list(file_entry.apps))
        if not primary_app:
            results['skipped'] += 1
            add_detail(f"Skip no app mapping for {file_entry.filepath}.")
            continue

        title_id = primary_app.title.title_id if primary_app.title else None
        if title_id not in title_name_cache:
            info = titles_lib.get_game_info(title_id) if title_id else None
            title_name_cache[title_id] = info['name'] if info else title_id or primary_app.app_id

        title_name = title_name_cache.get(title_id)
        dlc_name = None
        if primary_app.app_type == APP_TYPE_DLC:
            if primary_app.app_id not in app_name_cache:
                info = titles_lib.get_game_info(primary_app.app_id)
                app_name_cache[primary_app.app_id] = info['name'] if info else primary_app.app_id
            dlc_name = app_name_cache.get(primary_app.app_id)

        dest_dir, dest_filename = _build_destination(
            library_path,
            file_entry,
            primary_app,
            title_name,
            dlc_name
        )
        dest_path = os.path.join(dest_dir, dest_filename)
        if os.path.normpath(dest_path) == os.path.normpath(file_entry.filepath):
            results['skipped'] += 1
            add_detail(f"Skip already organized: {file_entry.filepath}.")
            continue
        dest_path = _ensure_unique_path(dest_path)

        if not dry_run:
            try:
                os.makedirs(dest_dir, exist_ok=True)
                old_path = file_entry.filepath
                shutil.move(old_path, dest_path)
                update_file_path(library_path, old_path, dest_path)
                results['moved'] += 1
                add_detail(f"Moved: {old_path} -> {dest_path}.")
            except Exception as e:
                logger.error(f"Failed to move {file_entry.filepath}: {e}")
                results['errors'].append(str(e))
                add_detail(f"Error moving {file_entry.filepath}: {e}.")
        else:
            results['moved'] += 1
            add_detail(f"Plan move: {file_entry.filepath} -> {dest_path}.")

    titles_lib.unload_titledb()
    if results['errors']:
        results['success'] = False
    return results

def delete_older_updates(dry_run=False, verbose=False, detail_limit=200):
    results = {
        'success': True,
        'deleted': 0,
        'skipped': 0,
        'errors': [],
        'details': []
    }
    detail_count = 0

    def add_detail(message):
        nonlocal detail_count
        if (verbose or dry_run) and detail_count < detail_limit:
            results['details'].append(message)
            detail_count += 1

    titles = Titles.query.all()
    for title in titles:
        update_apps = Apps.query.filter_by(
            title_id=title.id,
            app_type=APP_TYPE_UPD,
            owned=True
        ).all()
        if len(update_apps) <= 1:
            results['skipped'] += 1
            add_detail(f"Skip updates for {title.title_id}: {len(update_apps)} owned update(s).")
            continue

        latest_app = max(update_apps, key=lambda app: _safe_int(app.app_version))
        for app in update_apps:
            if app.id == latest_app.id:
                continue
            filepaths = [file.filepath for file in list(app.files)]
            if not filepaths:
                results['skipped'] += 1
                add_detail(f"Skip no files for update {app.app_id} v{app.app_version}.")
                continue
            for filepath in filepaths:
                if dry_run:
                    results['deleted'] += 1
                    add_detail(f"Plan delete: {filepath}.")
                    continue
                try:
                    if filepath and os.path.exists(filepath):
                        os.remove(filepath)
                    delete_file_by_filepath(filepath)
                    results['deleted'] += 1
                    add_detail(f"Deleted: {filepath}.")
                except Exception as e:
                    logger.error(f"Failed to delete update {filepath}: {e}")
                    results['errors'].append(str(e))
                    add_detail(f"Error deleting {filepath}: {e}.")

    if results['errors']:
        results['success'] = False
    return results

def convert_to_nsz(command_template, delete_original=True, dry_run=False, verbose=False, detail_limit=200, log_cb=None, progress_cb=None, stream_output=False, threads=None, library_id=None, cancel_cb=None, timeout_seconds=None, min_size_bytes=None):
    results = {
        'success': True,
        'converted': 0,
        'skipped': 0,
        'errors': [],
        'details': []
    }
    detail_count = 0

    def add_detail(message):
        nonlocal detail_count
        if (verbose or dry_run) and detail_count < detail_limit:
            results['details'].append(message)
            detail_count += 1

    keys_ok, keys_error = _ensure_nsz_keys()
    if not keys_ok:
        results['success'] = False
        results['errors'].append(keys_error)
        add_detail(keys_error)
        return results

    if '{nsz_exe}' in (command_template or '') and not _get_nsz_exe():
        warning = 'NSZ tool not found in ./nsz; using PATH lookup.'
        add_detail(warning)
        if log_cb:
            log_cb(warning)

    query = Files.query.filter(Files.extension.in_(['nsp', 'xci']))
    if library_id:
        query = query.filter_by(library_id=library_id)
    files = query.all()
    total_files = len(files)
    processed = 0
    if progress_cb:
        progress_cb(0, total_files)
    if log_cb:
        log_cb(f"Found {total_files} file(s) to convert.")
    for file_entry in files:
        if cancel_cb and cancel_cb():
            if log_cb:
                log_cb('Conversion cancelled.')
            break
        if not file_entry.filepath or not os.path.exists(file_entry.filepath):
            results['skipped'] += 1
            add_detail('Skip missing file path.')
            if log_cb:
                log_cb('Skip missing file path.')
            processed += 1
            if progress_cb:
                progress_cb(processed, total_files)
            continue

        if min_size_bytes and file_entry.size and file_entry.size < min_size_bytes:
            results['skipped'] += 1
            add_detail(f"Skip small file (<{min_size_bytes} bytes): {file_entry.filepath}.")
            if log_cb:
                log_cb(f"Skip small file (<{min_size_bytes} bytes): {file_entry.filepath}.")
            processed += 1
            if progress_cb:
                progress_cb(processed, total_files)
            continue

        output_file = os.path.splitext(file_entry.filepath)[0] + '.nsz'
        if os.path.exists(output_file):
            results['skipped'] += 1
            add_detail(f"Skip existing output: {output_file}.")
            if log_cb:
                log_cb(f"Skip existing output: {output_file}.")
            processed += 1
            if progress_cb:
                progress_cb(processed, total_files)
            continue

        command = _format_nsz_command(
            command_template,
            file_entry.filepath,
            output_file,
            threads=threads
        )

        if dry_run:
            results['converted'] += 1
            add_detail(f"Plan convert: {file_entry.filepath} -> {output_file}.")
            if log_cb:
                log_cb(f"Plan convert: {file_entry.filepath} -> {output_file}.")
            processed += 1
            if progress_cb:
                progress_cb(processed, total_files)
            continue

        try:
            if log_cb:
                log_cb(f"Running: {command}")
            process = _run_command(
                command,
                log_cb=log_cb,
                stream_output=stream_output,
                cancel_cb=cancel_cb,
                timeout_seconds=timeout_seconds
            )
            if process.returncode != 0:
                results['errors'].append(process.stderr.strip() or 'Conversion failed.')
                add_detail(f"Error converting {file_entry.filepath}: {process.stderr.strip() or 'Conversion failed.'}.")
                processed += 1
                if progress_cb:
                    progress_cb(processed, total_files)
                continue
            if not os.path.exists(output_file):
                results['errors'].append(f'Output not found: {output_file}')
                add_detail(f"Error missing output: {output_file}.")
                processed += 1
                if progress_cb:
                    progress_cb(processed, total_files)
                continue

            if delete_original:
                old_path = file_entry.filepath
                if os.path.exists(old_path):
                    os.remove(old_path)
                library_path = get_library_path(file_entry.library_id)
                update_file_path(library_path, old_path, output_file)
                file_entry.extension = 'nsz'
                file_entry.compressed = True
                file_entry.size = os.path.getsize(output_file)
                db.session.commit()
                add_detail(f"Converted and replaced: {old_path} -> {output_file}.")
            else:
                library_path = get_library_path(file_entry.library_id)
                folder = _compute_relative_folder(library_path, output_file)
                existing_file = Files.query.filter_by(filepath=output_file).first()
                if existing_file:
                    existing_file.extension = 'nsz'
                    existing_file.compressed = True
                    existing_file.size = os.path.getsize(output_file)
                    for app in list(file_entry.apps):
                        if existing_file not in app.files:
                            app.files.append(existing_file)
                    db.session.commit()
                    add_detail(f"Converted output already indexed: {output_file}.")
                else:
                    new_file = Files(
                        filepath=output_file,
                        library_id=file_entry.library_id,
                        folder=folder,
                        filename=os.path.basename(output_file),
                        extension='nsz',
                        size=os.path.getsize(output_file),
                        compressed=True,
                        multicontent=file_entry.multicontent,
                        nb_content=file_entry.nb_content,
                        identified=True,
                        identification_type=file_entry.identification_type,
                        identification_attempts=file_entry.identification_attempts,
                        last_attempt=file_entry.last_attempt
                    )
                    db.session.add(new_file)
                    db.session.flush()
                    for app in list(file_entry.apps):
                        app.files.append(new_file)
                    db.session.commit()
                    add_detail(f"Converted: {file_entry.filepath} -> {output_file}.")

            results['converted'] += 1
            processed += 1
            if progress_cb:
                progress_cb(processed, total_files)
        except Exception as e:
            logger.error(f"Failed to convert {file_entry.filepath}: {e}")
            results['errors'].append(str(e))
            add_detail(f"Error converting {file_entry.filepath}: {e}.")
            processed += 1
            if progress_cb:
                progress_cb(processed, total_files)

    if results['errors']:
        results['success'] = False
    return results

def list_convertible_files(limit=2000, library_id=None, min_size_bytes=50 * 1024 * 1024):
    query = Files.query.filter(Files.extension.in_(['nsp', 'xci']))
    if library_id:
        query = query.filter_by(library_id=library_id)
    files = query.limit(limit).all()
    filtered = [
        {
            'id': file.id,
            'filename': file.filename,
            'filepath': file.filepath,
            'extension': file.extension,
            'size': file.size or 0
        }
        for file in files
        if not min_size_bytes or not file.size or file.size >= min_size_bytes
    ]
    return filtered

def convert_single_to_nsz(file_id, command_template, delete_original=True, dry_run=False, verbose=False, log_cb=None, progress_cb=None, stream_output=False, threads=None, cancel_cb=None, timeout_seconds=None):
    results = {
        'success': True,
        'converted': 0,
        'skipped': 0,
        'errors': [],
        'details': []
    }

    file_entry = Files.query.filter_by(id=file_id).first()
    if not file_entry:
        return {
            'success': False,
            'converted': 0,
            'skipped': 0,
            'errors': ['File not found.'],
            'details': []
        }

    if not file_entry.filepath or not os.path.exists(file_entry.filepath):
        results['success'] = False
        results['errors'].append('File path missing.')
        return results

    keys_ok, keys_error = _ensure_nsz_keys()
    if not keys_ok:
        results['success'] = False
        results['errors'].append(keys_error)
        if verbose:
            results['details'].append(keys_error)
        return results

    if '{nsz_exe}' in (command_template or '') and not _get_nsz_exe() and verbose:
        warning = 'NSZ tool not found in ./nsz; using PATH lookup.'
        results['details'].append(warning)
        if log_cb:
            log_cb(warning)
    output_file = os.path.splitext(file_entry.filepath)[0] + '.nsz'
    if os.path.exists(output_file):
        results['skipped'] = 1
        if verbose:
            results['details'].append(f"Skip existing output: {output_file}.")
        return results

    nsz_exe = _get_nsz_exe() or 'nsz'
    command = _format_nsz_command(
        command_template,
        file_entry.filepath,
        output_file,
        threads=threads
    )

    if cancel_cb and cancel_cb():
        if log_cb:
            log_cb('Conversion cancelled.')
        return results

    if dry_run:
        results['converted'] = 1
        if verbose:
            results['details'].append(f"Plan convert: {file_entry.filepath} -> {output_file}.")
        if progress_cb:
            progress_cb(1, 1)
        return results

    try:
        if log_cb:
            log_cb(f"Running: {command}")
        process = _run_command(
            command,
            log_cb=log_cb,
            stream_output=stream_output,
            cancel_cb=cancel_cb,
            timeout_seconds=timeout_seconds
        )
        if process.returncode != 0:
            results['success'] = False
            results['errors'].append(process.stderr.strip() or 'Conversion failed.')
            if verbose:
                results['details'].append(f"Error converting {file_entry.filepath}: {process.stderr.strip() or 'Conversion failed.'}.")
            if progress_cb:
                progress_cb(1, 1)
            return results
        if not os.path.exists(output_file):
            results['success'] = False
            results['errors'].append(f'Output not found: {output_file}')
            if verbose:
                results['details'].append(f"Error missing output: {output_file}.")
            if progress_cb:
                progress_cb(1, 1)
            return results

        if delete_original:
            old_path = file_entry.filepath
            if os.path.exists(old_path):
                os.remove(old_path)
            library_path = get_library_path(file_entry.library_id)
            update_file_path(library_path, old_path, output_file)
            file_entry.extension = 'nsz'
            file_entry.compressed = True
            file_entry.size = os.path.getsize(output_file)
            db.session.commit()
            if verbose:
                results['details'].append(f"Converted and replaced: {old_path} -> {output_file}.")
        else:
            library_path = get_library_path(file_entry.library_id)
            folder = _compute_relative_folder(library_path, output_file)
            existing_file = Files.query.filter_by(filepath=output_file).first()
            if existing_file:
                existing_file.extension = 'nsz'
                existing_file.compressed = True
                existing_file.size = os.path.getsize(output_file)
                for app in list(file_entry.apps):
                    if existing_file not in app.files:
                        app.files.append(existing_file)
                db.session.commit()
                if verbose:
                    results['details'].append(f"Converted output already indexed: {output_file}.")
            else:
                new_file = Files(
                    filepath=output_file,
                    library_id=file_entry.library_id,
                    folder=folder,
                    filename=os.path.basename(output_file),
                    extension='nsz',
                    size=os.path.getsize(output_file),
                    compressed=True,
                    multicontent=file_entry.multicontent,
                    nb_content=file_entry.nb_content,
                    identified=True,
                    identification_type=file_entry.identification_type,
                    identification_attempts=file_entry.identification_attempts,
                    last_attempt=file_entry.last_attempt
                )
                db.session.add(new_file)
                db.session.flush()
                for app in list(file_entry.apps):
                    app.files.append(new_file)
                db.session.commit()
            if verbose:
                results['details'].append(f"Converted: {file_entry.filepath} -> {output_file}.")

        results['converted'] = 1
        if progress_cb:
            progress_cb(1, 1)
    except Exception as e:
        logger.error(f"Failed to convert {file_entry.filepath}: {e}")
        results['success'] = False
        results['errors'].append(str(e))
        if verbose:
            results['details'].append(f"Error converting {file_entry.filepath}: {e}.")
        if progress_cb:
            progress_cb(1, 1)

    return results
