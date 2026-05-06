import os
import re
import shutil
from constants import *
from db import *
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
import titles as titles_lib
import sys
from pathlib import Path
from utils import *
from db import update_file_path

def prepare_template_names(format_data, windows_compatible):
    """Sanitize the names before formatting, so they cannot introduce path separators, and cap their length."""
    names = {k: sanitize_filename(v, windows_compatible) for k, v in format_data.items() if k in TEMPLATE_NAME_KEYS}
    if sys.platform == 'win32' or windows_compatible:
        names = {k: trim_name(v, MAX_NAME_WINDOWS) for k, v in names.items()}

    return {**format_data, **names}

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
        
        # Format the new relative path, sanitizing and shortening the names first
        windows_compatible = organizer_settings.get('windows_compatible', False)
        format_data = prepare_template_names(format_data, windows_compatible)
        safe_parts = sanitized_path_parts(template.format(**format_data), windows_compatible)
        if sys.platform == 'win32' or windows_compatible:
            safe_parts = truncate_path_parts(safe_parts, len(library_path))
        new_relative_path = os.path.join(*safe_parts)
        
        # Construct the full new path
        new_full_path = os.path.join(library_path, new_relative_path)

        if current_filepath == new_full_path:
            return True

        # Already organized with an "(n)" suffix from a previous collision:
        # Avoid re-running the rename loop only to bail out at the same name.
        new_dir_norm = os.path.dirname(new_full_path)
        base_name = os.path.splitext(os.path.basename(new_full_path))[0]
        current_dir = os.path.dirname(current_filepath)
        current_name = os.path.basename(current_filepath)
        if current_dir == new_dir_norm and os.path.exists(new_full_path) and re.fullmatch(
            rf"{re.escape(base_name)}\(\d+\)\.{re.escape(file_obj.extension)}",
            current_name,
        ):
            return True
        
        # Ensure the directory exists
        new_dir = os.path.dirname(new_full_path)
        try:
            os.makedirs(new_dir, exist_ok=True)
        except OSError as e:
            logger.error(f"Error creating directory {new_dir} for file {file_obj.filename}: {e}")
            return
        
        # Move the file, handling duplicates.
        library_path_str = get_library_path(file_obj.library_id)
        original_filename = file_obj.filename

        counter = 1
        candidate = new_full_path
        src = current_filepath
        while True:
            if candidate == current_filepath:
                return True
            try:
                add_ignored_event(src, candidate)
                if os.path.exists(candidate):
                    raise FileExistsError(candidate)
                shutil.move(src, candidate)
                update_file_path(library_path_str, current_filepath, candidate)
                rel = os.path.relpath(candidate, library_path_str)
                logger.info(f'Organizing file: {original_filename} → {rel}')
                return True
            except (FileExistsError, IntegrityError) as e:
                pop_ignored_event(src_path=src, dest_path=candidate)
                # If the move already happened, the file is now at `candidate`;
                # the next iteration must move from there, not from the original.
                if os.path.exists(candidate) and not os.path.exists(src):
                    src = candidate
                counter += 1
                candidate = os.path.join(new_dir, f"{base_name}({counter}).{file_obj.extension}")
            except (shutil.Error, OSError) as e:
                logger.error(f"Error moving file from '{src}' to '{candidate}': {e}")
                pop_ignored_event(src_path=src, dest_path=candidate)
                return
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
    """Remove a library: stop watching, drop from settings, enqueue DB cleanup task."""
    from settings import delete_library_path_from_settings
    import tasks as tasks_mod

    with app.app_context():
        watcher.remove_directory(path)
        success, errors = delete_library_path_from_settings(path)
        if success:
            tasks_mod.enqueue_task('remove_library', {'library_path': path})
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

def get_files_to_identify(library_id):
    non_identified_files = get_all_non_identified_files_from_library(library_id)
    if titles_lib.Keys.keys_loaded:
        files_to_identify_with_cnmt = get_files_with_identification_from_library(library_id, 'filename')
        non_identified_files = list(set(non_identified_files).union(files_to_identify_with_cnmt))
    return non_identified_files

