"""Task queue model, registry, and helpers."""
import hashlib
import json
import datetime
import logging
import os
from db import db

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


# --- Task Model ---
class Task(db.Model):
    __tablename__ = 'tasks'

    id = db.Column(db.Integer, primary_key=True)
    parent_id = db.Column(db.Integer, db.ForeignKey('tasks.id'), nullable=True)
    task_name = db.Column(db.String, nullable=False, index=True)
    status = db.Column(db.String, nullable=False, default='pending')
    completion_pct = db.Column(db.Integer, default=0)
    input_json = db.Column(db.Text, nullable=False, default='{}')
    input_hash = db.Column(db.String(64), nullable=False)
    output_json = db.Column(db.Text)
    exit_code = db.Column(db.Integer)
    error_message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.datetime.utcnow)
    started_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)

    children = db.relationship('Task', backref=db.backref('parent', remote_side=[id]), lazy='dynamic')

    __table_args__ = (
        db.Index('ix_tasks_status_created', 'status', 'created_at'),
        db.Index('ix_tasks_parent_id', 'parent_id'),
    )


# --- Progress ---
_current_task_id = None


def update_progress(pct):
    """Update completion_pct for the currently executing task."""
    if _current_task_id is None:
        return
    task = db.session.get(Task, _current_task_id)
    if task:
        task.completion_pct = int(pct)
        logger.debug(f"Task {task.id} ({task.task_name}) progress: {task.completion_pct}%")
        db.session.commit()


# --- Child task helpers ---

def create_child_task(parent_id, task_name, input_data=None):
    """Create a child task record linked to a parent. Returns the child Task."""
    input_data = input_data or {}
    child = Task(
        parent_id=parent_id,
        task_name=task_name,
        status='pending',
        input_json=json.dumps(input_data, sort_keys=True),
        input_hash=compute_input_hash(input_data),
        created_at=datetime.datetime.utcnow(),
    )
    db.session.add(child)
    db.session.flush()
    return child


def complete_child_task(child, output=None, error=None):
    """Mark a child task as completed or failed and update parent progress."""
    now = datetime.datetime.utcnow()
    if error:
        child.status = 'failed'
        child.error_message = str(error)
        child.exit_code = 1
    else:
        child.status = 'completed'
        child.exit_code = 0
        child.output_json = json.dumps(output) if output else None
    child.completed_at = now
    child.completion_pct = 100
    parent_id = child.parent_id
    db.session.commit()

    # Update parent progress atomically (handles multi-worker race)
    if parent_id:
        _try_complete_parent(parent_id)


def set_waiting_for_children():
    """Mark the current task as waiting for its children to complete."""
    if _current_task_id is None:
        return
    task = db.session.get(Task, _current_task_id)
    if task:
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
        logger.debug(f"Task {parent_id} ({row[1]}) progress: {done}/{total} children — {pct}%")

        if done < total:
            cursor.execute("UPDATE tasks SET completion_pct = ? WHERE id = ?", (pct, parent_id))
            connection.commit()
            return

        # All children done — mark parent complete
        now = datetime.datetime.utcnow().isoformat()
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
            logger.info(f"Parent task {parent_id} ({task_name}) completed, continuation executed")
        else:
            logger.info(f"Parent task {parent_id} ({task_name}) completed")

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
    """Startup cleanup: remove completed tasks and fail stale running tasks."""
    # Remove completed tasks
    completed = Task.query.filter_by(status='completed').count()
    if completed:
        Task.query.filter_by(status='completed').delete()
        logger.info(f"Removed {completed} completed tasks")

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


def enqueue_task(task_name, input_data=None):
    """Enqueue a task. Returns (task, created) — created is False if a duplicate pending/running task exists."""
    if task_name not in TASK_REGISTRY:
        raise ValueError(f"Unknown task: {task_name}")

    input_data = input_data or {}
    input_hash = compute_input_hash(input_data)
    input_json = json.dumps(input_data, sort_keys=True)

    connection = db.engine.raw_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("BEGIN IMMEDIATE")

        cursor.execute(
            "SELECT id FROM tasks WHERE task_name = ? AND input_hash = ? AND status IN ('pending', 'running', 'waiting_for_children')",
            (task_name, input_hash)
        )
        existing = cursor.fetchone()

        if existing:
            connection.commit()
            task = db.session.get(Task, existing[0])
            return task, False

        now = datetime.datetime.utcnow().isoformat()
        cursor.execute(
            "INSERT INTO tasks (task_name, status, completion_pct, input_json, input_hash, created_at) "
            "VALUES (?, 'pending', 0, ?, ?, ?)",
            (task_name, input_json, input_hash, now)
        )
        new_id = cursor.lastrowid
        connection.commit()

        task = db.session.get(Task, new_id)
        return task, True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_task(task_id):
    return db.session.get(Task, task_id)


