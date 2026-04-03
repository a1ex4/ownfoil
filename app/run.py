"""Production entrypoint — Gunicorn HTTP server + task worker pool."""
import logging
import os
import sys
import threading
import time
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


def _watch_settings(pool, config_file, check_interval=2.0, stop_event=None):
    """Thread that monitors settings.yaml and adjusts worker pool size."""
    import yaml
    last_mtime = None
    while not (stop_event and stop_event.is_set()):
        try:
            if os.path.exists(config_file):
                mtime = os.path.getmtime(config_file)
                if mtime != last_mtime:
                    last_mtime = mtime
                    with open(config_file, 'r') as f:
                        settings = yaml.safe_load(f) or {}
                    desired = settings.get('worker', {}).get('count', 1)
                    max_workers = os.cpu_count() or 1
                    desired = max(1, min(desired, max_workers))
                    if desired != pool.count:
                        logger.info(f'Settings changed: scaling workers from {pool.count} to {desired}')
                        pool.scale(desired)
        except Exception as e:
            logger.error(f'Error watching settings: {e}')
        if stop_event:
            stop_event.wait(check_interval)
        else:
            time.sleep(check_interval)


logger = logging.getLogger('main')


def main():
    from app import app, init
    from db import init_db
    from auth import init_users
    from constants import CONFIG_FILE
    from settings import load_settings

    logger.info('Starting initialization of Ownfoil...')

    init_db(app)
    init_users(app)
    with app.app_context():
        from tasks import cleanup_tasks
        cleanup_tasks()

    # Read initial worker count from settings
    with app.app_context():
        settings = load_settings()
    initial_worker_count = settings.get('worker', {}).get('count', 1)
    max_workers = os.cpu_count() or 1
    initial_worker_count = max(1, min(initial_worker_count, max_workers))

    # Start worker pool
    master_pid = os.getpid()
    pool = WorkerPool(initial_count=initial_worker_count)

    # Start settings watcher thread
    watcher_stop = threading.Event()
    settings_watcher = threading.Thread(
        target=_watch_settings,
        args=(pool, CONFIG_FILE, 2.0, watcher_stop),
        daemon=True,
    )
    settings_watcher.start()

    def post_fork(server, worker):
        """Clear inherited multiprocessing children so atexit doesn't try to join them."""
        import multiprocessing.process as mp_process
        mp_process._children = set()

    def post_worker_init(worker):
        """Start watcher inside the Gunicorn worker process."""
        with app.app_context():
            from db import db
            db.engine.dispose()
        init()

    def worker_exit(server, worker):
        """Clean shutdown of watcher when Gunicorn worker exits."""
        from app import watcher
        if watcher:
            watcher.stop()

    def on_exit(server):
        """Stop task worker pool when Gunicorn master exits."""
        if os.getpid() != master_pid:
            return
        watcher_stop.set()
        pool.shutdown()
        logger.debug('Task worker pool terminated.')

    options = {
        'bind': '0.0.0.0:8465',
        'workers': 1,
        'worker_class': 'gthread',
        'threads': 4,
        'accesslog': '-',
        'post_fork': post_fork,
        'post_worker_init': post_worker_init,
        'worker_exit': worker_exit,
        'on_exit': on_exit,
    }

    logger.info('Initialization done, starting Gunicorn server...')
    OwnfoilServer(app, options).run()


if __name__ == '__main__':
    main()