def add_missing_apps_for_title(title_id):
    """Expand missing base/update/DLC apps (owned=False) for a single title via one bulk upsert.
    Safe to run concurrently with other workers expanding the same title."""
    title_db_id = get_title_id_db_id(title_id)

    rows = []
    update_app_id = title_id[:-3] + '800'
    base_added = False
    for version_info in titles_lib.get_all_existing_versions(title_id):
        v = str(version_info['version'])
        if v == '0':
            rows.append(dict(app_id=title_id, app_version=v, app_type=APP_TYPE_BASE,
                             owned=False, title_id=title_db_id,
                             release_date=version_info.get('release_date')))
            base_added = True
        else:
            rows.append(dict(app_id=update_app_id, app_version=v, app_type=APP_TYPE_UPD,
                             owned=False, title_id=title_db_id,
                             release_date=version_info.get('release_date')))

    if not base_added:
        rows.append(dict(app_id=title_id, app_version="0", app_type=APP_TYPE_BASE,
                         owned=False, title_id=title_db_id, release_date=None))

    for dlc_app_id, dlc_version, dlc_release_date in titles_lib.get_all_dlc_versions(title_id):
        rows.append(dict(app_id=dlc_app_id, app_version=str(dlc_version),
                         app_type=APP_TYPE_DLC, owned=False, title_id=title_db_id,
                         release_date=dlc_release_date))

    # Only refresh release_date on conflict — never touch `owned` or any other
    # column, since this same row may have been flipped to owned=True by a file
    # scan in between.
    stmt = sqlite_insert(Apps.__table__).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=['app_id', 'app_version'],
        set_={'release_date': stmt.excluded.release_date},
        where=Apps.__table__.c.release_date.is_not(stmt.excluded.release_date),
    )
    result = db.session.execute(stmt)
    db.session.commit()
    apps_upserted = result.rowcount or 0
    if apps_upserted:
        logger.debug(f'Upserted {apps_upserted} apps for Title ID {title_id}')
    return apps_upserted


def add_missing_apps_to_db():
    """Batch: expand missing apps for every title. Used post-titledb-update."""
    logger.info('Adding missing apps to database...')
    titles = get_all_titles()
    total = 0
    for n, title in enumerate(titles):
        total += add_missing_apps_for_title(title.title_id)
        if (n + 1) % 100 == 0:
            logger.info(f'Processed {n + 1}/{len(titles)} titles, upserted {total} apps so far')
    logger.info(f'Finished adding missing apps to database. Total apps upserted: {total}')

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

def update_title_flags(title_id):
    """Recompute have_base / up_to_date / complete for a single title.
    Wrapped in BEGIN IMMEDIATE to serialize concurrent recomputes and prevent
    lost updates when another worker is mutating owned state for the same title."""
    connection = db.engine.raw_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("BEGIN IMMEDIATE")

        cursor.execute("SELECT id FROM titles WHERE title_id = ?", (title_id,))
        row = cursor.fetchone()
        if not row:
            connection.commit()
            return
        title_db_id = row[0]

        cursor.execute(
            "SELECT app_type, app_version, owned FROM apps WHERE title_id = ?",
            (title_db_id,)
        )
        title_apps = [{'app_type': r[0], 'app_version': r[1], 'owned': bool(r[2])} for r in cursor.fetchall()]

        owned_base_apps = [a for a in title_apps if a['app_type'] == APP_TYPE_BASE and a['owned']]
        have_base = len(owned_base_apps) > 0

        available_update_apps = [a for a in title_apps if a['app_type'] == APP_TYPE_UPD]
        owned_update_apps = [a for a in available_update_apps if a['owned']]
        if not available_update_apps:
            up_to_date = True
        elif not owned_update_apps:
            up_to_date = False
        else:
            highest_available = max(int(a['app_version']) for a in available_update_apps)
            highest_owned = max(int(a['app_version']) for a in owned_update_apps)
            up_to_date = highest_owned >= highest_available

        cursor.execute(
            "SELECT app_id, app_version, owned FROM apps WHERE title_id = ? AND app_type = ?",
            (title_db_id, APP_TYPE_DLC)
        )
        dlc_by_id = {}
        for dlc_app_id, version_str, owned in cursor.fetchall():
            version = int(version_str)
            if dlc_app_id not in dlc_by_id or version > dlc_by_id[dlc_app_id]['version']:
                dlc_by_id[dlc_app_id] = {'version': version, 'owned': bool(owned)}
        complete = all(d['owned'] for d in dlc_by_id.values()) if dlc_by_id else True

        cursor.execute(
            "UPDATE titles SET have_base = ?, up_to_date = ?, complete = ? WHERE id = ?",
            (int(have_base), int(up_to_date), int(complete), title_db_id)
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def update_titles():
    """Batch: recompute all titles. Also removes titles with no owned apps."""
    titles_removed = remove_titles_without_owned_apps()
    if titles_removed > 0:
        logger.info(f"Removed {titles_removed} titles with no owned apps.")

    for title in get_all_titles():
        update_title_flags(title.title_id)

