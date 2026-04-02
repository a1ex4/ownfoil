"""Production entrypoint — Gunicorn HTTP server + task worker + watcher + scheduler."""
import logging
import os
import sys
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


def main():
    from app import app, init
    from db import init_db
    from auth import init_users
    from worker import start_worker_process

    logger = logging.getLogger('main')
    logger.info('Starting initialization of Ownfoil...')

    init_db(app)
    init_users(app)
    with app.app_context():
        from tasks import cleanup_tasks
        cleanup_tasks()

    # Start task worker subprocess (managed by arbiter)
    master_pid = os.getpid()
    worker_stop_event = MPEvent()
    worker_process = Process(
        target=start_worker_process, args=(worker_stop_event,)
    )
    worker_process.start()
    logger.info('Worker process started.')

    def post_fork(server, worker):
        """Clear inherited multiprocessing children so atexit doesn't try to join them."""
        import multiprocessing.process as mp_process
        mp_process._children = set()

    def post_worker_init(worker):
        """Start watcher and scheduler inside the Gunicorn worker process."""
        with app.app_context():
            from db import db
            db.engine.dispose()
        init()

    def worker_exit(server, worker):
        """Clean shutdown of watcher and scheduler when Gunicorn worker exits."""
        from app import watcher
        watcher.stop()
        app.scheduler.shutdown()

    def on_exit(server):
        """Stop task worker subprocess when Gunicorn master exits."""
        if os.getpid() != master_pid:
            return
        worker_stop_event.set()
        worker_process.join(timeout=10)
        if worker_process.is_alive():
            worker_process.terminate()
        logger.debug('Task worker process terminated.')

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
