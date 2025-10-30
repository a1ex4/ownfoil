import datetime
import hashlib
import os
import shutil
import unicodedata
from collections import defaultdict
from typing import Dict, Optional

from sqlalchemy import or_

from cache import compute_library_apps_hash, is_library_snapshot_current
from constants import *
from db import *
from overrides import build_override_index
from settings import load_settings
import titles as titles_lib
from utils import *

def _normalize_sort_text(value):
    if value is None:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    stripped = "".join(ch for ch in text if not unicodedata.combining(ch))
    return stripped.casefold()

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
    _, files = titles_lib.get_dirs_and_files(library_path)

    filepaths_in_library = get_library_file_paths(library_id)
    new_files = [f for f in files if f not in filepaths_in_library]
    add_files_to_library(library_id, new_files)
    set_library_scan_time(library_id)

def get_files_to_identify(library_id, *, force_all: bool = False):
    q = (
        Files.query
        .filter(Files.library_id == library_id)
        .options(db.joinedload(Files.apps).joinedload(Apps.override))
    )

    if force_all:
        return q.order_by(Files.last_attempt.asc().nullsfirst()).all()

    staged = ("filename", "titles_lib", "not_in_titledb") # staged markers
    seven_days_ago = datetime.datetime.utcnow() - datetime.timedelta(days=7)

    q = q.filter(
        or_(
            Files.identified.is_(False), # not yet identified
            Files.identification_type.in_(staged), # staged by first pass
            Files.last_attempt.is_(None), # never attempted
            Files.last_attempt < seven_days_ago, # stale
        )
    )

    candidates = q.order_by(Files.last_attempt.asc().nullsfirst()).all()

    filtered = []
    for file in candidates:
        ident_type = (getattr(file, "identification_type", "") or "").lower()
        if ident_type == "not_in_titledb":
            app_types = {
                (getattr(app, "app_type", "") or "").upper()
                for app in getattr(file, "apps", [])
            }
            if APP_TYPE_UPD in app_types:
                continue
            has_override = any(
                getattr(app, "override", None)
                and getattr(app.override, "enabled", True)
                for app in getattr(file, "apps", [])
            )
            if has_override:
                continue
        filtered.append(file)

    return filtered

def identify_library_files(library):
    # Resolve library_id / path
    if isinstance(library, int) or (isinstance(library, str) and library.isdigit()):
        library_id = int(library)
        library_path = get_library_path(library_id)
    else:
        library_path = library
        library_id = get_library_id(library_path)

    files_to_identify = get_files_to_identify(library_id)
    not_in_titledb_count = sum(
        1 for f in files_to_identify
        if (getattr(f, "identification_type", "") or "").lower() == "not_in_titledb"
    )

    if not_in_titledb_count:
        logger.info(
            "Re-identifying %s not-in-TitleDB file(s) for library %s to refresh overrides.",
            not_in_titledb_count,
            library_path,
        )
    nb_to_identify = len(files_to_identify)

    # Load TitleDB once so we can check presence of title_ids quickly
    with titles_lib.titledb_session("identify_library_files"):
        name_lookup_cache: Dict[str, Optional[str]] = {}
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

                    file_basename = os.path.splitext(filename)[0]
                    clean_display_name = titles_lib.clean_display_name(file_basename)
                    normalized_display_name = titles_lib.normalize_display_name(file_basename)

                    nb_content = 0
                    auto_override_candidates = []
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

                        auto_override_candidates.append((app_row, file_content))
                        nb_content += 1

                    # Update multi-content flags
                    file.multicontent = nb_content > 1
                    file.nb_content = nb_content

                    for app_row, file_content in auto_override_candidates:
                        _auto_create_metadata_override(
                            app_row=app_row,
                            file_content=file_content,
                            clean_display_name=clean_display_name,
                            normalized_display_name=normalized_display_name,
                            name_lookup_cache=name_lookup_cache,
                        )

                    # Determine if any of this file's title_ids are unknown to TitleDB
                    needs_override = any(_title_metadata_missing(tid) for tid in title_ids)

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

    if not_in_titledb_count:
        logger.info(
            "Finished re-identifying %s not-in-TitleDB file(s) for library %s.",
            not_in_titledb_count,
            library_path,
        )

