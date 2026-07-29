"""Local entrypoint used by the `ownfoil` uv tool.

Unlike run.py, this avoids Gunicorn (which requires Unix's os.fork and cannot
run on Windows) and instead serves the app with Werkzeug's built-in server,
so the same command works cross-platform for local/dev use.
"""
import logging
import webbrowser

logger = logging.getLogger('main')

HOST = '0.0.0.0'
PORT = 8465
LOCAL_URL = f'http://127.0.0.1:{PORT}'


def _open_ui(url):
    """Open the Web UI in the default browser, never failing the server if it can't."""
    try:
        if webbrowser.open(url):
            return
    except Exception:
        pass
    logger.warning(f'Could not open a browser automatically, go to {url} manually.')


def main(open_browser=False):
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
        logger.info(f'Initialization done, starting local server on http://{HOST}:{PORT} ...')
        if open_browser:
            _open_ui(LOCAL_URL)
        app.run(host=HOST, port=PORT, threaded=True, use_reloader=False)
    finally:
        app_mod.pool.shutdown()
        if app_mod.watcher is not None:
            app_mod.watcher.stop()


if __name__ == '__main__':
    main()
