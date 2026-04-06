import hashlib
import os
import shutil
from constants import *
from db import *
import titles as titles_lib
import datetime
import sys
from pathlib import Path
from utils import *
from db import update_file_path

def sanitize_filename(name, windows_compatible=False):
    if sys.platform == 'win32' or windows_compatible:
        forbidden_chars = FORBIDDEN_CHARS_WINDOWS
        # Replace forbidden characters with underscore
        sanitized = ''.join('' if c in forbidden_chars else c for c in name)
        # Remove trailing periods and spaces specific to Windows
        sanitized = sanitized.strip().rstrip('. ')
        # Handle Windows reserved names
        if sanitized.lower() in RESERVED_NAMES_WINDOWS:
            sanitized = '_' + sanitized # Prepend an underscore to avoid conflict
    else:
        forbidden_chars = FORBIDDEN_CHARS_UNIX
        # Replace forbidden characters with underscore
        sanitized = ''.join('_' if c in forbidden_chars else c for c in name)
        # Remove leading/trailing spaces (general good practice)
        sanitized = sanitized.strip()

    return sanitized

def organize_file(file_obj, library_path, organizer_settings):
    try:
        templates = organizer_settings['templates']
        
        current_filepath = file_obj.filepath
        
        # Get the associated app for the file
        app = file_obj.apps[0] if file_obj.apps else None
        if not app:
            logger.warning(f"No app associated with file {file_obj.filename}. Skipping organization.")
            return

        template = _get_template_for_file(file_obj, app, templates)

        # Retrieve data for template formatting
        format_data = {}
        # Get title name from the associated title_id
        title_info = titles_lib.get_game_info(app.title.title_id)
        if title_info['name'] == 'Unrecognized':
            logger.warning(f"No title info associated with file {file_obj.filename}. Skipping organization.")
            return
        format_data["extension"] = file_obj.extension
        format_data["titleId"] = app.title.title_id
        format_data["titleName"] = title_info['name']
        if not file_obj.multicontent:
            format_data["appId"] = app.app_id
            format_data["appVersion"] = app.app_version
            format_data["patchLevel"] = titles_lib.get_update_number(app.app_version)

            game_info = titles_lib.get_game_info(app.app_id)
            if app.app_type == APP_TYPE_DLC:
                format_data["appName"] = game_info['name']
            else:
                format_data["appName"] = title_info['name']
        
        # Format the new relative path and remove leading slash if present
        raw_path = template.format(**format_data).lstrip('/')
        windows_compatible = organizer_settings.get('windows_compatible', False)
        safe_parts = [sanitize_filename(part, windows_compatible) for part in Path(raw_path).parts]
        new_relative_path = os.path.join(*safe_parts)
        
        # Construct the full new path
        new_full_path = os.path.join(library_path, new_relative_path)

        if current_filepath == new_full_path:
            return
        
        # Ensure the directory exists
        new_dir = os.path.dirname(new_full_path)
        try:
            os.makedirs(new_dir, exist_ok=True)
        except OSError as e:
            logger.error(f"Error creating directory {new_dir} for file {file_obj.filename}: {e}")
            return
        
        # Move the file, handling duplicates
        base_name = os.path.splitext(os.path.basename(new_full_path))[0]
        
        counter = 1
        final_new_full_path = new_full_path
        while os.path.exists(final_new_full_path):
            if final_new_full_path == current_filepath:
                return
            counter += 1
            new_filename = f"{base_name}({counter}).{file_obj.extension}"
            final_new_full_path = os.path.join(new_dir, new_filename)
        
        logger.info(f'Organizing file: {file_obj.filename}')
        try:
            # Add the move event to the ignored list before performing the move
            add_ignored_event(current_filepath, final_new_full_path)

            shutil.move(current_filepath, final_new_full_path)
            logger.info(f"Moved '{current_filepath}' to '{final_new_full_path}'")
            
            # Update the file path in the database
            # Get the library path from the library ID
            library_path_str = get_library_path(file_obj.library_id)
            update_file_path(library_path_str, current_filepath, final_new_full_path)

        except (shutil.Error, OSError) as e:
            logger.error(f"Error moving file from '{current_filepath}' to '{final_new_full_path}': {e}")
            # If an error occurs, remove from the ignored list
            pop_ignored_event(src_path=current_filepath, dest_path=final_new_full_path)
        # No finally block needed for removing from ignored_move_events, as it's removed by the watchdog handler

    except Exception as e:
        logger.error(f"An unexpected error occurred while organizing file {file_obj.filename}: {e}")

