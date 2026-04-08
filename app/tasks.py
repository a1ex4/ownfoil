"""Task queue model, registry, and helpers."""
import hashlib
import json
import datetime
import functools
import logging
import os
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
import titles as titles_lib
import titledb
from db import (
    db, Task, Files, Apps, get_library_id, get_library_path, get_library_file_paths,
    get_libraries, add_title_id_in_db, get_title_id_db_id, add_file_to_app,
    file_exists_in_db, update_file_path, delete_file_by_filepath,
    set_library_scan_time, remove_missing_files_from_db,
)
from settings import get_settings
from utils import interval_string_to_timedelta, delete_empty_folders
from library import (
    get_files_to_identify, add_missing_apps_for_title, update_title_flags,
    add_missing_apps_to_db, update_titles, organize_file,
    remove_outdated_update_files, generate_library,
)

logger = logging.getLogger('main')

# --- Task Registry ---
TASK_REGISTRY = {}
TASK_CONTINUATIONS = {}


def register_task(name):
    """Decorator to register a callable as a named task."""
    def decorator(func):
        TASK_REGISTRY[name] = func
        return func
    return decorator


def register_continuation(task_name):
    """Register a function to call when all children of a parent task complete."""
    def decorator(func):
        TASK_CONTINUATIONS[task_name] = func
        return func
    return decorator


def get_registered_task(name):
    return TASK_REGISTRY.get(name)


# --- Progress ---
_current_task_id = None

# --- Child task helpers ---
def create_child_task(parent_id, task_name, input_data=None):
    """Create a child task, deduped against existing active children of the same parent."""
    if task_name not in TASK_REGISTRY:
        raise ValueError(f"Unknown task: {task_name}")
    input_data = input_data or {}
    input_json = json.dumps(input_data, sort_keys=True)
    input_hash = compute_input_hash(input_data)
    now = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')

    connection = db.engine.raw_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute(
            "SELECT id FROM tasks WHERE parent_id = ? AND task_name = ? AND input_hash = ? "
            "AND status IN ('pending', 'running', 'waiting_for_children', 'completed') LIMIT 1",
            (parent_id, task_name, input_hash)
        )
        row = cursor.fetchone()
        if row:
            connection.commit()
            return row[0]
        cursor.execute(
            "INSERT INTO tasks (parent_id, task_name, status, completion_pct, input_json, input_hash, created_at) "
            "VALUES (?, ?, 'pending', 0, ?, ?, ?)",
            (parent_id, task_name, input_json, input_hash, now)
        )
        child_id = cursor.lastrowid
        connection.commit()
        return child_id
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def enqueue_or_child(task_name, input_data=None):
    """Create as child of the running task, or top-level if called outside a task."""
    if _current_task_id is not None:
        return create_child_task(_current_task_id, task_name, input_data)
    return enqueue_task(task_name, input_data)[0].id


def set_waiting_for_children():
    """Mark the current task as waiting for its children to complete."""
    task = db.session.get(Task, _current_task_id)
    task.status = 'waiting_for_children'
    db.session.commit()


def on_task_completed(task_id, parent_id):
    """Called by the worker after any task completes. Updates parent progress and checks for completion."""
    if not parent_id:
        return
    _try_complete_parent(parent_id)


