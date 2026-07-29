"""Local entrypoint used by the `ownfoil` uv tool.

Unlike run.py, this avoids Gunicorn (which requires Unix's os.fork and cannot
run on Windows) and instead serves the app with Werkzeug's built-in server,
so the same command works cross-platform for local/dev use.
"""
import logging
import webbrowser

from werkzeug.serving import make_server

logger = logging.getLogger('main')

LOCAL_URL = 'http://127.0.0.1:8465'


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

    # make_server returns once the socket is listening, so a browser opened right after it
    # can never race the bind - anything early just waits in the accept backlog.
    server = make_server('0.0.0.0', 8465, app, threaded=True)
    try:
        logger.info('Initialization done, starting local server on http://0.0.0.0:8465 ...')
        if open_browser:
            _open_ui(LOCAL_URL)
        server.serve_forever()
    finally:
        server.server_close()
        app_mod.pool.shutdown()
        if app_mod.watcher is not None:
            app_mod.watcher.stop()


if __name__ == '__main__':
    main()
