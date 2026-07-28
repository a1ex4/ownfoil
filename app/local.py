"""Local entrypoint used by the `ownfoil` uv tool.

Unlike run.py, this avoids Gunicorn (which requires Unix's os.fork and cannot
run on Windows) and instead serves the app with Werkzeug's built-in server,
so the same command works cross-platform for local/dev use.
"""
import logging

logger = logging.getLogger('main')


def main():
    import app as app_mod
    from app import app, init
    from db import init_db
    from auth import init_users
    from settings import get_settings
    from worker_pool import WorkerPool

    logger.info('Starting initialization of Ownfoil...')

    init_db(app)
    init_users(app)
    with app.app_context():
        from tasks import cleanup_tasks
        cleanup_tasks()

    init()
    initial_count = max(1, get_settings().get('worker', {}).get('count', 1))
    app_mod.pool = WorkerPool(app, initial_count=initial_count)

    try:
        logger.info('Initialization done, starting local server on http://0.0.0.0:8465 ...')
        app.run(host='0.0.0.0', port=8465, threaded=True, use_reloader=False)
    finally:
        app_mod.pool.shutdown()
        if app_mod.watcher is not None:
            app_mod.watcher.stop()


if __name__ == '__main__':
    main()