def _lookup_title_id_by_normalized_name(normalized_name: str, cache: Dict[str, Optional[str]]) -> Optional[str]:
    """
    Resolve a normalized display name to a Title ID using cached lookups.
    """
    if not normalized_name:
        return None
    key = normalized_name.strip().upper()
    if not key:
        return None
    if key in cache:
        return cache[key]
    match = titles_lib.find_title_id_by_normalized_name(key)
    cache[key] = match
    return match

def _title_metadata_missing(title_id: Optional[str]) -> bool:
    """
    True when TitleDB lacks a usable entry for the given Title ID.
    """
    tid = normalize_id(title_id, "title")
    if not tid:
        return True
    if not titles_lib.title_id_exists(tid):
        return True
    info = titles_lib.get_game_info(tid) or {}
    existing_name = (info.get("name") or "").strip().lower()
    return not existing_name or existing_name in {"unrecognized", "unidentified"}

def _auto_create_metadata_override(
    *,
    app_row,
    file_content: dict,
    clean_display_name: str,
    normalized_display_name: str,
    name_lookup_cache: Dict[str, Optional[str]],
) -> None:
    """
    Ensure an override exists when TitleDB lacks the detected Title ID.
    """
    if not app_row or not getattr(app_row, "id", None):
        return

    # Skip non-base/DLC entries; overrides are tied to those families.
    if getattr(app_row, "app_type", None) not in (APP_TYPE_BASE, APP_TYPE_DLC):
        return

    if AppOverrides.query.filter_by(app_fk=app_row.id).first():
        return

    title_id = normalize_id(file_content.get("title_id"), "title")
    if not title_id:
        return

    if not _title_metadata_missing(title_id):
        return

    corrected_title_id = _lookup_title_id_by_normalized_name(normalized_display_name, name_lookup_cache)
    if corrected_title_id:
        corrected_title_id = normalize_id(corrected_title_id, "title")
        if not corrected_title_id or corrected_title_id == title_id:
            return
        _create_override(app_row, corrected_title_id=corrected_title_id)
        logger.info(
            "Auto-created redirect override for app %s → TitleID %s",
            app_row.app_id,
            corrected_title_id,
        )
        return

    clean_name = clean_display_name.strip() if clean_display_name else ""
    if not clean_name:
        return

    _create_override(app_row, name=clean_name)
    logger.info(
        "Auto-created name override for app %s with name '%s'",
        app_row.app_id,
        clean_name,
    )

def _create_override(app_row, *, corrected_title_id: Optional[str] = None, name: Optional[str] = None) -> None:
    """
    Persist a new AppOverrides row attached to the provided app.
    """
    ov = AppOverrides(app=app_row)
    ov.enabled = True
    ov.created_at = datetime.datetime.utcnow()
    ov.updated_at = datetime.datetime.utcnow()
    if corrected_title_id:
        ov.corrected_title_id = corrected_title_id
    elif name:
        ov.name = name
    db.session.add(ov)

