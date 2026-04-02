"""Task queue model, registry, and helpers."""
import hashlib
import json
import datetime
import logging
from db import db

logger = logging.getLogger('main')

# --- Task Registry ---
TASK_REGISTRY = {}


def register_task(name):
    """Decorator to register a callable as a named task."""
    def decorator(func):
        TASK_REGISTRY[name] = func
        return func
    return decorator


def get_registered_task(name):
    return TASK_REGISTRY.get(name)


# --- Task Model ---
class Task(db.Model):
    __tablename__ = 'tasks'

    id = db.Column(db.Integer, primary_key=True)
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

    __table_args__ = (
        db.Index('ix_tasks_status_created', 'status', 'created_at'),
    )


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
            "SELECT id FROM tasks WHERE task_name = ? AND input_hash = ? AND status IN ('pending', 'running')",
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


# --- Built-in test tasks ---

@register_task('echo')
def echo_task(**kwargs):
    return kwargs


@register_task('sleep')
def sleep_task(seconds=1, **kwargs):
    import time
    time.sleep(seconds)
    return {'slept': seconds}
