"""Task queue model, registry, and helpers."""
import hashlib
import json
import datetime
import logging
import os
from collections import namedtuple
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
import titles as titles_lib
import titledb
from containers import compression
from containers import verification as verification_lib
from constants import COMPRESS_EXT, DECOMPRESS_EXT
from db import (
    db, Task, Files, Apps, Libraries, get_library_id, get_library_path, get_library_file_paths,
    get_libraries, add_title_id_in_db, get_title_id_db_id, add_file_to_app,
    file_exists_in_db, update_file_path, delete_file_by_filepath,
    delete_files_under_dir, add_ignored_event, pop_ignored_event,
    add_temp_file, remove_temp_file, claim_temp_file, get_temp_file_paths, purge_temp_files,
    set_library_scan_time, remove_missing_files_from_db,
    remove_file_from_apps, reset_file_identification, reset_file_verification, create_file,
)
from settings import get_settings
from utils import interval_string_to_timedelta, delete_empty_folders, human_size
from library import (
    add_missing_apps_for_title, update_title_flags,
    add_missing_apps_to_db, update_titles, organize_file,
    remove_outdated_update_files,
)

logger = logging.getLogger('main')

# --- Task Registry ---
TASK_REGISTRY = {}
TASK_CONTINUATIONS = {}
TASK_CLEANUP = {}
TASK_GROUPS = {}  # task_name -> concurrency-group name


def register_task(name, group=None):
    """Register a callable as a named task. `group` assigns it to a concurrency group whose
    parallelism is capped by worker.group_limits."""
    def decorator(func):
        TASK_REGISTRY[name] = func
        if group:
            TASK_GROUPS[name] = group
        return func
    return decorator


def blocked_task_names(running_task_names):
    """Task names that must not be claimed right now because their concurrency group is already
    at its configured limit, given the task_names currently running."""
    limits = get_settings().get('worker', {}).get('group_limits', {})
    if not limits:
        return set()
    running_per_group = {}
    for name in running_task_names:
        group = TASK_GROUPS.get(name)
        if group is not None:
            running_per_group[group] = running_per_group.get(group, 0) + 1
    full = {g for g, limit in limits.items() if running_per_group.get(g, 0) >= limit}
    return {name for name, group in TASK_GROUPS.items() if group in full}


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


def _task_progress(task_id):
    """Return a callback that writes live percent to a task row, or None outside a task."""
    if task_id is None:
        return None
    engine = db.engine
    logged = [-1]

    def report(pct):
        connection = engine.raw_connection()
        try:
            cursor = connection.cursor()
            cursor.execute("UPDATE tasks SET completion_pct = ? WHERE id = ? AND status = 'running'",
                           (pct, task_id))
            connection.commit()
        finally:
            connection.close()
        if pct // 5 != logged[0]:
            logged[0] = pct // 5
            logger.debug(f"Task {task_id} progress: {pct}%")

    return report

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
        _run_cleanup_hook(task_name, input_json)

    if parent_id:
        _try_complete_parent(parent_id)
    return True


def _run_cleanup_hook(task_name, input_json):
    """Run a task's registered @register_cleanup hook (idempotent) if it has one."""
    cleanup = TASK_CLEANUP.get(task_name)
    if not cleanup:
        return
    input_data = json.loads(input_json) if input_json else {}
    try:
        cleanup(**input_data)
    except Exception as e:
        logger.error(f"Cleanup hook for task '{task_name}' failed: {e}")


def reap_worker_task(worker_id):
    """Fail and clean up the task a worker was running when it was stopped mid-task."""
    task = Task.query.filter_by(status='running', worker_id=worker_id).first()
    if task is None:
        return
    task_name, input_json, parent_id = task.task_name, task.input_json, task.parent_id
    task.status = 'failed'
    task.error_message = 'Interrupted by worker stop'
    task.exit_code = 1
    task.completed_at = datetime.datetime.utcnow()
    db.session.commit()
    logger.info(f"Reaped task {task.id} ({task_name}) from stopped worker {worker_id}")
    _run_cleanup_hook(task_name, input_json)
    if parent_id:
        _try_complete_parent(parent_id)


# --- Startup cleanup ---

