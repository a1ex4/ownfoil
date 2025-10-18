import datetime
import json
import hashlib
import os
import shutil
from constants import *
from db import *
from overrides import build_override_index
import titles as titles_lib
from pathlib import Path
from sqlalchemy import or_
from utils import *
from settings import load_settings
from db import update_file_path

def organize_file(file_obj, library_path, organizer_settings, watcher):
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
        new_relative_path = template.format(**format_data).lstrip('/')

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
            with watcher.event_handler.ignored_events_lock:
                watcher.event_handler.ignored_events_tuples.add((current_filepath, final_new_full_path))
            
            shutil.move(current_filepath, final_new_full_path)
            logger.info(f"Moved '{current_filepath}' to '{final_new_full_path}'")
            
            # Update the file path in the database
            # Get the library path from the library ID
            library_path_str = get_library_path(file_obj.library_id)
            update_file_path(library_path_str, current_filepath, final_new_full_path)
            # logger.info(f"Updated database for file '{current_filepath}' to '{final_new_full_path}'")

        except (shutil.Error, OSError) as e:
            logger.error(f"Error moving file from '{current_filepath}' to '{final_new_full_path}': {e}")
            # If an error occurs, ensure the event is removed from the ignored list
            with watcher.event_handler.ignored_events_lock:
                if (current_filepath, final_new_full_path) in watcher.event_handler.ignored_events_tuples:
                    watcher.event_handler.ignored_events_tuples.remove((current_filepath, final_new_full_path))
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
    """
    Upsert files into the Files table.
    - Always inserts a row even when file_info is None.
    - Marks unidentified files via identification_type="filename".
    - Updates existing rows in place.
    - Commits in batches of 100.
    """
    nb_to_identify = len(files)

    # Resolve library_id/path
    if isinstance(library, int) or (isinstance(library, str) and library.isdigit()):
        library_id = int(library)
        library_path = get_library_path(library_id)
    else:
        library_path = library
        library_id = get_library_id(library_path)

    library_path = get_library_path(library_id)
    for n, filepath in enumerate(files):
        file_rel = filepath.replace(library_path, "")
        logger.info(f'Getting file info ({n+1}/{nb_to_identify}): {file_rel}')

        # Basic FS info
        filename = os.path.basename(filepath)
        extension = os.path.splitext(filename)[1].lstrip('.').lower()
        try:
            size = os.path.getsize(filepath)
        except Exception:
            size = None

        # Best-effort metadata probe (non-final)
        try:
            file_info = titles_lib.get_file_info(filepath)
        except Exception as e:
            logger.exception(f"Error getting info for file {file_rel}: {e}")
            file_info = None

        # Upsert by unique filepath
        existing = Files.query.filter_by(filepath=filepath).first()
        if existing:
            # Refresh core info (prefer parsed values when present)
            existing.folder = (file_info.get("filedir") if file_info else os.path.dirname(filepath))
            existing.filename = (file_info.get("filename") if file_info else filename)
            existing.extension = (file_info.get("extension") if file_info else extension)
            existing.size = (file_info.get("size") if (file_info and "size" in file_info) else size)

            # STAGE for deep identification (don't finalize here)
            if file_info is None:
                existing.identified = False
                existing.identification_type = "unidentified"
                existing.identification_error = "Failed to parse file info"
            else:
                existing.identified = False
                existing.identification_type = "filename"
                existing.identification_error = None

            existing.identification_attempts = (existing.identification_attempts or 0) + 1
            existing.last_attempt = datetime.datetime.utcnow()

        else:
            # Insert new staged row
            if file_info is None:
                new_file = Files(
                    filepath=filepath,
                    library_id=library_id,
                    folder=os.path.dirname(filepath),
                    filename=filename,
                    extension=extension,
                    size=size,
                    identified=False,
                    identification_type="unidentified",
                    identification_error="Failed to parse file info",
                    identification_attempts=1,
                    last_attempt=datetime.datetime.utcnow(),
                )
            else:
                new_file = Files(
                    filepath=filepath,
                    library_id=library_id,
                    folder=file_info.get("filedir", os.path.dirname(filepath)),
                    filename=file_info.get("filename", filename),
                    extension=file_info.get("extension", extension),
                    size=file_info.get("size", size),
                    identified=False,                 # <- STAGED, not final
                    identification_type="filename",   # <- STAGED marker
                    identification_error=None,
                    identification_attempts=1,
                    last_attempt=datetime.datetime.utcnow(),
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

def get_files_to_identify(library_id, *, force_all: bool = False):
    q = Files.query.filter(Files.library_id == library_id)

    if force_all:
        return q.order_by(Files.last_attempt.asc().nullsfirst()).all()

    staged = ("filename", "titles_lib") # staged markers
    seven_days_ago = datetime.datetime.utcnow() - datetime.timedelta(days=7)

    q = q.filter(
        or_(
            Files.identified.is_(False), # not yet identified
            Files.identification_type.in_(staged), # staged by first pass
            Files.last_attempt.is_(None), # never attempted
            Files.last_attempt < seven_days_ago, # stale
        )
    )

    return q.order_by(Files.last_attempt.asc().nullsfirst()).all()

def identify_library_files(library):
    # Resolve library_id / path
    if isinstance(library, int) or (isinstance(library, str) and library.isdigit()):
        library_id = int(library)
        library_path = get_library_path(library_id)
    else:
        library_path = library
        library_id = get_library_id(library_path)

    files_to_identify = get_files_to_identify(library_id)
    nb_to_identify = len(files_to_identify)

    # Load TitleDB once so we can check presence of title_ids quickly
    titles_lib.load_titledb()
    try:
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
                    # Unique title_ids present in this file
                    title_ids = list(dict.fromkeys([c['title_id'] for c in file_contents]))

                    # Ensure Titles table has those IDs
                    for title_id in title_ids:
                        add_title_id_in_db(title_id)

                    nb_content = 0
                    for file_content in file_contents:
                        logger.info(
                            f'Identifying file ({n+1}/{nb_to_identify}) - '
                            f'Found content Title ID: {file_content["title_id"]} '
                            f'App ID : {file_content["app_id"]} '
                            f'Title Type: {file_content["type"]} '
                            f'Version: {file_content["version"]}'
                        )
                        # Upsert Apps and attach file via M2M
                        title_id_in_db = get_title_id_db_id(file_content["title_id"])

                        # Idempotent get-or-create, then link file and flip owned=True
                        if file_content["type"] == APP_TYPE_BASE:
                            app_row, _ = _ensure_single_latest_base(
                                app_id=file_content["app_id"],
                                detected_version=file_content["version"],
                                title_db_id=title_id_in_db
                            )
                        else:
                            app_row, _ = _get_or_create_app(
                                app_id=file_content["app_id"],
                                app_version=file_content["version"],
                                app_type=file_content["type"],
                                title_db_id=title_id_in_db
                            )

                        # Ensure a newly-added row is visible to the session query used by add_file_to_app
                        db.session.flush()
                        linked = add_file_to_app(file_content["app_id"], file_content["version"], file_id, commit=False)
                        if not linked:
                            file_obj = get_file_from_db(file_id)
                            if file_obj and file_obj not in app_row.files:
                                app_row.files.append(file_obj)
                            app_row.owned = True

                        nb_content += 1

                    # Update multi-content flags
                    file.multicontent = nb_content > 1
                    file.nb_content = nb_content

                    # Determine if any of this file's title_ids are unknown to TitleDB
                    needs_override = any(not titles_lib.has_title_id(tid) for tid in title_ids)

                    # - identified=True means "we parsed/understood the file (CNMT etc.)"
                    # - recognition in TitleDB is tracked via identification_type
                    file.identified = True
                    file.identification_type = "not_in_titledb" if needs_override else identification  # e.g., 'cnmt'
                    file.identification_error = None  # not an error condition

                else:
                    # Failed to identify contents
                    logger.warning(f"Error identifying file {filename}: {error}")
                    file.identification_error = error
                    file.identified = False
                    if not getattr(file, "identification_type", None):
                        file.identification_type = "exception"

            except Exception as e:
                logger.warning(f"Error identifying file {getattr(file, 'filename', '<unknown>')}: {e}")
                file.identification_error = str(e)
                file.identified = False
                # keep identification_type as-is if set earlier; otherwise mark generic
                if not getattr(file, "identification_type", None):
                    file.identification_type = "exception"

            # finally update attempts/time
            file.identification_attempts = (file.identification_attempts or 0) + 1
            file.last_attempt = datetime.datetime.utcnow()

            # Commit every 100 files to avoid excessive memory use
            if (n + 1) % 100 == 0:
                db.session.commit()

        # Final commit for the batch
        db.session.commit()

    finally:
        # Keep titledb counters consistent and unload after a short debounce window
        titles_lib.unload_titledb()

def add_missing_apps_to_db():
    logger.info('Adding missing apps to database...')
    titles = get_all_titles()
    apps_added = 0

    for n, title in enumerate(titles):
        title_id = title.title_id
        title_db_id = get_title_id_db_id(title_id)

        # --- BASE (create placeholder v0 only if no BASE exists at all) ---
        has_any_base = Apps.query.filter_by(app_id=title_id, app_type=APP_TYPE_BASE).first() is not None
        if not has_any_base:
            _, created = _get_or_create_app(
                app_id=title_id,
                app_version="0",
                app_type=APP_TYPE_BASE,
                title_db_id=title_db_id
            )
            if created:
                apps_added += 1
                logger.debug(f'Added missing base app placeholder v0: {title_id}')

        # Add missing update versions
        title_versions = titles_lib.get_all_existing_versions(title_id) or []
        update_app_id = title_id[:-3] + '800'  # base->update transform
        for version_info in title_versions:
            version = str(version_info['version'])
            _, created = _get_or_create_app(
                app_id=update_app_id,
                app_version=version,
                app_type=APP_TYPE_UPD,
                title_db_id=title_db_id
            )
            if created:
                apps_added += 1
                logger.debug(f'Added missing update app: {update_app_id} v{version}')

        # --- DLCs (all known DLC app_ids + versions) ---
        title_dlc_ids = titles_lib.get_all_existing_dlc(title_id) or []
        for dlc_app_id in title_dlc_ids:
            dlc_versions = titles_lib.get_all_app_existing_versions(dlc_app_id) or []
            for dlc_version in dlc_versions:
                _, created = _get_or_create_app(
                    app_id=dlc_app_id,
                    app_version=str(dlc_version),
                    app_type=APP_TYPE_DLC,
                    title_db_id=title_db_id
                )
                if created:
                    apps_added += 1
                    logger.debug(f'Added missing DLC app: {dlc_app_id} v{dlc_version}')

        # Commit every 100 titles to avoid excessive memory use
        if (n + 1) % 100 == 0:
            db.session.commit()
            logger.info(f'Processed {n + 1}/{len(titles)} titles, added {apps_added} missing apps so far')

    # Final commit
    db.session.commit()
    logger.info(f'Finished adding missing apps. Total apps added: {apps_added}')

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

def process_library_organization(app, watcher):
    logger.info(f"Starting library organization process for all libraries...")
    try:
        app_settings = load_settings()
        organizer_settings = app_settings['library']['management']['organizer']
        if organizer_settings['enabled']:
            with app.app_context():
                libraries = get_libraries()
                for library in libraries:
                    library_path = library.path
                    # Get all identified files for the current library
                    identified_files = Files.query.filter_by(library_id=library.id, identified=True).all()
                    for file_obj in identified_files:
                        organize_file(file_obj, library_path, organizer_settings, watcher)
                    
                    # Remove empty directories if needed
                    if organizer_settings['remove_empty_folders']:
                        delete_empty_folders(library_path)

        # Remove outdated update files
        if app_settings['library']['management']['delete_older_updates']:
            remove_outdated_update_files(watcher)
    except Exception as e:
        logger.error(f"Error during library organization process: {e}")
    logger.info(f"Library organization process for all libraries completed.")

def remove_outdated_update_files(watcher):
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
                                            with watcher.event_handler.ignored_events_lock:
                                                watcher.event_handler.ignored_events_tuples.add((file_obj.filepath, ""))
                                            os.remove(file_obj.filepath)
                                            logger.debug(f"Deleted physical file: {file_obj.filepath}")
                                            # Remove from database and update app owned status
                                            # This function handles db.session.delete(file_obj) and app.owned status
                                            remove_file_from_apps(file_obj.id)
                                        except OSError as e:
                                            logger.error(f"Error deleting physical file {file_obj.filepath}: {e}")
                                            # If an error occurs, ensure the event is removed from the ignored list
                                            with watcher.event_handler.ignored_events_lock:
                                                if (file_obj.filepath, "") in watcher.event_handler.ignored_events_tuples:
                                                    watcher.event_handler.ignored_events_tuples.remove((file_obj.filepath, ""))
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
        available_update_apps = [a for a in title_apps if a.get('app_type') == APP_TYPE_UPD]
        owned_update_apps = [a for a in available_update_apps if a.get('owned')]

        if not available_update_apps:
            # No updates available, consider up to date
            up_to_date = True
        elif not owned_update_apps:
            # Updates available but none owned
            up_to_date = False
        else:
            # Find highest available version and highest owned version
            highest_available_version = max(int(app['app_version'] or 0) for app in available_update_apps)
            highest_owned_version = max(int(app['app_version'] or 0) for app in owned_update_apps)
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
    if not title:
        return {
            'has_base': False,
            'has_latest_version': False,
            'version': [],
            'has_all_dlcs': False
        }

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

def is_library_unchanged(saved_library):
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
    """
    Public entry-point for routes:

    - Load library from disk if unchanged,
    - Apply corrected_title_id merges (non-destructive),
    - Return the list used by the API layer.
    """
    # Load library from disk or regenerate if hash changed
    saved = _load_library()  # {'hash': ..., 'library': [...]}
    if not saved or 'library' not in saved:
        return []

    return _with_overridden_title_ids(saved)

def _load_library():
    """
    Load the BASE library (no overrides) from disk if hash unchanged.
    Otherwise, regenerate and save.
    """
    saved = load_library_from_disk()
    if is_library_unchanged(saved):
        return saved

    # Hash changed or cache missing/corrupt -> regenerate
    return _generate_library()

def _generate_library():
    """Generate the BASE game library from Apps table (NO overrides), and cache it to disk."""
    logger.info('Generating library ...')

    titles_lib.load_titledb()
    try:
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
            info_from_titledb = titles_lib.get_game_info(title['title_id'])
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

            title['file_basename'] = _infer_file_basename_for_app(
                title.get('app_id', ""),
                title.get('app_version', "")
            )
            games_info.append(title)
        
        _add_files_without_apps(games_info)

        library_data = {
            'hash': compute_apps_hash(),
            'library': sorted(games_info, key=lambda x: (
                "title_id_name" not in x,
                x.get("title_id_name", "Unrecognized") or "Unrecognized",
                x.get('app_id', "") or ""
            ))
        }

        # Persist snapshot to disk
        save_library_to_disk(library_data)
        logger.info('Generating library done.')

        return library_data

    finally:
        titles_lib.identification_in_progress_count -= 1
        titles_lib.unload_titledb()

def _with_overridden_title_ids(base_library):
    # Build a lightweight override map by app_id -> { corrected_title_id, ... }
    try:
        ov_idx = build_override_index() or {}
        by_app = ov_idx.get("by_app") or {}
    except Exception:
        by_app = {}

    # Apply corrected TitleDB merges per item (does not change title_id)
    titles_lib.load_titledb()
    try:
        out = [_merge_corrected_titledb(item, by_app) for item in base_library['library']]
    finally:
        titles_lib.unload_titledb()

    return out

def _infer_file_basename_for_app(app_id: str, app_version: str | int | None) -> str | None:
    """
    Return a representative file basename for the given app (id+version),
    using the Apps.files relationship. If multiple files are linked, prefer the
    most recently identified one; otherwise just pick a deterministic first.
    """
    if not app_id:
        return None

    app_row = Apps.query.filter_by(app_id=app_id, app_version=str(app_version or "0")).first()
    if not app_row or not getattr(app_row, "files", None):
        return None

    def _score(f):
        # Prefer latest identification attempt; fall back to the earliest date
        return (f.last_attempt or datetime.datetime.min,)

    best = sorted(app_row.files, key=_score, reverse=True)[0]

    # Prefer Files.filename; fall back to basename(filepath)
    if getattr(best, "filename", None):
        return best.filename
    if getattr(best, "filepath", None):
        return os.path.basename(best.filepath)
    return None

def _add_files_without_apps(games_info):
    unid_files = Files.query.filter(
        or_(
            Files.identified.is_(False),
            Files.identification_type.in_(["unidentified", "exception"])
        )
    ).all()

    for f in unid_files:
        # If this file is already linked to an App it will already be represented; skip here
        if getattr(f, "apps", None):
            try:
                if len(f.apps) > 0:
                    continue
            except Exception:
                # if relationship not configured to be immediately usable, just continue
                pass

        fname = f.filename or (os.path.basename(f.filepath) if f.filepath else None)

        games_info.append({
            # No app or title linkage
            'name': None,
            'app_id': None,
            'app_version': None,
            'app_type': APP_TYPE_BASE,        # treat as base-ish for filtering
            'title_id': None,
            'title_id_name': None,

            # Identification flags
            'identified': False,
            'identification_type': (f.identification_type or 'unidentified'),

            # What the UI needs
            'file_basename': fname,
            'filename': fname,

            # Other fields the UI expects to exist
            'owned': True,
            'has_latest_version': True,
            'has_all_dlcs': True,
            'version': [],
        })

def _merge_corrected_titledb(record: dict, override_map: dict) -> dict:
    """
    If there's an override for this app_id with a corrected_title_id, pull TitleDB
    metadata for that ID and merge onto a shallow copy of `record`. Then apply any
    explicit override fields.
    - Does NOT change the underlying title_id in the DB.
    - Echoes corrected_title_id and sets recognized_via_correction when applicable.
    """
    app_id = record.get("app_id")
    if not app_id:
        return record

    dst = record.copy()

    # --- Preserve ORIGINAL recognition flags (before any override) ---
    tid_name_raw = (record.get("title_id_name") or "").strip().lower()
    orig_recognized = bool(tid_name_raw and tid_name_raw not in ("unrecognized", "unidentified"))
    dst["hasTitleDb"] = orig_recognized
    dst["isUnrecognized"] = not orig_recognized

    ov = (override_map or {}).get(app_id)
    if not ov:
        return dst

    # --- Apply corrected_title_id merge from TitleDB (if any) ---
    corrected_id = (ov.get("corrected_title_id") or "").strip().upper()
    used_correction = False
    if corrected_id:
        try:
            td = titles_lib.get_game_info_by_title_id(corrected_id)
        except Exception:
            td = None

        if td:
            # Use TitleDB from corrected ID as baseline (display-only fields)
            if td.get("name"):
                dst["name"] = td["name"]
                # base items usually mirror name into title_id_name for sorting/search
                if dst.get("app_type") == APP_TYPE_BASE:
                    dst["title_id_name"] = td["name"]
            if td.get("bannerUrl"):
                dst["bannerUrl"] = td["bannerUrl"]
            if td.get("iconUrl"):
                dst["iconUrl"] = td["iconUrl"]
            if "category" in td and td["category"] is not None:
                dst["category"] = td["category"]
            used_correction = True

        # Surface the corrected ID for the UI
        dst["corrected_title_id"] = corrected_id

    # --- Apply explicit override fields (only when present on the override) ---
    # Name
    if "name" in ov:
        if ov["name"] is not None:
            name_val = (ov["name"] or "").strip()
            if name_val:
                dst["name"] = name_val
                dst["title_id_name"] = name_val  # keep search/sort in sync for BASE rows

    # Release date (may be None to clear)
    if "release_date" in ov:
        dst["release_date"] = ov["release_date"]

    # Artwork overrides (prefer file paths from override if present)
    banner_override = ov.get("banner_path")
    icon_override   = ov.get("icon_path")
    if banner_override:
        dst["banner_path"] = banner_override
        dst["bannerUrl"] = banner_override
    if icon_override:
        dst["icon_path"] = icon_override
        dst["iconUrl"] = icon_override

    dst["recognized_via_correction"] = bool(used_correction)

    return dst

def _v(s) -> int:
    # Accepts int or str like "65536" / "0" / "v0"
    if s is None:
        return 0
    if isinstance(s, int):
        return s
    s = str(s).lower().lstrip("v")
    try:
        return int(s, 10)
    except Exception:
        return 0

def _ensure_single_latest_base(app_id: str, detected_version, title_db_id=None):
    """
    Guarantee a single BASE row for app_id holding the HIGHEST version.
    - If none exists, create one at detected_version.
    - If one exists at lower version, upgrade that row (in-place) to detected_version.
    - If multiple exist, pick highest as winner, migrate files & overrides to it, delete losers.
    Returns: (winner_app, created_or_upgraded: bool)
    """
    Vnew = _v(detected_version)

    rows = Apps.query.filter_by(app_id=app_id, app_type=APP_TYPE_BASE).all()

    if not rows:
        # Create brand-new BASE
        winner, _ = _get_or_create_app(
            app_id=app_id,
            app_version=str(Vnew),
            app_type=APP_TYPE_BASE,
            title_db_id=title_db_id
        )
        # sanity: make sure version is exactly Vnew
        winner.app_version = str(Vnew)
        db.session.flush()
        return winner, True

    # If exactly one exists, upgrade in-place if needed
    if len(rows) == 1:
        winner = rows[0]
        Vold = _v(winner.app_version)
        if Vnew > Vold:
            winner.app_version = str(Vnew)
            if title_db_id is not None:
                winner.title_db_id = title_db_id
            db.session.flush()
            return winner, True
        else:
            return winner, False

    # Multiple exist → collapse
    rows_sorted = sorted(rows, key=lambda r: _v(r.app_version), reverse=True)
    winner = rows_sorted[0]
    Vwin = _v(winner.app_version)

    # If detected version is higher than current winner, upgrade winner in-place
    if Vnew > Vwin:
        winner.app_version = str(Vnew)
        if title_db_id is not None:
            winner.title_db_id = title_db_id
        db.session.flush()

    # Migrate files & overrides from losers → winner
    losers = rows_sorted[1:]
    for loser in losers:
        # move Files relationships
        for f in list(loser.files):
            if winner not in f.apps:
                f.apps.append(winner)
        # move overrides
        for ov in AppOverrides.query.filter_by(app_id=loser.id).all():
            ov.app_id = winner.id
        # delete loser
        db.session.delete(loser)

    # update owned based on files presence
    winner.owned = bool(getattr(winner, "files", []))
    db.session.flush()

    return winner, True

def _get_or_create_app(app_id: str, app_version, app_type: str, title_db_id: int):
    """
    Get or create an Apps row by (app_id, app_version).
    - app_version is normalized to str
    - If row exists, fill in missing fields (title_id/app_type) but DO NOT
      overwrite 'owned' or other set fields.
    Returns: (app, created_bool)
    """
    ver = str(app_version if app_version is not None else "0")
    app = Apps.query.filter_by(app_id=app_id, app_version=ver).first()
    if app:
        # Backfill minimal fields if missing
        if not app.title_id and title_db_id:
            app.title_id = title_db_id

        # Only fill in app_type if missing — warn if a conflicting one is detected
        if not app.app_type and app_type:
            app.app_type = app_type
        elif app.app_type and app_type and app.app_type != app_type:
            logger.warning(
                f"Conflicting app_type for app_id={app_id} v{ver}: "
                f"existing={app.app_type}, new={app_type}. Keeping existing value."
            )

        return app, False

    # No existing row — create a new one
    app = Apps(
        app_id=app_id,
        app_version=ver,
        app_type=app_type,
        owned=False,
        title_id=title_db_id
    )
    db.session.add(app)
    return app, True