def _get_template_for_file(file_obj, app, templates):
    """Helper function to determine the correct template for file organization."""
    if file_obj.multicontent:
        template_key = "multi"
    else:
        if app.app_type == APP_TYPE_BASE:
            template_key = "base"
        elif app.app_type == APP_TYPE_UPD:
            template_key = "update"
        elif app.app_type == APP_TYPE_DLC:
            template_key = "dlc"
    
    return templates.get(template_key) + '.{extension}'


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
            
            # Remove file-app associations
            for file_id in file_ids:
                remove_file_from_apps(file_id)

            # Delete library (cascade will delete files automatically)
            db.session.delete(library)
            db.session.commit()

            # Clean up orphaned apps and titles
            apps_removed = remove_apps_without_files()
            titles_removed = remove_titles_without_apps()

            logger.info(f"Removed library: {path}")
            if apps_removed > 0:
                logger.info(f"Removed {apps_removed} orphaned apps.")
            if titles_removed > 0:
                logger.info(f"Removed {titles_removed} orphaned titles.")
        
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
    if isinstance(library, int) or library.isdigit():
        library_id = library
        library_path = get_library_path(library_id)
    else:
        library_path = library
        library_id = get_library_id(library_path)

    library_path = get_library_path(library_id)
    
    # Get existing file paths in the library
    filepaths_in_db = get_library_file_paths(library_id)
    
    # Filter out files that are already in the database
    new_files_to_add = [f for f in files if f not in filepaths_in_db]
    
    if not new_files_to_add:
        return

    nb_to_identify = len(new_files_to_add)
    for n, filepath in enumerate(new_files_to_add):
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

        try:
            db.session.flush()
        except Exception:
            # Another worker already added this file concurrently
            db.session.rollback()
            continue

        # Commit every 100 files to avoid excessive memory use
        if (n + 1) % 100 == 0:
            db.session.commit()

    # Final commit
    db.session.commit()

def get_files_to_identify(library_id):
    non_identified_files = get_all_non_identified_files_from_library(library_id)
    if titles_lib.Keys.keys_loaded:
        files_to_identify_with_cnmt = get_files_with_identification_from_library(library_id, 'filename')
        non_identified_files = list(set(non_identified_files).union(files_to_identify_with_cnmt))
    return non_identified_files

def add_missing_apps_to_db():
    logger.info('Adding missing apps to database...')
    titles = get_all_titles()
    apps_added = 0
    
    for n, title in enumerate(titles):
        title_id = title.title_id
        title_db_id = get_title_id_db_id(title_id)
        
        # Add base game if not present at all (any version)
        existing_bases = [
            a for a in get_all_title_apps(title_id)
            if a.get("app_type") == APP_TYPE_BASE
        ]

        if not existing_bases:
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

def remove_outdated_update_files():
    logger.info("Starting removal of outdated update files...")
    try:
        titles = get_all_titles()
        
        for title in titles:
            title_apps = get_all_title_apps(title.title_id)
            
            # Filter for owned update apps
            owned_update_apps = [app for app in title_apps if app.get('app_type') == APP_TYPE_UPD and app.get('owned')]
            
            # If there's only one or no owned update apps, there's no "greater version available" to compare against.
            if len(owned_update_apps) <= 1:
                continue
            
            # Group owned update apps by their version for easy lookup
            owned_versions = {int(app['app_version']) for app in owned_update_apps}
            
            # Iterate through all update apps (owned or not) for this title
            for app_data in title_apps:
                if app_data.get('app_type') == APP_TYPE_UPD:
                    current_app_version = int(app_data['app_version'])
                    
                    # Check if there's a greater owned version available for this title
                    has_greater_owned_version = any(
                        owned_v > current_app_version for owned_v in owned_versions
                    )
                    
                    if has_greater_owned_version:
                        # Get the actual App object from the database
                        app_obj = get_app_by_id_and_version(app_data['app_id'], app_data['app_version'])
                        
                        if app_obj:
                            # Get files associated with this specific app version
                            # Create a list to iterate over as the original collection might change during deletion
                            files_to_process = list(app_obj.files) 
                            for file_obj in files_to_process:
                                # Check if file meets criteria: identified, not multicontent
                                if file_obj.identified and not file_obj.multicontent:
                                    logger.info(f"Removing outdated update file: {file_obj.filepath} (App ID: {app_obj.app_id}, Version: {app_obj.app_version}) - Greater owned version available.")
                                    
                                    # Remove from disk
                                    if os.path.exists(file_obj.filepath):
                                        try:
                                            # Add the delete event to the ignored list before performing the remove
                                            add_ignored_event(file_obj.filepath, '')
                                            os.remove(file_obj.filepath)
                                            logger.debug(f"Deleted physical file: {file_obj.filepath}")
                                            # Remove from database and update app owned status
                                            # This function handles db.session.delete(file_obj) and app.owned status
                                            remove_file_from_apps(file_obj.id)
                                        except OSError as e:
                                            logger.error(f"Error deleting physical file {file_obj.filepath}: {e}")
                                            # If an error occurs, remove from the ignored list
                                            pop_ignored_event(src_path=file_obj.filepath, dest_path='')
                                    else:
                                        logger.warning(f"Physical file not found for deletion: {file_obj.filepath}")
                                    
        logger.info(f"Finished removal of outdated update files.")
    except Exception as e:
        logger.error(f"Error during removal of outdated update files: {e}")

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
                # Only mark as up to date if the base itself is owned and up_to_date
                title['has_latest_version'] = (
                    title_obj.have_base and title_obj.up_to_date
                )
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
            title['owned'] = any(app.get('owned') for app in dlc_apps)

            # Check if this DLC has latest version
            if dlc_apps:
                highest_version = max(int(app['app_version']) for app in dlc_apps)
                owned_versions = [int(app['app_version']) for app in dlc_apps if app.get('owned')]
                # Only true if at least one version is OWNED and the highest owned >= highest available
                title['has_latest_version'] = (
                    len(owned_versions) > 0 and max(owned_versions) >= highest_version
                )
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
