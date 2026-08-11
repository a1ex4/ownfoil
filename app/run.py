"""Production entrypoint — Gunicorn HTTP server + task worker pool."""
import logging
from gunicorn.app.base import BaseApplication
from gunicorn.glogging import Logger as GunicornLogger

from utils import ColoredFormatter, LOG_FORMAT, LOG_DATEFMT
from worker_pool import WorkerPool


class OwnfoilLogger(GunicornLogger):
    """Route Gunicorn's error/access logs through the app's colored format and drop SIGWINCH noise."""

    def setup(self, cfg):
        super().setup(cfg)
        # Show a fixed '(gunicorn)' source; the real module would just be 'glogging'.
        formatter = ColoredFormatter(LOG_FORMAT.replace('%(module)s', 'gunicorn'), datefmt=LOG_DATEFMT)
        for log in (self.error_log, self.access_log):
            for handler in log.handlers:
                handler.setFormatter(formatter)
        self.error_log.addFilter(lambda record: record.getMessage() != 'Handling signal: winch')


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
        app_mod.pool = WorkerPool(app, initial_count=initial_count)

    def worker_exit(server, worker):
        """Stop watcher and worker pool when Gunicorn worker exits."""
        from realtime import stop as stop_realtime
        stop_realtime()
        if app_mod.pool is not None:
            app_mod.pool.shutdown()
        if app_mod.watcher is not None:
            app_mod.watcher.stop()

    options = {
        'bind': '0.0.0.0:8465',
        'workers': 1,
        'worker_class': 'gthread',
        # Each open realtime WebSocket pins a thread for its lifetime, so the pool has to
        # leave room for ordinary requests alongside every watching browser tab.
        'threads': 16,
        'accesslog': '-',
        'access_log_format': 'Handled request: %(h)s %(u)s %(s)s %(L)ss %(m)s %(U)s',
        'logger_class': OwnfoilLogger,
        'proc_name': 'ownfoil',
        'post_fork': post_fork,
        'post_worker_init': post_worker_init,
        'worker_exit': worker_exit,
    }

    logger.info('Initialization done, starting Gunicorn server...')
    OwnfoilServer(app, options).run()


if __name__ == '__main__':
    main()
