"""Task worker process — polls the tasks table and executes claimed tasks."""
import json
import datetime
import logging
import sys
from multiprocessing import Event

logger = logging.getLogger('worker')


class TaskWorker:
    def __init__(self, app, poll_interval=2.0, stop_event=None, worker_id=1):
        self.app = app
        self.poll_interval = poll_interval
        self.stop_event = stop_event or Event()
        self.worker_id = worker_id

    def claim_task(self):
        """Atomically claim the oldest pending task. Returns task_id or None."""
        from db import db
        connection = db.engine.raw_connection()
        try:
            cursor = connection.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
                "SELECT id FROM tasks WHERE status = 'pending' "
                "AND (run_after IS NULL OR run_after <= datetime('now')) "
                "ORDER BY created_at ASC LIMIT 1"
            )
            row = cursor.fetchone()
            if row is None:
                connection.commit()
                return None

            task_id = row[0]
            now = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute(
                "UPDATE tasks SET status = 'running', started_at = ? WHERE id = ? AND status = 'pending'",
                (now, task_id)
            )
            if cursor.rowcount == 0:
                connection.commit()
                return None

            connection.commit()
            return task_id
        except Exception as e:
            connection.rollback()
            logger.error(f"Error claiming task: {e}")
            return None
        finally:
            connection.close()

    def execute_task(self, task_id):
        from tasks import get_registered_task, on_task_completed
        from db import db, Task
        from settings import get_settings
        import tasks as tasks_mod

        # Refresh settings + prime Keys.keys_loaded before each task
        get_settings()

        task = db.session.get(Task, task_id)
        task_func = get_registered_task(task.task_name)
        input_data = json.loads(task.input_json) if task.input_json else {}

        try:
            tasks_mod._current_task_id = task_id
            result = task_func(**input_data)
            tasks_mod._current_task_id = None

            # Re-read task — function may have set waiting_for_children
            db.session.expire(task)
            task = db.session.get(Task, task_id)

            if task.status == 'waiting_for_children':
                return

            task.status = 'completed'
            task.completion_pct = 100
            task.exit_code = 0
            task.output_json = json.dumps(result) if result else None
            task.completed_at = datetime.datetime.utcnow()
            parent_id = task.parent_id
            db.session.commit()
            on_task_completed(task_id, parent_id)
            # Delete completed non-parent tasks (parent+children are cleaned up in _try_complete_parent)
            if not parent_id:
                db.session.delete(task)
                db.session.commit()
        except Exception as e:
            tasks_mod._current_task_id = None
            logger.error(f"Task {task_id} failed: {e}")
            db.session.rollback()
            task = db.session.get(Task, task_id)
            task.status = 'failed'
            task.error_message = str(e)
            task.exit_code = 1
            task.completed_at = datetime.datetime.utcnow()
            parent_id = task.parent_id
            db.session.commit()
            on_task_completed(task_id, parent_id)

    def run(self):
        with self.app.app_context():
            logger.info(f"Worker started, polling every {self.poll_interval}s")
            while not self.stop_event.is_set():
                task_id = self.claim_task()
                if task_id is not None:
                    self.execute_task(task_id)
                else:
                    self.stop_event.wait(self.poll_interval)
            logger.info("Worker stopped")


def start_worker_process(stop_event, worker_id=1):
    """Entry point for the worker subprocess."""
    import signal
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    from app import create_app
    import tasks  # noqa: F401 — registers @register_task decorators

    from utils import ColoredFormatter
    grey = '\033[90m'
    reset = '\033[0m'
    formatter = ColoredFormatter(
        f'[%(asctime)s.%(msecs)03d] %(levelname)s (%(module)s) {grey}worker-{worker_id}{reset} %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)

    app = create_app()
    worker = TaskWorker(app, poll_interval=2.0, stop_event=stop_event, worker_id=worker_id)
    worker.run()