def add_missing_apps_to_db():
    logger.info('Adding missing apps to database...')
    apps_added = 0
    commit_every = 250

    def _chunked(sequence, size):
        for idx in range(0, len(sequence), size):
            yield sequence[idx:idx + size]

    def _map_title_ids(title_ids):
        if not title_ids:
            return {}
        mapping = {}
        normalized = [tid.upper() for tid in set(title_ids) if isinstance(tid, str)]
        chunk_size = 900  # stay below sqlite variable limit
        for chunk in _chunked(normalized, chunk_size):
            rows = (
                db.session.query(Titles.title_id, Titles.id)
                .filter(Titles.title_id.in_(chunk))
                .all()
            )
            for title_id, db_id in rows:
                if title_id:
                    mapping[title_id.upper()] = db_id
        return mapping

    def _ensure_missing_base_apps():
        added = 0
        pending_commits = 0
        missing_base_rows = (
            db.session.query(Titles.id, Titles.title_id)
            .filter(~Titles.apps.any(Apps.app_type == APP_TYPE_BASE))
            .all()
        )
        for title_db_id, title_id in missing_base_rows:
            if not title_id:
                continue
            _, created = _get_or_create_app(
                app_id=title_id.upper(),
                app_version="0",
                app_type=APP_TYPE_BASE,
                title_db_id=title_db_id
            )
            if created:
                added += 1
                pending_commits += 1
                logger.debug(f'Added missing base app placeholder v0: {title_id}')
                if pending_commits >= commit_every:
                    db.session.commit()
                    pending_commits = 0
        if pending_commits:
            db.session.commit()
        return added

    def _ensure_missing_update_apps():
        versions_db = getattr(titles_lib, "_versions_db", {}) or {}
        if not versions_db:
            return 0

        title_map = _map_title_ids([tid.upper() for tid in versions_db.keys()])
        if not title_map:
            return 0

        existing_updates = defaultdict(set)
        for app_id, app_version in (
            db.session.query(Apps.app_id, Apps.app_version)
            .filter(Apps.app_type == APP_TYPE_UPD)
        ):
            if app_id:
                existing_updates[app_id.upper()].add(str(app_version))

        added = 0
        pending_commits = 0
        for title_lower, version_entries in versions_db.items():
            title_id = title_lower.upper()
            if len(title_id) != 16:
                continue
            title_db_id = title_map.get(title_id)
            if not title_db_id:
                continue

            update_app_id = (title_id[:-3] + '800').upper()
            known_versions = existing_updates.setdefault(update_app_id, set())

            for version_key in version_entries.keys():
                version_str = str(version_key)
                if version_str in known_versions:
                    continue
                _, created = _get_or_create_app(
                    app_id=update_app_id,
                    app_version=version_str,
                    app_type=APP_TYPE_UPD,
                    title_db_id=title_db_id
                )
                if created:
                    known_versions.add(version_str)
                    added += 1
                    pending_commits += 1
                    logger.debug(f'Added missing update app: {update_app_id} v{version_str}')
                    if pending_commits >= commit_every:
                        db.session.commit()
                        pending_commits = 0
        if pending_commits:
            db.session.commit()
        return added

    def _ensure_missing_dlc_apps():
        cnmts_db = getattr(titles_lib, "_cnmts_db", {}) or {}
        if not cnmts_db:
            return 0

        dlc_index = defaultdict(lambda: defaultdict(set))
        for app_id_lower, version_map in cnmts_db.items():
            app_id = app_id_lower.upper()
            for version_key, metadata in version_map.items():
                if not isinstance(metadata, dict):
                    continue
                if metadata.get('titleType') != 130:
                    continue
                base_tid = metadata.get('otherApplicationId')
                if base_tid:
                    base_tid = base_tid.upper()
                else:
                    try:
                        base_tid = titles_lib.get_title_id_from_app_id(app_id, APP_TYPE_DLC)
                    except Exception:
                        base_tid = None
                    if base_tid:
                        base_tid = base_tid.upper()
                if not base_tid or len(base_tid) != 16:
                    continue
                dlc_index[base_tid][app_id].add(str(version_key))

        if not dlc_index:
            return 0

        title_map = _map_title_ids(dlc_index.keys())
        if not title_map:
            return 0

        existing_dlcs = defaultdict(set)
        for app_id, app_version in (
            db.session.query(Apps.app_id, Apps.app_version)
            .filter(Apps.app_type == APP_TYPE_DLC)
        ):
            if app_id:
                existing_dlcs[app_id.upper()].add(str(app_version))

        added = 0
        pending_commits = 0
        for base_title_id, dlc_apps in dlc_index.items():
            title_db_id = title_map.get(base_title_id)
            if not title_db_id:
                continue
            for dlc_app_id, versions in dlc_apps.items():
                dlc_app_id_upper = dlc_app_id.upper()
                known_versions = existing_dlcs.setdefault(dlc_app_id_upper, set())
                for version_str in versions:
                    version_str = str(version_str)
                    if version_str in known_versions:
                        continue
                    _, created = _get_or_create_app(
                        app_id=dlc_app_id_upper,
                        app_version=version_str,
                        app_type=APP_TYPE_DLC,
                        title_db_id=title_db_id
                    )
                    if created:
                        known_versions.add(version_str)
                        added += 1
                        pending_commits += 1
                        logger.debug(f'Added missing DLC app: {dlc_app_id_upper} v{version_str}')
                        if pending_commits >= commit_every:
                            db.session.commit()
                            pending_commits = 0
        if pending_commits:
            db.session.commit()
        return added

    with titles_lib.titledb_session("add_missing_apps_to_db"):
        apps_added += _ensure_missing_base_apps()
        apps_added += _ensure_missing_update_apps()
        apps_added += _ensure_missing_dlc_apps()
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

