"""Task queue model, registry, and helpers."""
import hashlib
import json
import datetime
import functools
import logging
import os
import shutil
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
import titles as titles_lib
import titledb
import compression
from constants import COMPRESS_EXT, DECOMPRESS_EXT, COMPRESS_TMP_DIRNAME
from db import (
    db, Task, Files, Apps, Libraries, get_library_id, get_library_path, get_library_file_paths,
    get_libraries, add_title_id_in_db, get_title_id_db_id, add_file_to_app,
    file_exists_in_db, update_file_path, delete_file_by_filepath,
    delete_files_under_dir, add_ignored_event, pop_ignored_event,
    set_library_scan_time, remove_missing_files_from_db,
    remove_file_from_apps, reset_file_identification, create_file,
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
TASK_CLEANUP = {}


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


def register_cleanup(task_name):
    """Register a function to call when a running task is cancelled.

    Receives the task's input_data as kwargs. Should be idempotent — the task
    may have been killed at any point, so any intermediate state (temp files,
    partial output) should be removed if present and ignored otherwise.
    """
    def decorator(func):
        TASK_CLEANUP[task_name] = func
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
        # logger.debug(f"Enqueued task child '{task_name}' (id={child_id}) of parent_id={parent_id}")
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
    task.worker_id = None
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

        cursor.execute("SELECT status, task_name, input_json, parent_id FROM tasks WHERE id = ?", (parent_id,))
        row = cursor.fetchone()
        if not row or row[0] != 'waiting_for_children':
            connection.commit()
            return
        grandparent_id = row[3]

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

        # Propagate completion up the chain
        if grandparent_id:
            _try_complete_parent(grandparent_id)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


# --- Cancellation ---

def _cancel_atomic(task_id):
    """Delete the task and any pending descendants under one transaction.
    Running descendants are orphaned (parent_id=NULL) so they finish naturally
    and self-delete on completion. Waiting descendants are recursed into.

    Returns (found, parent_id, running_worker_id, task_name, input_json).
    running_worker_id and task_name/input_json are only set when the cancelled
    task itself was running (so the caller can restart its worker and run cleanup).
    """
    connection = db.engine.raw_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute(
            "SELECT status, task_name, input_json, parent_id, worker_id FROM tasks WHERE id = ?",
            (task_id,)
        )
        row = cursor.fetchone()
        if not row:
            connection.commit()
            return False, None, None, None, None
        status, task_name, input_json, parent_id, worker_id = row
        if status in ('completed', 'failed'):
            connection.commit()
            return False, None, None, None, None

        running_worker_id = worker_id if status == 'running' else None
        cancelled_task_name = task_name if status == 'running' else None
        cancelled_input_json = input_json if status == 'running' else None

        def _walk(pid):
            cursor.execute("SELECT id, status FROM tasks WHERE parent_id = ?", (pid,))
            for child_id, child_status in cursor.fetchall():
                if child_status == 'pending':
                    cursor.execute("DELETE FROM tasks WHERE id = ?", (child_id,))
                elif child_status == 'running':
                    cursor.execute("UPDATE tasks SET parent_id = NULL WHERE id = ?", (child_id,))
                elif child_status == 'waiting_for_children':
                    _walk(child_id)
                    cursor.execute("DELETE FROM tasks WHERE id = ?", (child_id,))

        if status == 'waiting_for_children':
            _walk(task_id)

        cursor.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        connection.commit()
        return True, parent_id, running_worker_id, cancelled_task_name, cancelled_input_json
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def cancel_task(task_id):
    """Cancel a task. Returns True if cancelled, False if not found or already terminal.

    - pending: deleted.
    - running: worker is restarted (mid-task termination), cleanup hook runs.
    - waiting_for_children: pending descendants deleted, running descendants
      orphaned (allowed to finish), parent deleted.
    """
    found, parent_id, worker_id, task_name, input_json = _cancel_atomic(task_id)
    if not found:
        return False

    if worker_id is not None:
        import app as app_mod
        if app_mod.pool is not None:
            app_mod.pool.restart_worker(worker_id)

    if task_name is not None:
        cleanup = TASK_CLEANUP.get(task_name)
        if cleanup:
            input_data = json.loads(input_json) if input_json else {}
            try:
                cleanup(**input_data)
            except Exception as e:
                logger.error(f"Cleanup hook for cancelled task '{task_name}' failed: {e}")

    if parent_id:
        _try_complete_parent(parent_id)
    return True


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

@register_task('startup')
def startup_task(**kwargs):
    """Startup task: cleanup and kick off periodic titledb update."""
    update_titledb_task()
    scan_libraries_task()

# --- Periodic tasks ---
@register_task('update_titledb')
def update_titledb_task(**kwargs):
    settings = get_settings()
    titledb.update_titledb(settings)
    enqueue_task('organize_library')
    add_missing_apps_to_db()
    update_titles()
    # Re-enqueue for next scheduled run
    interval_str = settings.get('scheduler', {}).get('scan_interval', '12h')
    delta = interval_string_to_timedelta(interval_str)
    if delta:
        enqueue_task('update_titledb', run_after=datetime.datetime.utcnow() + delta)


# --- Scan pipeline ---
@register_task('scan_libraries')
def scan_libraries_task(**kwargs):
    """Scan all library paths for new files."""
    libraries = get_libraries()
    if not libraries:
        logger.info('No libraries to scan.')
        return
    for lib in libraries:
        enqueue_or_child('scan_library', {'library_path': lib.path})
    set_waiting_for_children()

@register_task('scan_library')
def scan_library_task(library_path, **kwargs):
    """Scan a library path for new files, creating a child task per file."""
    library_id = get_library_id(library_path)
    if not os.path.isdir(library_path):
        logger.warning(f'Library path {library_path} does not exist.')
        return

    logger.info(f'Scanning library path {library_path} ...')
    _, files = titles_lib.getDirsAndFiles(library_path)
    filepaths_in_db = set(get_library_file_paths(library_id))
    new_files = [f for f in files if f not in filepaths_in_db]

    if not new_files:
        logger.info(f'No new files found in {library_path}.')
        _scan_library_done(library_path=library_path)
        return

    enqueued = 0
    for fp in new_files:
        new_file = _insert_file(library_path, library_id, fp)
        if new_file is not None:
            enqueue_or_child('identify_file', {'filepath': fp, 'file_id': new_file.id})
            enqueued += 1

    if enqueued:
        set_waiting_for_children()
    else:
        _scan_library_done(library_path=library_path)


@register_continuation('scan_library')
def _scan_library_done(library_path, **kwargs):
    set_library_scan_time(get_library_id(library_path))
    enqueue_task('remove_missing_files')


def _insert_file(library_path, library_id, filepath):
    """Read file info from disk and insert a Files row. Returns the row, or None on failure."""
    file_display = filepath.replace(library_path, "").lstrip("/")
    logger.info(f'Getting file info: {file_display}')
    file_info = titles_lib.get_file_info(filepath)
    if file_info is None:
        logger.error(f'Failed to get info for file: {file_display}')
        return None
    return create_file(library_id, filepath, file_info)


@register_task('add_file')
def add_file_task(library_path, filepath, **kwargs):
    """Add a single file to the library DB."""
    library_id = get_library_id(library_path)
    if filepath in get_library_file_paths(library_id):
        return

    new_file = _insert_file(library_path, library_id, filepath)
    if new_file is None:
        raise ValueError(f'Failed to add file: {filepath}')

    enqueue_or_child('identify_file', {'filepath': filepath, 'file_id': new_file.id})
    set_waiting_for_children()


# --- Identify pipeline ---
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
    # final pass GCs unowned titles and recomputes flags.
    enqueue_task('update_titles')


@register_task('identify_file')
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
    # ensure Keys loaded status
    get_settings()
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

        mgmt = get_settings()['library']['management']
        if mgmt['organizer']['enabled']:
            # Organizer runs first; it enqueues compression after the file is in place.
            enqueue_or_child('organize_file', {'file_id': file_id})
        elif mgmt['compress_files']:
            enqueue_task('compress_file', {'file_id': file_id})

        set_waiting_for_children()


@register_task('add_missing_apps_for_title')
@_schedules_generate_library
def add_missing_apps_for_title_task(title_id, **kwargs):
    """Per-title: expand missing base/update/DLC apps for one title, then enqueue update_titles_for_title."""
    add_missing_apps_for_title(title_id)
    enqueue_or_child('update_titles_for_title', {'title_id': title_id})
    set_waiting_for_children()


@register_task('update_titles_for_title')
@_schedules_generate_library
def update_titles_for_title_task(title_id, **kwargs):
    """Per-title: recompute have_base / up_to_date / complete under BEGIN IMMEDIATE."""
    update_title_flags(title_id)


# --- Organize pipeline ---
@register_task('organize_library')
def organize_library_task(**kwargs):
    """Organize all identified files, creating a child task per file."""
    app_settings = get_settings()
    organizer_settings = app_settings['library']['management']['organizer']

    if not organizer_settings['enabled']:
        _organize_library_done()
        return

    files = Files.query.filter_by(identified=True, organized=False).all()
    if not files:
        logger.info('No files to organize.')
        _organize_library_done()
        return
    for f in files:
        enqueue_or_child('organize_file', {'file_id': f.id})
    set_waiting_for_children()


@register_task('organize_library_done')
@register_continuation('organize_library')
def _organize_library_done(library_path=None, **kwargs):
    settings = get_settings()
    organizer_settings = settings['library']['management']['organizer']
    if organizer_settings.get('enabled') and organizer_settings.get('remove_empty_folders'):
        paths = [library_path] if library_path else [lib.path for lib in get_libraries()]
        for path in paths:
            delete_empty_folders(path)
    if settings['library']['management']['delete_older_updates']:
        enqueue_task('remove_outdated_updates')
    if settings['library']['management']['compress_files']:
        enqueue_task('compress_library')


@register_task('organize_file')
def organize_file_task(file_id, **kwargs):
    """Organize a single file."""
    file_obj = db.session.get(Files, file_id)
    if not file_obj:
        return
    library_path = get_library_path(file_obj.library_id)
    organizer_settings = get_settings()['library']['management']['organizer']
    if organize_file(file_obj, library_path, organizer_settings):
        file_obj.organized = True
        db.session.commit()
    if get_settings()['library']['management']['compress_files']:
        enqueue_task('compress_file', {'file_id': file_id})
    enqueue_task('organize_library_done', {'library_path': library_path})


@register_task('remove_outdated_updates')
def remove_outdated_updates_task(**kwargs):
    """Remove outdated update files."""
    remove_outdated_update_files()
    enqueue_task('update_titles')


# --- Compression pipeline ---
def _compress_tmp_dir(library_path, file_id):
    """Per-file working dir under the library root (same filesystem as the source,
    so os.replace into place is atomic; hidden from scanner and watcher)."""
    return os.path.join(library_path, COMPRESS_TMP_DIRNAME, str(file_id))


def _replace_in_place(file_obj, out, target, new_extension, compressed):
    """Atomically move a produced file onto target and update the DB row, bracketing
    the source-delete and target-create so the watcher ignores our own writes."""
    source = file_obj.filepath
    # Suppress our own writes across both observer shapes: native inotify reports the
    # move out of the temp dir as moved(out -> target); a polling observer reports it as
    # a bare created(target). Plus the source deletion.
    add_ignored_event(target, '')
    add_ignored_event(str(out), target)
    add_ignored_event(source, '')
    try:
        os.replace(str(out), target)
        # DB now points at a file that exists on disk; only then drop the source.
        file_obj.filepath = target
        file_obj.extension = new_extension
        file_obj.size = os.path.getsize(target)
        file_obj.compressed = compressed
        db.session.commit()
        if os.path.abspath(source) != os.path.abspath(target):
            os.remove(source)
    except Exception:
        pop_ignored_event(src_path=target, dest_path='')
        pop_ignored_event(src_path=str(out), dest_path=target)
        pop_ignored_event(src_path=source, dest_path='')
        raise


@register_task('compress_library')
def compress_library_task(**kwargs):
    """Compress every uncompressed, identified game file, one child task per file."""
    if not get_settings()['library']['management']['compress_files']:
        return
    files = Files.query.filter(
        Files.compressed.is_(False),
        Files.identified.is_(True),
        Files.extension.in_(list(COMPRESS_EXT.keys())),
    ).all()
    enqueued = 0
    for f in files:
        enqueue_or_child('compress_file', {'file_id': f.id})
        enqueued += 1
    if enqueued:
        set_waiting_for_children()


@register_task('compress_file')
def compress_file_task(file_id, **kwargs):
    """Compress a single file in place: NSP->NSZ / XCI->XCZ, preserving its DB row."""
    file_obj = db.session.get(Files, file_id)
    if not file_obj or file_obj.compressed or file_obj.extension not in COMPRESS_EXT:
        return
    source = file_obj.filepath
    if not os.path.exists(source):
        return

    tmp_dir = _compress_tmp_dir(get_library_path(file_obj.library_id), file_id)
    target = str(compression.compressed_path(source))
    opts = get_settings()['library']['management']['compression']
    shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(tmp_dir, exist_ok=True)
    try:
        out = compression.compress_to(source, tmp_dir, opts)
        _replace_in_place(file_obj, out, target, COMPRESS_EXT[file_obj.extension], True)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    logger.info(f'Compressed {os.path.basename(source)} -> {os.path.basename(target)}')


@register_task('decompress_file')
def decompress_file_task(file_id, **kwargs):
    """Decompress a single file in place: NSZ->NSP / XCZ->XCI, preserving its DB row."""
    file_obj = db.session.get(Files, file_id)
    if not file_obj or not file_obj.compressed or file_obj.extension not in DECOMPRESS_EXT:
        return
    source = file_obj.filepath
    if not os.path.exists(source):
        return

    tmp_dir = _compress_tmp_dir(get_library_path(file_obj.library_id), file_id)
    target = str(compression.decompressed_path(source))
    shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(tmp_dir, exist_ok=True)
    try:
        out = compression.decompress_to(source, tmp_dir)
        _replace_in_place(file_obj, out, target, DECOMPRESS_EXT[file_obj.extension], False)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    logger.info(f'Decompressed {os.path.basename(source)} -> {os.path.basename(target)}')


@register_cleanup('compress_file')
@register_cleanup('decompress_file')
def _compression_cleanup(file_id, **kwargs):
    """Idempotent cancel/crash cleanup: drop the working dir and any queued ignored
    events. The source is never mutated before its verified replacement exists, so
    there is nothing else to undo."""
    file_obj = db.session.get(Files, file_id)
    if not file_obj:
        return
    shutil.rmtree(_compress_tmp_dir(get_library_path(file_obj.library_id), file_id), ignore_errors=True)
    paths = {file_obj.filepath}
    if file_obj.extension in COMPRESS_EXT:
        paths.add(str(compression.compressed_path(file_obj.filepath)))
    if file_obj.extension in DECOMPRESS_EXT:
        paths.add(str(compression.decompressed_path(file_obj.filepath)))
    for p in paths:
        pop_ignored_event(src_path=p, dest_path='')

# --- Batch maintenance ---
@register_task('add_missing_apps')
def add_missing_apps_task(**kwargs):
    """Batch: expand missing apps for every title. Used post-titledb-update."""
    add_missing_apps_to_db()
    enqueue_task('update_titles')


@register_task('remove_missing_files')
def remove_missing_files_task(**kwargs):
    """Delete DB entries for files missing from disk, then recompute all title flags."""
    remove_missing_files_from_db()
    enqueue_task('update_titles')


@register_task('update_titles')
@_schedules_generate_library
def update_titles_task(**kwargs):
    """Batch: recompute flags for every title. Used post-titledb-update."""
    update_titles()


# --- Library lifecycle ---
@register_task('remove_library')
def remove_library_task(library_path, **kwargs):
    """Delete a library and its files (flipping app ownership), then recompute titles."""
    library = Libraries.query.filter_by(path=library_path).first()
    if not library:
        return
    for file_id in [f.id for f in library.files]:
        remove_file_from_apps(file_id)
    db.session.delete(library)
    db.session.commit()
    logger.info(f"Removed library: {library_path}")
    enqueue_task('update_titles')


# --- Shop generation ---
@register_task('generate_library')
def generate_library_task(**kwargs):
    generate_library()


# --- Watcher event handlers ---
@register_task('handle_file_added')
def handle_file_added_task(library_path, filepath, **kwargs):
    file = Files.query.filter_by(filepath=filepath).first()
    if file is None:
        enqueue_task('add_file', {'library_path': library_path, 'filepath': filepath})
        return

    new_size = titles_lib.get_file_size(filepath)
    new_mtime = os.path.getmtime(filepath)
    if file.size == new_size and file.mtime == new_mtime:
        return

    logger.info(f'File changed on disk, re-identifying: {file.filename}')
    remove_file_from_apps(file.id)
    file.size = new_size
    file.mtime = new_mtime
    file.organized = False
    reset_file_identification(file)
    db.session.commit()
    enqueue_task('identify_file', {'filepath': filepath, 'file_id': file.id})


@register_task('handle_file_moved')
def handle_file_moved_task(library_path, src_path, dest_path, **kwargs):
    if file_exists_in_db(src_path):
        update_file_path(library_path, src_path, dest_path)
    else:
        enqueue_task('add_file', {'library_path': library_path, 'filepath': dest_path})


@register_task('handle_file_deleted')
def handle_file_deleted_task(filepath, **kwargs):
    delete_file_by_filepath(filepath)
    enqueue_task('update_titles')


@register_task('handle_dir_deleted')
def handle_dir_deleted_task(dirpath, **kwargs):
    """A folder was moved out/removed: delete all its files from the library."""
    if delete_files_under_dir(dirpath):
        enqueue_task('update_titles')