def cleanup_tasks():
    """Startup cleanup: clear the pending queue and fail interrupted tasks."""
    # Remove completed tasks
    Task.query.filter_by(status='completed').delete()

    # Clear the entire pending queue
    Task.query.filter_by(status='pending').delete()

    # Mark running/waiting tasks as failed — they can't survive a restart
    stale = Task.query.filter(Task.status.in_(['running', 'waiting_for_children'])).all()
    for task in stale:
        task.status = 'failed'
        task.error_message = 'Interrupted by application restart'
        task.exit_code = 1
        task.completed_at = datetime.datetime.utcnow()
        logger.info(f"Reset stale task {task.id} ({task.task_name})")

    db.session.commit()

    # Sweep leftover output from any (de)compression interrupted by the restart.
    purge_temp_files()


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



@register_task('startup')
def startup_task(**kwargs):
    """Startup task: resume interrupted per-file work, then kick off the titledb update."""
    # Enqueued here rather than only from update_titledb_task, whose network fetch can fail:
    # recovery of an interrupted pipeline must not wait for the next scheduled titledb run.
    enqueue_task('process_library')
    update_titledb_task()
    scan_libraries_task()

# --- Periodic tasks ---
@register_task('update_titledb')
def update_titledb_task(**kwargs):
    settings = get_settings()
    titledb.update_titledb(settings)
    enqueue_task('process_library')
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
    skip = set(get_library_file_paths(library_id)) | get_temp_file_paths()
    new_files = [f for f in files if f not in skip]

    if not new_files:
        logger.info(f'No new files found in {library_path}.')
        _scan_library_done(library_path=library_path)
        return

    enqueued = 0
    for fp in new_files:
        new_file = _insert_file(library_path, library_id, fp)
        if new_file is not None:
            enqueue_or_child('process_file', {'file_id': new_file.id})
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

    enqueue_task('process_file', {'file_id': new_file.id})


# --- Per-file pipeline ---
#
# Every stage a file can need, in the order it needs them. A stage either runs inline in
# the driver (`run`) or is delegated to a registered task (`task`) that re-drives the file
# when it finishes — delegation is what buys a concurrency group, a cancel hook and a
# progress bar, so it is reserved for the stages that want them.
Stage = namedtuple('Stage', 'name applies run task')


def _needs_identify(file, mgmt):
    """The per-file form of library.get_files_to_identify."""
    if not file.identified and not file.identification_attempts:
        return True
    return bool(titles_lib.Keys.keys_loaded) and file.identification_type == 'filename'


def _identify(file, mgmt):
    """Identify one file and upsert its Apps/Titles."""
    identified_title_ids = []
    filepath = file.filepath
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

            add_file_to_app(file_content["app_id"], file_content["version"], file.id)
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

    # Top-level, not a child: the driver stays a leaf, so a scan is not held open by the
    # per-title expansion and cancelling one does not cancel the other.
    for title_id in identified_title_ids:
        enqueue_task('add_missing_apps_for_title', {'title_id': title_id})


def _needs_verify(file, mgmt):
    verification = mgmt['verification']
    if not verification['enabled'] or file.extension not in verification_lib.VERIFY_EXT:
        return False
    if not titles_lib.Keys.keys_loaded:
        return False   # nstools cannot open a container without them
    if verification['depth'] == verification_lib.DEPTH_HASH:
        # A failed hash recorded before hash_modified existed cannot say whether it was a
        # repack or real damage, and that is the difference between a shrug and an alert.
        # Re-check those - only already-failing files pay for it, and they are rare.
        return file.hash_valid is None or (file.hash_valid is False
                                           and file.hash_modified is None)
    return file.signature_valid is None


def _needs_organize(file, mgmt):
    return mgmt['organizer']['enabled'] and file.identified and not file.organized


def _organize(file, mgmt):
    """Place one file under the organizer templates, holding the path claim across the move."""
    claimed = file.filepath
    if not claim_temp_file(claimed):
        return
    library_path = get_library_path(file.library_id)
    try:
        if organize_file(file, library_path, mgmt['organizer']):
            file.organized = True
            db.session.commit()
    finally:
        remove_temp_file(claimed)
    enqueue_task('library_maintenance', {'library_path': library_path})


def _needs_compress(file, mgmt):
    if not mgmt['compression']['enabled'] or file.compressed or file.extension not in COMPRESS_EXT:
        return False
    if file.hash_valid is False:
        # Known-corrupt: its NCAs do not hash to what it claims, so compressing it would
        # just fail nsz's own round-trip check. A failed *signature* is not grounds to
        # refuse - a re-signed repack is commonplace and its content is still intact.
        return False
    target = compression.conversion_target(file)
    return Files.query.filter(Files.filepath == target, Files.id != file.id).first() is None