def _try_complete_parent(parent_id):
    """Atomically update parent progress and complete if all children are done."""
    connection = db.engine.raw_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("BEGIN IMMEDIATE")

        cursor.execute("SELECT status, task_name, input_json FROM tasks WHERE id = ?", (parent_id,))
        row = cursor.fetchone()
        if not row or row[0] != 'waiting_for_children':
            connection.commit()
            return

        # Count children atomically under the lock
        cursor.execute("SELECT COUNT(*) FROM tasks WHERE parent_id = ?", (parent_id,))
        total = cursor.fetchone()[0]
        cursor.execute(
            "SELECT COUNT(*) FROM tasks WHERE parent_id = ? AND status IN ('completed', 'failed')",
            (parent_id,)
        )
        done = cursor.fetchone()[0]
        pct = int(done * 100 / total) if total else 0

        if done < total:
            cursor.execute("UPDATE tasks SET completion_pct = ? WHERE id = ?", (pct, parent_id))
            connection.commit()
            return

        # All children done — mark parent complete
        now = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute(
            "UPDATE tasks SET status = 'completed', completion_pct = 100, exit_code = 0, completed_at = ? WHERE id = ?",
            (now, parent_id)
        )
        connection.commit()

        # Run continuation outside the transaction
        task_name = row[1]
        continuation = TASK_CONTINUATIONS.get(task_name)
        if continuation:
            input_data = json.loads(row[2])
            continuation(**input_data)

        # Delete parent and its children
        Task.query.filter_by(parent_id=parent_id).delete()
        Task.query.filter_by(id=parent_id).delete()
        db.session.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


# --- Startup cleanup ---

def cleanup_tasks():
    """Startup cleanup: remove completed/scheduled tasks and fail stale running tasks."""
    # Remove completed tasks
    Task.query.filter_by(status='completed').delete()

    # Remove pending scheduled tasks — they'll be re-enqueued by init()
    Task.query.filter(Task.status == 'pending', Task.run_after.isnot(None)).delete()

    # Mark running/waiting tasks as failed — they can't survive a restart
    stale = Task.query.filter(Task.status.in_(['running', 'waiting_for_children'])).all()
    for task in stale:
        task.status = 'failed'
        task.error_message = 'Interrupted by application restart'
        task.exit_code = 1
        task.completed_at = datetime.datetime.utcnow()
        logger.info(f"Reset stale task {task.id} ({task.task_name})")

    db.session.commit()


# --- Helpers ---

