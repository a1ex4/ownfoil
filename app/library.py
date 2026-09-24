import os
import re
import shutil
from constants import *
from db import *
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
import media
import titles as titles_lib
import sys
from pathlib import Path
from utils import *
from db import update_file_path
import titledb.store
from titledb.schema import OVERRIDE_SOURCES, SOURCE_EXTRACT

def prepare_template_names(format_data, windows_compatible):
    """Sanitize the names before formatting, so they cannot introduce path separators, and cap their length."""
    names = {k: sanitize_filename(v, windows_compatible) for k, v in format_data.items() if k in TEMPLATE_NAME_KEYS}
    if sys.platform == 'win32' or windows_compatible:
        names = {k: trim_name(v, MAX_NAME_WINDOWS) for k, v in names.items()}

    return {**format_data, **names}

def organized_path(file_obj, library_path, organizer_settings):
    """Where the organizer templates place a file, None when it cannot be organized."""
    templates = organizer_settings['templates']

    # Get the associated app for the file
    app = file_obj.apps[0] if file_obj.apps else None
    if not app:
        logger.warning(f"No app associated with file {file_obj.filename}. Skipping organization.")
        return None

    template = _get_template_for_file(file_obj, app, templates)

    # Retrieve data for template formatting
    format_data = {}
    # Get title name from the associated title_id
    title_info = titles_lib.get_game_info(app.title.title_id)
    if title_info['name'] == 'Unrecognized':
        logger.warning(f"No title info associated with file {file_obj.filename}. Skipping organization.")
        return None
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
    return os.path.join(library_path, os.path.join(*safe_parts))

def organize_file(file_obj, library_path, organizer_settings):
    try:
        current_filepath = file_obj.filepath
        new_full_path = organized_path(file_obj, library_path, organizer_settings)
        if new_full_path is None:
            return

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

def files_with_free_base_name(organizer_settings):
    """Organized files kept off their template path by a "(n)" collision suffix, now that the path is free.

    The suffix pattern only preselects: a template can render a name ending in "(n)" itself, so the
    template path is what decides - else such a file would be released on every pass.
    """
    files = Files.query.filter(Files.organized.is_(True), Files.filename.like('%(%).%')).all()
    released = []
    for f in files:
        if not re.fullmatch(r'.*\(\d+\)\.[^.]+', f.filename):
            continue
        target = organized_path(f, get_library_path(f.library_id), organizer_settings)
        if target and target != f.filepath and not file_exists_in_db(target):
            released.append(f)
    return released

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

def outdated_update_groups():
    """Per title with several owned updates: its newest update's Apps.id and files, and the
    single-content files of its older updates."""
    groups = []
    for title in get_all_titles():
        updates = [a for a in title.apps if a.app_type == APP_TYPE_UPD]
        owned = [a for a in updates if a.owned]
        if len(owned) <= 1:
            continue
        latest = max(owned, key=lambda a: int(a.app_version))
        outdated = [f for a in updates if int(a.app_version) < int(latest.app_version)
                    for f in a.files if f.identified and not f.multicontent]
        if outdated:
            groups.append((latest.id, list(latest.files), sorted(outdated, key=lambda f: f.id)))
    return groups

def outdated_update_files():
    return [f for _, _, outdated in outdated_update_groups() for f in outdated]

def remove_outdated_update_files():
    logger.info("Starting removal of outdated update files...")
    try:
        for file_obj in outdated_update_files():
            logger.info(f"Removing outdated update file: {file_obj.filepath} - Greater owned version available.")
            delete_library_file(file_obj)
        logger.info(f"Finished removal of outdated update files.")
    except Exception as e:
        logger.error(f"Error during removal of outdated update files: {e}")

def delete_library_file(file_obj):
    """Delete a library file from disk and the database, unseen by the watcher."""
    if not os.path.exists(file_obj.filepath):
        logger.warning(f"Physical file not found for deletion: {file_obj.filepath}")
        return
    add_ignored_event(file_obj.filepath, '')
    try:
        os.remove(file_obj.filepath)
    except OSError as e:
        logger.error(f"Error deleting physical file {file_obj.filepath}: {e}")
        pop_ignored_event(src_path=file_obj.filepath, dest_path='')
        return
    delete_file_by_filepath(file_obj.filepath)