def generate_library_snapshot():
    """
    Public entry-point for routes:
    - Load library from disk if unchanged,
    - Add a strong ETag derived from the snapshot's content identifiers (apps hash + TitleDB commit),
    - Return the list used by the API layer.
    """
    # Load library from disk or regenerate if hash changed
    saved = load_or_generate_library_snapshot()  # {'hash': ..., 'titledb_commit': ..., 'library': [...]}
    if not saved:
        empty_etag = hashlib.sha256(b":").hexdigest()
        return [], empty_etag

    library = saved.get('library') or []
    payload_hash = saved.get('hash') or ""
    titledb_commit = saved.get('titledb_commit') or ""
    etag_source = f"{payload_hash}:{titledb_commit}".encode("utf-8")
    etag = hashlib.sha256(etag_source).hexdigest()
    return library, etag

def load_or_generate_library_snapshot():
    """
    Load the BASE library (no overrides) from disk if hash unchanged.
    Otherwise, regenerate and save.
    """
    saved = load_json(LIBRARY_CACHE_FILE)
    if saved and is_library_snapshot_current(saved):
        return saved

    # Hash changed or cache missing/corrupt -> regenerate
    return _generate_library_snapshot()

def _generate_library_snapshot():
    """Generate the BASE/DLC library from Apps table and cache to disk."""
    logger.info('Generating library snapshot...')

    with titles_lib.titledb_session("generate_library"):
        titles = get_all_apps(include_files=True)
        games_info = []
        processed_dlc_apps = set()  # Track processed DLC app_ids to avoid duplicates

        for title in titles:
            has_none_value = any(value is None for value in title.values())
            if has_none_value:
                logger.warning(f'File contains None value, it will be skipped: {title}')
                continue
            if title['app_type'] == APP_TYPE_UPD:
                continue

            # Use DLC app_id for DLC metadata; BASE keeps family/base title_id.
            lookup_id = title['app_id'] if title['app_type'] == APP_TYPE_DLC else title['title_id']
            info_from_titledb = titles_lib.get_game_info(lookup_id)
            if info_from_titledb is None:
                logger.warning(f'Info not found for game: {title}')
                continue
            metadata_missing = _title_metadata_missing(lookup_id)

            title.update(info_from_titledb)
            title['has_title_db'] = not metadata_missing
            title['is_unrecognized'] = metadata_missing

            # Stable sort/display key:
            # - BASE: use its own (family) name
            # - DLC : use the family/base title name (so DLCs sort alongside their bases)
            if title['app_type'] == APP_TYPE_DLC:
                family_info = titles_lib.get_game_info(title['title_id'])  # family/base lookup
                if family_info:
                    # Use the family's artwork when this DLC lacks its own assets so cards don’t show the gray placeholder.
                    if not (title.get("bannerUrl") or title.get("banner_path")):
                        fallback_banner = family_info.get("bannerUrl")
                        if fallback_banner:
                            title["bannerUrl"] = fallback_banner
                    if not (title.get("iconUrl") or title.get("icon_path")):
                        fallback_icon = family_info.get("iconUrl")
                        if fallback_icon:
                            title["iconUrl"] = fallback_icon
                family_name = (family_info or {}).get('name') or title.get('name')
                title['title_id_name'] = family_name or 'Unrecognized'
            else:
                title['title_id_name'] = title.get('name') or 'Unrecognized'

            if title['app_type'] == APP_TYPE_BASE:
                # Status flags from Titles table (computed by update_titles)
                title_obj = get_title(title['title_id'])
                if title_obj:
                    title['has_base'] = title_obj.have_base
                    # Only mark as up to date if the base itself is owned and up_to_date
                    title['has_latest_version'] = (title_obj.have_base and title_obj.up_to_date)
                    title['has_all_dlcs'] = title_obj.complete
                else:
                    title['has_base'] = False
                    title['has_latest_version'] = False
                    title['has_all_dlcs'] = False

                # Version list for BASE using Apps + versions DB release dates
                title_apps = get_all_title_apps(title['title_id'])
                update_apps = [a for a in title_apps if a.get('app_type') == APP_TYPE_UPD]

                available_versions = titles_lib.get_all_existing_versions(title['title_id'])
                version_release_dates = {v['version']: v['release_date'] for v in available_versions}

                version_list = []
                for update_app in update_apps:
                    app_version = int(update_app['app_version'])
                    rd = version_release_dates.get(app_version)
                    version_list.append({
                        'version': app_version,
                        'owned': update_app.get('owned', False),
                        'release_date': rd.isoformat() if isinstance(rd, (datetime.datetime, datetime.date)) else rd
                    })

                title['version'] = sorted(version_list, key=lambda x: x['version'])

            elif title['app_type'] == APP_TYPE_DLC:
                # Skip if we've already processed this DLC app_id
                app_id = title['app_id']
                if app_id in processed_dlc_apps:
                    continue
                processed_dlc_apps.add(app_id)

                # Get all versions for this DLC app_id
                title_apps = get_all_title_apps(title['title_id'])
                dlc_apps = [app for app in title_apps if app.get('app_type') == APP_TYPE_DLC and app['app_id'] == app_id]

                # Create version list for this DLC
                version_list = []
                for dlc_app in dlc_apps:
                    version_list.append({
                        'version': int(dlc_app['app_version']),
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
                    title['has_latest_version'] = len(owned_versions) > 0 and max(owned_versions) >= highest_version
                else:
                    # No local rows → nothing to update
                    title['has_latest_version'] = True

            # File basename hint for organizer/UI
            title['file_basename'] = _best_file_basename(title.get('files'))
            # We don't need to send the full files payload to the client cache, and it can carry datetimes which are not serializable
            title.pop('files', None)

            games_info.append(title)

        _add_files_without_apps(games_info)

        def _normalized_id(value, kind: str) -> str:
            if not isinstance(value, str):
                return ""
            trimmed = value.strip()
            if not trimmed:
                return ""
            try:
                normalized = normalize_id(trimmed, kind)
            except Exception:
                normalized = None
            if normalized:
                return normalized.upper()
            return trimmed.upper()

        override_index = build_override_index(include_disabled=False) or {}
        raw_overrides = override_index.get("by_app") if isinstance(override_index, dict) else {}
        overrides_by_app: dict[str, dict] = {}
        if isinstance(raw_overrides, dict):
            for raw_app_id, payload in raw_overrides.items():
                if not isinstance(payload, dict):
                    continue
                normalized_app_id = _normalized_id(raw_app_id, "app")
                if normalized_app_id:
                    overrides_by_app[normalized_app_id] = payload

        def _first_nonempty(*values):
            for val in values:
                if isinstance(val, str):
                    trimmed = val.strip()
                    if trimmed:
                        return trimmed
            return None

        def _override_for_app(app_id):
            key = _normalized_id(app_id, "app")
            return overrides_by_app.get(key)

        base_sort_name_by_id: dict[str, str] = {}
        for game in games_info:
            app_type = (game.get('app_type') or '').upper()
            if app_type != APP_TYPE_BASE:
                continue

            override = _override_for_app(game.get('app_id'))
            override_name = _first_nonempty(override.get('name')) if override else None
            display_name = _first_nonempty(
                override_name,
                game.get('title_id_name'),
                game.get('name')
            ) or 'Unrecognized'

            for candidate in (
                _normalized_id(game.get('title_id'), "title"),
                _normalized_id(game.get('app_id'), "app"),
                _normalized_id(game.get('corrected_title_id'), "title")
            ):
                if candidate:
                    base_sort_name_by_id[candidate] = display_name

            if override:
                corrected = _normalized_id(override.get('corrected_title_id'), "title")
                if corrected:
                    base_sort_name_by_id[corrected] = display_name

        def _compute_sort_tuple(game: dict) -> tuple[str, str, int, str, str]:
            app_type = (game.get('app_type') or '').upper()
            app_id_norm = _normalized_id(game.get('app_id'), "app")
            title_id_norm = _normalized_id(game.get('title_id'), "title")
            override = overrides_by_app.get(app_id_norm)
            override_name = _first_nonempty(override.get('name')) if override else None

            if app_type == APP_TYPE_DLC:
                base_sort_name = base_sort_name_by_id.get(title_id_norm)
                if not base_sort_name and override:
                    corrected = _normalized_id(override.get('corrected_title_id'), "title")
                    if corrected:
                        base_sort_name = base_sort_name_by_id.get(corrected)
                if not base_sort_name:
                    corrected = _normalized_id(game.get('corrected_title_id'), "title")
                    if corrected:
                        base_sort_name = base_sort_name_by_id.get(corrected)

                sort_name = _first_nonempty(
                    base_sort_name,
                    game.get('title_id_name'),
                    override_name,
                    game.get('name')
                ) or 'Unrecognized'
                base_key = title_id_norm or app_id_norm
                sort_kind = 1
            elif app_type == APP_TYPE_BASE:
                sort_name = base_sort_name_by_id.get(title_id_norm) or (
                    _first_nonempty(
                        override_name,
                        game.get('title_id_name'),
                        game.get('name')
                    ) or 'Unrecognized'
                )
                base_key = title_id_norm or app_id_norm
                sort_kind = 0
            else:
                sort_name = _first_nonempty(
                    override_name,
                    game.get('title_id_name'),
                    game.get('name')
                ) or 'Unrecognized'
                base_key = title_id_norm or app_id_norm
                sort_kind = 2

            fallback = game.get('file_basename') or ''
            return sort_name, base_key, sort_kind, app_id_norm, fallback

        def _library_sort_key(record: dict) -> tuple:
            sort_name, base_key, sort_kind, app_id_norm, fallback = _compute_sort_tuple(record)
            fallback_key = fallback.upper() if isinstance(fallback, str) else ''
            return (
                0 if sort_name else 1,
                _normalize_sort_text(sort_name),
                base_key,
                sort_kind,
                app_id_norm,
                fallback_key,
            )

        sorted_games = sorted(games_info, key=_library_sort_key)

        library_data = {
            'hash': compute_library_apps_hash(),
            'titledb_commit': titles_lib.get_titledb_commit_hash() or "",
            'snapshot_version': LIBRARY_SNAPSHOT_VERSION,
            'library': sorted_games
        }

        # Persist snapshot to disk
        save_json(library_data, LIBRARY_CACHE_FILE, default=_json_default)
        logger.info('Generating library snapshot done.')
        return library_data

def _best_file_basename(files):
    if not files:
        return None

    ext_rank = {".nsz": 5, ".xcz": 5, ".nsp": 4, ".xci": 4, ".zip": 2, ".rar": 1}
    strong_id = {"cnmt", "tik", "cert"}

    def _ext_score(name):
        _, ext = os.path.splitext((name or "").lower())
        return ext_rank.get(ext, 0)

    def score(f):
        name_for_ext = f.get("filename") or os.path.basename(f.get("filepath") or "")
        return (
            1 if f.get("identification_type") in strong_id else 0,            # strong ID first
            _ext_score(name_for_ext),                                         # prefer compressed
            f.get("last_attempt") or datetime.datetime.min,                   # recent work
            f.get("created_at") or datetime.datetime.min,                     # stable tiebreaker
            f.get("id") or 0,                                                 # deterministic
        )

    best = max(files, key=score)
    if best.get("filename"):
        return best["filename"]
    return os.path.basename(best.get("filepath") or "") or None

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

def _ensure_single_latest_base(app_id: str, detected_version, title_db_id=None):
    """
    Guarantee a single BASE row for app_id holding the HIGHEST version.
    - If none exists, create one at detected_version.
    - If one exists at lower version, upgrade that row (in-place) to detected_version.
    - If multiple exist, pick highest as winner, migrate files & overrides to it, delete losers.
    Returns: (winner_app, created_or_upgraded: bool)
    """
    Vnew = _normalize_version_int(detected_version)

    # If we weren't handed a Titles FK, resolve it from app_id (BASE app_id == TitleID)
    if title_db_id is None:
        try:
            add_title_id_in_db(app_id)                 # idempotent (ensures Titles row exists)
            title_db_id = get_title_id_db_id(app_id)   # integer FK (or None if something’s off)
        except Exception:
            title_db_id = None  # fail-soft; we’ll still create/upgrade the app row

    rows = Apps.query.filter_by(app_id=app_id, app_type=APP_TYPE_BASE).all()

    if not rows:
        # Create brand-new BASE
        winner, _ = _get_or_create_app(
            app_id=app_id,
            app_version=str(Vnew),
            app_type=APP_TYPE_BASE,
            title_db_id=title_db_id
        )
        # ensure FK is correct if resolver succeeded
        if title_db_id is not None:
            winner.title_id = title_db_id
        # sanity: make sure version is exactly Vnew
        winner.app_version = str(Vnew)
        db.session.flush()
        return winner, True

    # If exactly one exists, upgrade in-place if needed
    if len(rows) == 1:
        winner = rows[0]
        Vold = _normalize_version_int(winner.app_version)
        if Vnew > Vold:
            winner.app_version = str(Vnew)
            if title_db_id is not None:
                winner.title_id = title_db_id
            db.session.flush()
            return winner, True
        else:
            return winner, False

    # Multiple exist → collapse
    rows_sorted = sorted(rows, key=lambda r: _normalize_version_int(r.app_version), reverse=True)
    winner = rows_sorted[0]
    Vwin = _normalize_version_int(winner.app_version)

    # If detected version is higher than current winner, upgrade winner in-place
    if Vnew > Vwin:
        winner.app_version = str(Vnew)
        if title_db_id is not None:
            winner.title_id = title_db_id
        db.session.flush()

    # Migrate files & overrides from losers → winner
    losers = rows_sorted[1:]
    for loser in losers:
        # move Files relationships
        for f in list(loser.files):
            if winner not in f.apps:
                f.apps.append(winner)
        # move overrides
        for ov in AppOverrides.query.filter_by(app_fk=loser.id).all():
            ov.app_fk = winner.id
        # delete loser
        db.session.delete(loser)

    # update owned based on files presence
    winner.owned = bool(getattr(winner, "files", []))
    db.session.flush()

    return winner, True

def _get_or_create_app(app_id: str, app_version, app_type: str, title_db_id: Optional[int] = None):
    """
    Get or create an Apps row by (app_id, app_version).
    - app_version is normalized to str
    - If row exists, fill in missing fields (title_id/app_type) but DO NOT
      overwrite 'owned' or other set fields.
    Returns: (app, created_bool)
    """
    ver = str(app_version if app_version is not None else "0")

    # Defensive: resolve family/base Titles FK if missing
    if not title_db_id:
        base_tid = None
        if isinstance(app_id, str):
            try:
                if app_type == APP_TYPE_BASE:
                    base_tid = app_id
                elif app_type in (APP_TYPE_UPD, APP_TYPE_DLC) and len(app_id) >= 16:
                    base_tid = titles_lib.get_title_id_from_app_id(app_id, app_type)
            except Exception:
                base_tid = None

        base_tid = normalize_id(base_tid, "title") if base_tid else None
        if base_tid:
            try:
                add_title_id_in_db(base_tid)  # idempotent
            except Exception as exc:
                logger.debug("Failed to ensure Title row for %s: %s", base_tid, exc)
            title_db_id = get_title_id_db_id(base_tid)

    if not title_db_id:
        # We *cannot* create a new Apps row without a Titles FK (nullable=False).
        # Check if a row already exists; if not, log and bail clearly.
        existing = Apps.query.filter_by(app_id=app_id, app_version=ver).first()
        if existing:
            return existing, False

        logger.warning(f"Cannot resolve Titles FK for app_id={app_id} v{ver} ({app_type}); skipping create.")
        # Returning the non-existent row would break callers; raise clearly instead.
        raise RuntimeError(f"Missing Titles FK for app_id={app_id} v{ver} ({app_type})")

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

def _normalize_version_int(s) -> int:
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

def _json_default(o):
    if isinstance(o, (datetime.datetime, datetime.date)):
        return o.isoformat()
    # Let json raise for anything else non-serializable so we don’t hide bugs
    raise TypeError(f'Object of type {o.__class__.__name__} is not JSON serializable')