STAGES = [
    Stage('identify', _needs_identify, _identify, None),
    Stage('verify', _needs_verify, None, 'verify_file'),
    Stage('organize', _needs_organize, _organize, None),
    Stage('compress', _needs_compress, None, 'compress_file'),
]


@register_task('process_file')
def process_file_task(file_id, **kwargs):
    """Drive one file down the stage list: inline stages here, delegated stages by task."""
    done = set()
    while True:
        file = db.session.get(Files, file_id)
        if file is None:
            return
        if not os.path.exists(file.filepath):
            logger.warning(f'File {file.filename} no longer exists, deleting from database.')
            remove_file_from_apps(file_id)
            Files.query.filter_by(id=file_id).delete(synchronize_session=False)
            db.session.commit()
            return
        mgmt = get_settings()['library']['management']
        # `done` and not a plain break: a stage is not guaranteed to flip its own applies()
        # - organize_file gives up on a file with no app or no title info, and the claim can
        # be held elsewhere - and re-picking it would spin the worker forever.
        stage = next((s for s in STAGES if s.name not in done and s.applies(file, mgmt)), None)
        if stage is None:
            return
        done.add(stage.name)
        if stage.task:
            enqueue_task(stage.task, {'file_id': file_id})
            return
        stage.run(file, mgmt)


@register_task('process_library')
def process_library_task(**kwargs):
    """Drive every file that still has pipeline work."""
    mgmt = get_settings()['library']['management']
    files = [f for f in Files.query.all() if any(s.applies(f, mgmt) for s in STAGES)]
    logger.info(f'Processing library: {len(files)} file(s) with pending work.')
    for f in files:
        enqueue_or_child('process_file', {'file_id': f.id})
    if files:
        set_waiting_for_children()


@register_continuation('process_library')
def _process_library_done(**kwargs):
    enqueue_task('library_maintenance')
    enqueue_task('update_titles')


@register_task('library_maintenance')
def library_maintenance_task(library_path=None, **kwargs):
    """Post-organization GC: prune empty folders and outdated updates."""
    settings = get_settings()
    organizer = settings['library']['management']['organizer']
    if organizer.get('enabled') and organizer.get('remove_empty_folders'):
        paths = [library_path] if library_path else [lib.path for lib in get_libraries()]
        for path in paths:
            delete_empty_folders(path)
    if settings['library']['management']['delete_older_updates']:
        enqueue_task('remove_outdated_updates')


@register_task('add_missing_apps_for_title')
def add_missing_apps_for_title_task(title_id, **kwargs):
    """Per-title: expand missing base/update/DLC apps for one title, then enqueue update_titles_for_title."""
    add_missing_apps_for_title(title_id)
    enqueue_or_child('update_titles_for_title', {'title_id': title_id})
    set_waiting_for_children()


@register_task('update_titles_for_title')
def update_titles_for_title_task(title_id, **kwargs):
    """Per-title: recompute have_base / up_to_date / complete under BEGIN IMMEDIATE."""
    update_title_flags(title_id)


@register_task('remove_outdated_updates')
def remove_outdated_updates_task(**kwargs):
    """Remove outdated update files."""
    remove_outdated_update_files()
    enqueue_task('update_titles')


# --- Verification ---
@register_task('verify_file', group='io')
def verify_file_task(file_id, **kwargs):
    """Verify one file's signatures and, at hash depth, its NCA content hashes."""
    file_obj = db.session.get(Files, file_id)
    if not file_obj or file_obj.extension not in verification_lib.VERIFY_EXT:
        return
    if not os.path.exists(file_obj.filepath):
        return
    depth = get_settings()['library']['management']['verification']['depth']
    logger.info(f'Verifying file ({depth}): {file_obj.filename}')
    signature_valid, hash_valid, hash_modified, error = verification_lib.verify(
        file_obj.filepath, depth, progress=_task_progress(_current_task_id))

    file_obj.signature_valid = signature_valid
    if hash_valid is not None:
        file_obj.hash_valid = hash_valid
        file_obj.hash_modified = hash_modified
    file_obj.verification_error = error
    file_obj.verified_at = datetime.datetime.now()
    db.session.commit()

    if error:
        logger.warning(f'Verification failed for {file_obj.filename}: {error}')
    enqueue_task('process_file', {'file_id': file_id})