def compute_input_hash(input_data):
    canonical = json.dumps(input_data, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def enqueue_task(task_name, input_data=None, run_after=None):
    """Enqueue a task. Returns (task, created) — created is False if a duplicate exists."""
    if task_name not in TASK_REGISTRY:
        raise ValueError(f"Unknown task: {task_name}")

    input_data = input_data or {}
    input_hash = compute_input_hash(input_data)
    input_json = json.dumps(input_data, sort_keys=True)

    # Scheduled tasks only dedup against pending; immediate tasks dedup against running too
    if run_after:
        dedup_statuses = "('pending', 'waiting_for_children')"
    else:
        dedup_statuses = "('pending', 'running', 'waiting_for_children')"

    connection = db.engine.raw_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("BEGIN IMMEDIATE")

        cursor.execute(
            f"SELECT id FROM tasks WHERE task_name = ? AND input_hash = ? AND status IN {dedup_statuses}",
            (task_name, input_hash)
        )
        existing = cursor.fetchone()

        if existing:
            connection.commit()
            task = db.session.get(Task, existing[0])
            logger.debug(f"Task '{task_name}' already exists (id={existing[0]}), skipping")
            return task, False

        now = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        run_after_str = run_after.strftime('%Y-%m-%d %H:%M:%S') if run_after else None
        cursor.execute(
            "INSERT INTO tasks (task_name, status, completion_pct, input_json, input_hash, run_after, created_at) "
            "VALUES (?, 'pending', 0, ?, ?, ?, ?)",
            (task_name, input_json, input_hash, run_after_str, now)
        )
        new_id = cursor.lastrowid
        connection.commit()

        if run_after:
            local_run_after = run_after + (datetime.datetime.now() - datetime.datetime.utcnow())
            schedule_info = f", run_after={local_run_after.strftime('%Y-%m-%d %H:%M:%S')}"
        else:
            schedule_info = ""
        logger.debug(f"Enqueued task '{task_name}' (id={new_id}{schedule_info})")
        task = db.session.get(Task, new_id)
        return task, True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def update_scheduled_task(task_name, run_after):
    """Update run_after on a pending scheduled task, delete if None, or create if missing."""
    connection = db.engine.raw_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        if run_after is None:
            cursor.execute(
                "DELETE FROM tasks WHERE task_name = ? AND status = 'pending' AND run_after IS NOT NULL",
                (task_name,)
            )
            logger.debug(f"Deleted scheduled task '{task_name}' (disabled)")
        else:
            cursor.execute(
                "UPDATE tasks SET run_after = ? WHERE task_name = ? AND status = 'pending' AND run_after IS NOT NULL",
                (run_after.strftime('%Y-%m-%d %H:%M:%S'), task_name)
            )
            if cursor.rowcount == 0:
                # No existing scheduled task — create one
                now = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
                input_hash = compute_input_hash({})
                cursor.execute(
                    "INSERT INTO tasks (task_name, status, completion_pct, input_json, input_hash, run_after, created_at) "
                    "VALUES (?, 'pending', 0, '{}', ?, ?, ?)",
                    (task_name, input_hash, run_after.strftime('%Y-%m-%d %H:%M:%S'), now)
                )
                local_ra = run_after + (datetime.datetime.now() - datetime.datetime.utcnow())
                logger.debug(f"Created scheduled task '{task_name}' run_after={local_ra.strftime('%Y-%m-%d %H:%M:%S')}")
            else:
                local_ra = run_after + (datetime.datetime.now() - datetime.datetime.utcnow())
                # logger.debug(f"Updated scheduled task '{task_name}' run_after={local_ra.strftime('%Y-%m-%d %H:%M:%S')}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_task(task_id):
    return db.session.get(Task, task_id)



# --- Titledb helper for tasks ---

def _schedules_generate_library(func):
    """Decorator: after func runs, (re)schedule generate_library with a debounce."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        result = func(*args, **kwargs)
        update_scheduled_task('generate_library', datetime.datetime.utcnow() + datetime.timedelta(seconds=5))
        return result
    return wrapper


def _with_titledb(func):
    """Decorator: load titledb before func runs, release after."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        titles_lib.load_titledb()
        try:
            return func(*args, **kwargs)
        finally:
            titles_lib.identification_in_progress_count -= 1
            titles_lib.unload_titledb()
    return wrapper


# --- Pipeline tasks ---

@register_task('update_titledb')
def update_titledb_task(**kwargs):
    settings = get_settings()
    titledb.update_titledb(settings)
    for lib in get_libraries():
        enqueue_task('scan_library', {'library_path': lib.path})
    # After titledb update, existing titles may have new DLC/update versions
    enqueue_task('add_missing_apps')
    # Re-enqueue for next scheduled run
    interval_str = settings.get('scheduler', {}).get('scan_interval', '12h')
    delta = interval_string_to_timedelta(interval_str)
    if delta:
        enqueue_task('update_titledb', run_after=datetime.datetime.utcnow() + delta)


@register_task('scan_library')
def scan_library_task(library_path, **kwargs):
    """Scan a library path for new files, creating a child task per file."""
    library_id = get_library_id(library_path)
    if not os.path.isdir(library_path):
        logger.warning(f'Library path {library_path} does not exist.')
        return

    logger.info(f'Scanning library path {library_path} ...')
    _, files = titles_lib.getDirsAndFiles(library_path)
    filepaths_in_db = get_library_file_paths(library_id)
    new_files = [f for f in files if f not in filepaths_in_db]

    if not new_files:
        logger.info(f'No new files found in {library_path}.')
        _scan_library_done(library_path=library_path)
        return

    for filepath in new_files:
        enqueue_or_child('add_file', {'library_path': library_path, 'filepath': filepath})
    set_waiting_for_children()


@register_continuation('scan_library')
def _scan_library_done(library_path, **kwargs):
    set_library_scan_time(get_library_id(library_path))
    enqueue_task('remove_missing_files')


@register_task('add_file')
@_schedules_generate_library
def add_file_task(library_path, filepath, **kwargs):
    """Add a single file to the library DB."""
    library_id = get_library_id(library_path)
    # Check if already in DB
    if filepath in get_library_file_paths(library_id):
        return

    file_display = filepath.replace(library_path, "")
    logger.info(f'Getting file info: {file_display}')
    file_info = titles_lib.get_file_info(filepath)
    if file_info is None:
        raise ValueError(f'Failed to get info for file: {file_display}')

    new_file = Files(
        filepath=filepath,
        library_id=library_id,
        folder=file_info["filedir"],
        filename=file_info["filename"],
        extension=file_info["extension"],
        size=file_info["size"],
    )
    db.session.add(new_file)
    db.session.commit()

    enqueue_or_child('identify_file', {'filepath': filepath, 'file_id': new_file.id})
    if _current_task_id is not None:
        set_waiting_for_children()


@register_task('identify_library')
def identify_library_task(**kwargs):
    """Identify all unidentified files across every library."""
    logger.info("Starting library identification process ...")
    files_to_identify = [f for lib in get_libraries() for f in get_files_to_identify(lib.id)]

    if not files_to_identify:
        logger.info('No files to identify.')
        _identify_library_done()
        return

    for f in files_to_identify:
        enqueue_or_child('identify_file', {'filepath': f.filepath, 'file_id': f.id})
    set_waiting_for_children()


@register_continuation('identify_library')
def _identify_library_done(**kwargs):
    # Per-file work already handled add_missing + update_titles per touched title;
    # remove_missing_files handles the tail (deleted files → owned flips → batch update).
    enqueue_task('remove_missing_files')


@register_task('identify_file')
@_schedules_generate_library
@_with_titledb
def identify_file_task(filepath, file_id, **kwargs):
    """Identify a single file, upsert its Apps/Titles, then enqueue add_missing_apps_for_title."""
    identified_title_ids = []

    file = db.session.get(Files, file_id)
    if not file:
        return
    if not os.path.exists(filepath):
        logger.warning(f'File {file.filename} no longer exists, deleting from database.')
        Files.query.filter_by(id=file_id).delete(synchronize_session=False)
        db.session.commit()
        return

    logger.info(f'Identifying file: {file.filename}')
    identification, success, file_contents, error = titles_lib.identify_file(filepath)

    if success and file_contents and not error:
        title_ids = list(dict.fromkeys([c['title_id'] for c in file_contents]))
        for title_id in title_ids:
            add_title_id_in_db(title_id)

        nb_content = 0
        for file_content in file_contents:
            logger.info(f'Found content Title ID: {file_content["title_id"]} App ID: {file_content["app_id"]} Type: {file_content["type"]} Version: {file_content["version"]}')
            title_id_in_db = get_title_id_db_id(file_content["title_id"])

            # Atomic owned-OR upsert: on conflict, flip owned=True without
            # clobbering an existing row's title_id/app_type.
            stmt = sqlite_insert(Apps.__table__).values(
                app_id=file_content["app_id"],
                app_version=file_content["version"],
                app_type=file_content["type"],
                owned=True,
                title_id=title_id_in_db,
            ).on_conflict_do_update(
                index_elements=['app_id', 'app_version'],
                set_={'owned': True},
            )
            db.session.execute(stmt)
            db.session.commit()

            add_file_to_app(file_content["app_id"], file_content["version"], file_id)
            nb_content += 1

        if nb_content > 1:
            file.multicontent = True
        file.nb_content = nb_content
        file.identified = True
        identified_title_ids = title_ids
    else:
        logger.warning(f"Error identifying file {file.filename}: {error}")
        file.identification_error = error
        file.identified = False

    file.identification_type = identification
    file.identification_attempts += 1
    file.last_attempt = datetime.datetime.now()
    db.session.commit()

    if identified_title_ids:
        for title_id in identified_title_ids:
            enqueue_or_child('add_missing_apps_for_title', {'title_id': title_id})

        if get_settings()['library']['management']['organizer']['enabled']:
            enqueue_or_child('organize_file', {'file_id': file_id})

        if _current_task_id is not None:
            set_waiting_for_children()


@register_task('add_missing_apps_for_title')
@_schedules_generate_library
@_with_titledb
def add_missing_apps_for_title_task(title_id, **kwargs):
    """Per-title: expand missing base/update/DLC apps for one title, then enqueue update_titles_for_title."""
    add_missing_apps_for_title(title_id)
    enqueue_or_child('update_titles_for_title', {'title_id': title_id})


@register_task('update_titles_for_title')
@_schedules_generate_library
def update_titles_for_title_task(title_id, **kwargs):
    """Per-title: recompute have_base / up_to_date / complete under BEGIN IMMEDIATE."""
    update_title_flags(title_id)


@register_task('add_missing_apps')
@_schedules_generate_library
def add_missing_apps_task(**kwargs):
    """Batch: expand missing apps for every title. Used post-titledb-update."""
    _with_titledb(add_missing_apps_to_db)()
    enqueue_task('update_titles')


@register_task('remove_missing_files')
@_schedules_generate_library
def remove_missing_files_task(**kwargs):
    """Delete DB entries for files missing from disk, then recompute all title flags."""
    remove_missing_files_from_db()
    update_titles()


@register_task('update_titles')
@_schedules_generate_library
def update_titles_task(**kwargs):
    """Batch: recompute flags for every title. Used post-titledb-update."""
    update_titles()


@register_task('organize_library')
def organize_library_task(**kwargs):
    """Organize all identified files, creating a child task per file."""
    app_settings = get_settings()
    organizer_settings = app_settings['library']['management']['organizer']

    if not organizer_settings['enabled']:
        _organize_library_done()
        return

    files = Files.query.filter_by(identified=True).all()
    if not files:
        logger.info('No files to organize.')
        _organize_library_done()
        return
    for f in files:
        enqueue_or_child('organize_file', {'file_id': f.id})
    set_waiting_for_children()


@register_continuation('organize_library')
def _organize_library_done(**kwargs):
    app_settings = get_settings()
    organizer_settings = app_settings['library']['management']['organizer']
    if organizer_settings.get('enabled') and organizer_settings.get('remove_empty_folders'):
        for library in get_libraries():
            delete_empty_folders(library.path)
    enqueue_task('remove_outdated_updates')


@register_task('organize_file')
@_schedules_generate_library
@_with_titledb
def organize_file_task(file_id, **kwargs):
    """Organize a single file."""
    file_obj = db.session.get(Files, file_id)
    if not file_obj:
        return
    organizer_settings = get_settings()['library']['management']['organizer']
    organize_file(file_obj, get_library_path(file_obj.library_id), organizer_settings)


@register_task('remove_outdated_updates')
@_schedules_generate_library
def remove_outdated_updates_task(**kwargs):
    """Remove outdated update files."""
    app_settings = get_settings()
    if app_settings['library']['management']['delete_older_updates']:
        _with_titledb(remove_outdated_update_files)()


@register_task('generate_library')
def generate_library_task(**kwargs):
    generate_library()


# --- File event tasks ---

@register_task('handle_file_added')
def handle_file_added_task(library_path, filepath, **kwargs):
    enqueue_task('add_file', {'library_path': library_path, 'filepath': filepath})


@register_task('handle_file_moved')
def handle_file_moved_task(library_path, src_path, dest_path, **kwargs):
    if file_exists_in_db(src_path):
        update_file_path(library_path, src_path, dest_path)
    else:
        enqueue_task('add_file', {'library_path': library_path, 'filepath': dest_path})


@register_task('handle_file_deleted')
@_schedules_generate_library
def handle_file_deleted_task(filepath, **kwargs):
    delete_file_by_filepath(filepath)
    enqueue_task('update_titles')
