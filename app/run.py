"""Production entrypoint — Gunicorn HTTP server + task worker pool."""
import logging
import threading
from multiprocessing import Process, Event as MPEvent
from gunicorn.app.base import BaseApplication


class OwnfoilServer(BaseApplication):
    def __init__(self, application, options=None):
        self.options = options or {}
        self.application = application
        super().__init__()

    def load_config(self):
        for key, value in self.options.items():
            if key in self.cfg.settings and value is not None:
                self.cfg.set(key.lower(), value)

    def load(self):
        return self.application


class WorkerPool:
    """Manages a dynamic pool of task worker subprocesses."""

    def __init__(self, initial_count=1):
        self.workers = {}  # worker_id -> (Process, MPEvent)
        self._lock = threading.Lock()
        self._next_id = 1
        self._scale_to(initial_count)

    def _start_worker(self):
        """Start a single worker process with the next available ID."""
        from worker import start_worker_process
        worker_id = self._next_id
        self._next_id += 1
        stop_event = MPEvent()
        proc = Process(target=start_worker_process, args=(stop_event, worker_id))
        proc.start()
        self.workers[worker_id] = (proc, stop_event)
        logger.info(f'Worker-{worker_id} started (pid={proc.pid}).')
        return worker_id

    def _stop_worker(self, worker_id):
        """Gracefully stop a worker by ID."""
        if worker_id not in self.workers:
            return
        proc, stop_event = self.workers.pop(worker_id)
        stop_event.set()
        proc.join(timeout=10)
        if proc.is_alive():
            proc.terminate()
        logger.info(f'Worker-{worker_id} stopped.')

    def _scale_to(self, desired_count):
        """Scale the pool to the desired number of workers."""
        current = len(self.workers)
        if desired_count > current:
            for _ in range(desired_count - current):
                self._start_worker()
        elif desired_count < current:
            # Stop the highest-numbered workers
            ids_to_stop = sorted(self.workers.keys(), reverse=True)[:current - desired_count]
            for wid in ids_to_stop:
                self._stop_worker(wid)

    def scale(self, desired_count):
        """Thread-safe scaling."""
        with self._lock:
            self._scale_to(desired_count)

    def shutdown(self):
        """Stop all workers."""
        with self._lock:
            for wid in list(self.workers.keys()):
                self._stop_worker(wid)

    @property
    def count(self):
        return len(self.workers)


logger = logging.getLogger('main')


def main():
    import app as app_mod
    from app import app, init
    from db import init_db
    from auth import init_users
    from settings import get_settings

    logger.info('Starting initialization of Ownfoil...')

    init_db(app)
    init_users(app)
    with app.app_context():
        from tasks import cleanup_tasks
        cleanup_tasks()

    def post_fork(server, worker):
        """Clear inherited multiprocessing children so atexit doesn't try to join them."""
        import multiprocessing.process as mp_process
        mp_process._children = set()

    def post_worker_init(worker):
        """Start file watcher and task worker pool inside the Gunicorn worker process."""
        with app.app_context():
            from db import db
            db.engine.dispose()
        init()
        # Start worker pool and expose it to app module so on_settings_change can scale it
        initial_count = max(1, get_settings().get('worker', {}).get('count', 1))
        app_mod.pool = WorkerPool(initial_count=initial_count)

    def worker_exit(server, worker):
        """Stop watcher and worker pool when Gunicorn worker exits."""
        if app_mod.pool is not None:
            app_mod.pool.shutdown()
        if app_mod.watcher is not None:
            app_mod.watcher.stop()

    options = {
        'bind': '0.0.0.0:8465',
        'workers': 1,
        'worker_class': 'gthread',
        'threads': 4,
        'accesslog': '-',
        'post_fork': post_fork,
        'post_worker_init': post_worker_init,
        'worker_exit': worker_exit,
    }

    logger.info('Initialization done, starting Gunicorn server...')
    OwnfoilServer(app, options).run()


if __name__ == '__main__':
    main()