def get_tasks(status=None, limit=50):
    query = Task.query.order_by(Task.created_at.desc())
    if status:
        query = query.filter_by(status=status)
    return query.limit(limit).all()


# --- Titledb helper for tasks ---

def _with_titledb(func):
    """Wrapper that loads titledb before and releases the reference after."""
    import titles as titles_lib
    titles_lib.load_titledb()
    try:
        return func()
    finally:
        titles_lib.identification_in_progress_count -= 1
        titles_lib.unload_titledb()


# --- Pipeline tasks ---

@register_task('update_titledb')
def update_titledb_task(**kwargs):
    import titledb
    from settings import load_settings
    from db import get_libraries
    settings = load_settings()
    titledb.update_titledb(settings)
    for lib in get_libraries():
        enqueue_task('scan_library', {'library_path': lib.path})


@register_task('scan_library')
def scan_library_task(library_path, **kwargs):
    """Scan a library path for new files, creating a child task per file."""
    import titles as titles_lib
    from db import get_library_id, get_library_file_paths

    library_id = get_library_id(library_path)
    if not os.path.isdir(library_path):
        logger.warning(f'Library path {library_path} does not exist.')
        return

    _, files = titles_lib.getDirsAndFiles(library_path)
    filepaths_in_db = get_library_file_paths(library_id)
    new_files = [f for f in files if f not in filepaths_in_db]

    if not new_files:
        logger.info(f'No new files found in {library_path}.')
        _scan_library_done(library_path=library_path)
        return

    parent_id = _current_task_id
    for filepath in new_files:
        create_child_task(parent_id, 'add_file', {'library_path': library_path, 'filepath': filepath})
    db.session.commit()
    set_waiting_for_children()


@register_continuation('scan_library')
def _scan_library_done(library_path, **kwargs):
    from db import set_library_scan_time, get_library_id
    set_library_scan_time(get_library_id(library_path))
    enqueue_task('identify_library', {'library_path': library_path})


@register_task('add_file')
def add_file_task(library_path, filepath, **kwargs):
    """Add a single file to the library DB."""
    import titles as titles_lib
    from db import get_library_id, get_library_file_paths, Files

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


@register_task('identify_library')
def identify_library_task(library_path, **kwargs):
    """Identify all unidentified files, creating a child task per file."""
    from library import get_files_to_identify
    from db import get_library_id

    library_id = get_library_id(library_path)
    files_to_identify = get_files_to_identify(library_id)

    if not files_to_identify:
        logger.info(f'No files to identify in {library_path}.')
        _identify_library_done(library_path=library_path)
        return

    parent_id = _current_task_id
    for f in files_to_identify:
        create_child_task(parent_id, 'identify_file', {'filepath': f.filepath, 'file_id': f.id})
    db.session.commit()
    set_waiting_for_children()


@register_continuation('identify_library')
def _identify_library_done(**kwargs):
    enqueue_task('add_missing_apps')


@register_task('identify_file')
def identify_file_task(filepath, file_id, **kwargs):
    """Identify a single file and create Apps/Titles entries."""
    def _work():
        import titles as titles_lib
        from db import Files, Apps, get_file_from_db, add_title_id_in_db, get_title_id_db_id
        from db import get_app_by_id_and_version, add_file_to_app

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
                existing_app = get_app_by_id_and_version(file_content["app_id"], file_content["version"])

                if existing_app:
                    add_file_to_app(file_content["app_id"], file_content["version"], file_id)
                else:
                    new_app = Apps(
                        app_id=file_content["app_id"],
                        app_version=file_content["version"],
                        app_type=file_content["type"],
                        owned=True,
                        title_id=title_id_in_db
                    )
                    db.session.add(new_app)
                    db.session.flush()
                    file_obj = get_file_from_db(file_id)
                    if file_obj:
                        new_app.files.append(file_obj)
                nb_content += 1

            if nb_content > 1:
                file.multicontent = True
            file.nb_content = nb_content
            file.identified = True
        else:
            logger.warning(f"Error identifying file {file.filename}: {error}")
            file.identification_error = error
            file.identified = False

        file.identification_type = identification
        file.identification_attempts += 1
        file.last_attempt = datetime.datetime.now()
        db.session.commit()

    _with_titledb(_work)