def duplicate_groups(prefer_multicontent, is_pending):
    """Per app losing a copy: its Apps.id, the copy kept, and its copies deleted - files whose
    every app has a better copy elsewhere.

    Skips apps with a copy still pending or identified from its filename only: such a file
    may carry more than the database knows, and deleting it would lose that.
    """
    copies = {}
    for app_id, file in db.session.query(app_files.c.app_id, Files).join(Files, Files.id == app_files.c.file_id):
        copies.setdefault(app_id, []).append(file)
    keep, judged = set(), {}
    for app_id, files in copies.items():
        best = best_file(files, prefer_multicontent)
        if (len(files) > 1 and all(f.identification_type == 'cnmt' and not is_pending(f) for f in files)
                and os.path.exists(best.filepath)):
            keep.add(best)
            judged[app_id] = best
        else:
            keep.update(files)
    groups = [(app_id, best, sorted(set(copies[app_id]) - keep, key=lambda f: f.id))
              for app_id, best in judged.items()]
    return [g for g in groups if g[2]]

def duplicate_files(prefer_multicontent, is_pending):
    """Files whose every app has a better copy elsewhere."""
    deleted = {f for _, _, files in duplicate_groups(prefer_multicontent, is_pending) for f in files}
    return sorted(deleted, key=lambda f: f.id)

def remove_duplicate_files(prefer_multicontent, is_pending):
    for file_obj in duplicate_files(prefer_multicontent, is_pending):
        logger.info(f"Removing duplicate file: {file_obj.filepath}")
        delete_library_file(file_obj)

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


def _media_files_in_use():
    """(kind, filename) of every stored file something still names.

    The artwork rows are not the whole answer: an extracted icon the titledb dump outranks
    loses its row but stays in the override that named it, and is served again the day the
    dump stops describing that title.
    """
    keep = get_media_files()
    for source in OVERRIDE_SOURCES:
        for row in list_title_overrides(source):
            for value in row.values():
                keep |= media.slots_in(value)
    return keep


def remove_orphan_media():
    """Drop the artwork and extracted metadata of ids the library no longer covers.

    A sweep rather than a hook on the title delete: media is keyed by the Switch id and a DLC
    is not a row in `titles` at all, so there is no cascade to hang one off - and a pass over
    the whole table also collects what a path that forgot to clean up left behind.
    """
    stored = {t.upper() for t in get_media_title_ids()}
    overrides = {r['id'] for r in list_title_overrides(SOURCE_EXTRACT)}
    covered = {t.title_id.upper() for t in get_all_titles() if t.title_id}
    covered |= {a.app_id.upper() for a in Apps.query.with_entities(Apps.app_id) if a.app_id}

    orphans = (stored | {o.upper() for o in overrides}) - covered
    if orphans:
        # A DLC is not a title, and only an owned one is an app of one, so cnmts is the only
        # thing that tells the artwork of an unowned DLC from a leftover.
        parents = titledb.store.get_dlc_base_titles(orphans)
        # None, not empty: titles.db unreadable means every DLC looks unclaimed.
        orphans = set() if parents is None else orphans - {
            dlc for dlc, base in parents.items() if base in covered}

    if orphans:
        rows = delete_media_for_titles(orphans)
        for title_id in overrides:
            if title_id.upper() in orphans:
                titledb.store.delete_override(title_id, SOURCE_EXTRACT)
        logger.info(
            f"Removed {rows} artwork row(s) of {len(orphans)} id(s) no longer in the library.")

    # One file backs every title using that image, so it is the whole table that retires one -
    # the same sweep, not a step of the deletion above. Runs even when no id was orphaned: a
    # slot that changed filename drops its old file without any title having gone anywhere.
    files = media.collect(_media_files_in_use())
    if files:
        logger.info(f"Removed {files} artwork file(s) nothing names any more.")
    return len(orphans)


def update_titles():
    """Batch: recompute all titles. Also removes titles with no owned apps, and their artwork."""
    titles_removed = remove_titles_without_owned_apps()
    if titles_removed > 0:
        logger.info(f"Removed {titles_removed} titles with no owned apps.")
    remove_orphan_media()

    for title in get_all_titles():
        update_title_flags(title.title_id)