# --- Compression pipeline ---
def _finalize_conversion(file_obj, target, new_extension, compressed):
    """Flip the Files row onto the verified output, then drop the now-redundant source."""
    source = file_obj.filepath
    add_ignored_event(source, '')  # our own deletion of the source
    file_obj.filepath = target
    file_obj.extension = new_extension
    file_obj.size = os.path.getsize(target)
    file_obj.mtime = os.path.getmtime(target)
    file_obj.compressed = compressed
    db.session.commit()
    if os.path.abspath(source) != os.path.abspath(target):
        os.remove(source)


def _convert_file(file_obj, produce, new_extension, compressed):
    """Run a (de)compression: produce the verified output at its final path, then finalize.
    Returns whether the row was flipped onto the new file - a caller that re-drives the
    pipeline must not do so after a no-op, or it delegates the same stage forever."""
    source = file_obj.filepath
    target = compression.conversion_target(file_obj)
    if Files.query.filter(Files.filepath == target, Files.id != file_obj.id).first() is not None:
        logger.warning(f'Skipping conversion of {os.path.basename(source)}: '
                       f'{os.path.basename(target)} is already in the library.')
        return False
    if not claim_temp_file(source):
        logger.debug(f'Skipping conversion of {os.path.basename(source)}: file is busy.')
        return False
    before = file_obj.size
    add_temp_file(target)
    try:
        out = str(produce(source, os.path.dirname(source)))
        _finalize_conversion(file_obj, out, new_extension, compressed)
    finally:
        remove_temp_file(target)
        remove_temp_file(source)
    after = file_obj.size
    ratio = after / before if before else 0
    verb = 'compressing' if compressed else 'decompressing'
    logger.info(f'Finished {verb} {os.path.basename(target)}: '
                f'{human_size(before)} -> {human_size(after)} (ratio {ratio:.1%})')
    return True


@register_task('compress_file', group='io')
def compress_file_task(file_id, **kwargs):
    """Compress a single file in place: NSP->NSZ / XCI->XCZ, preserving its DB row."""
    file_obj = db.session.get(Files, file_id)
    if not file_obj or file_obj.compressed or file_obj.extension not in COMPRESS_EXT:
        return
    if not os.path.exists(file_obj.filepath):
        return
    logger.info(f'Compressing file: {file_obj.filename}')
    opts = get_settings()['library']['management']['compression']
    progress = _task_progress(_current_task_id)
    if _convert_file(file_obj,
                     lambda source, out_dir: compression.compress_to(source, out_dir, opts, progress=progress),
                     COMPRESS_EXT[file_obj.extension], True):
        enqueue_task('process_file', {'file_id': file_id})


@register_task('decompress_file', group='io')
def decompress_file_task(file_id, **kwargs):
    """Decompress a single file in place: NSZ->NSP / XCZ->XCI, preserving its DB row."""
    file_obj = db.session.get(Files, file_id)
    if not file_obj or not file_obj.compressed or file_obj.extension not in DECOMPRESS_EXT:
        return
    if not os.path.exists(file_obj.filepath):
        return
    progress = _task_progress(_current_task_id)
    # No re-drive: the pipeline's compress stage would immediately undo what the user asked
    # for. The next process_library sweep still re-compresses it, as compress_library did.
    _convert_file(file_obj,
                  lambda source, out_dir: compression.decompress_to(source, out_dir, progress=progress),
                  DECOMPRESS_EXT[file_obj.extension], False)


@register_cleanup('compress_file')
@register_cleanup('decompress_file')
def _compression_cleanup(file_id, **kwargs):
    """Idempotent cancel/crash cleanup: clear the in-progress mark, remove the partial
    output if it isn't a committed file, and pop the source-deletion ignored event."""
    file_obj = db.session.get(Files, file_id)
    if not file_obj:
        return
    remove_temp_file(file_obj.filepath)  # release the source in-progress claim
    target = compression.conversion_target(file_obj)
    if target:
        remove_temp_file(target)
        if Files.query.filter_by(filepath=target).first() is None and os.path.exists(target):
            os.remove(target)
    pop_ignored_event(src_path=file_obj.filepath, dest_path='')

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
    reset_file_verification(file)
    db.session.commit()
    enqueue_task('process_file', {'file_id': file.id})


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