@register_task('add_missing_apps')
def add_missing_apps_task(**kwargs):
    from library import add_missing_apps_to_db
    _with_titledb(add_missing_apps_to_db)
    enqueue_task('remove_missing_files')


@register_task('remove_missing_files')
def remove_missing_files_task(**kwargs):
    from db import remove_missing_files_from_db
    remove_missing_files_from_db()
    enqueue_task('update_titles')


@register_task('update_titles')
def update_titles_task(**kwargs):
    from library import update_titles
    update_titles()
    enqueue_task('organize_library')


@register_task('organize_library')
def organize_library_task(**kwargs):
    """Organize all identified files, creating a child task per file."""
    from settings import load_settings
    from db import get_libraries, Files

    app_settings = load_settings()
    organizer_settings = app_settings['library']['management']['organizer']

    if not organizer_settings['enabled']:
        _organize_library_done()
        return

    parent_id = _current_task_id
    libraries = get_libraries()
    has_children = False
    for library in libraries:
        identified_files = Files.query.filter_by(library_id=library.id, identified=True).all()
        for file_obj in identified_files:
            create_child_task(parent_id, 'organize_file', {
                'file_id': file_obj.id,
                'library_path': library.path,
                'organizer_settings': organizer_settings,
            })
            has_children = True
    db.session.commit()

    if not has_children:
        logger.info('No files to organize.')
        _organize_library_done()
        return

    set_waiting_for_children()


@register_continuation('organize_library')
def _organize_library_done(**kwargs):
    from settings import load_settings
    from db import get_libraries
    from utils import delete_empty_folders

    app_settings = load_settings()
    organizer_settings = app_settings['library']['management']['organizer']
    if organizer_settings.get('enabled') and organizer_settings.get('remove_empty_folders'):
        for library in get_libraries():
            delete_empty_folders(library.path)
    enqueue_task('remove_outdated_updates')


@register_task('organize_file')
def organize_file_task(file_id, library_path, organizer_settings, **kwargs):
    """Organize a single file."""
    def _work():
        from library import organize_file
        from db import Files

        file_obj = db.session.get(Files, file_id)
        if not file_obj:
            return
        organize_file(file_obj, library_path, organizer_settings)

    _with_titledb(_work)


@register_task('remove_outdated_updates')
def remove_outdated_updates_task(**kwargs):
    """Remove outdated update files."""
    from settings import load_settings
    from library import remove_outdated_update_files

    app_settings = load_settings()
    if app_settings['library']['management']['delete_older_updates']:
        _with_titledb(remove_outdated_update_files)
    enqueue_task('generate_library')


@register_task('generate_library')
def generate_library_task(**kwargs):
    from library import generate_library
    generate_library()


# --- File event tasks ---

@register_task('handle_file_added')
def handle_file_added_task(library_path, filepath, **kwargs):
    from library import add_files_to_library
    add_files_to_library(library_path, [filepath])
    enqueue_task('identify_library', {'library_path': library_path})


@register_task('handle_file_moved')
def handle_file_moved_task(library_path, src_path, dest_path, **kwargs):
    from db import file_exists_in_db, update_file_path
    from library import add_files_to_library
    if file_exists_in_db(src_path):
        update_file_path(library_path, src_path, dest_path)
    else:
        add_files_to_library(library_path, [dest_path])
    enqueue_task('identify_library', {'library_path': library_path})


@register_task('handle_file_deleted')
def handle_file_deleted_task(filepath, **kwargs):
    from db import delete_file_by_filepath
    delete_file_by_filepath(filepath)
    enqueue_task('update_titles')
