"""Local entrypoint used by the `ownfoil` uv tool.

Unlike run.py, this avoids Gunicorn (which requires Unix's os.fork and cannot
run on Windows) and instead serves the app with Werkzeug's built-in server,
so the same command works cross-platform for local/dev use.
"""
import logging
import webbrowser

from werkzeug.serving import make_server

logger = logging.getLogger('main')

HOST = '0.0.0.0'
PORT = 8465


def _ui_url():
    """URL to open, preferring the LAN IP since loopback can be unreachable on Windows."""
    from utils import get_lan_ip
    return f'http://{get_lan_ip() or "127.0.0.1"}:{PORT}'


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

    server = None
    try:
        logger.info(f'Initialization done, starting local server on http://{HOST}:{PORT} ...')
        # Built before serving so the socket is already listening: a browser opened here
        # cannot race the bind, it just waits in the accept backlog.
        server = make_server(HOST, PORT, app, threaded=True)
        server.log_startup()
        if open_browser:
            _open_ui(_ui_url())
        server.serve_forever()
    finally:
        # make_server exits the process on a bind failure, so guard the cleanup below.
        from realtime import stop as stop_realtime
        stop_realtime()
        if server is not None:
            server.server_close()
        app_mod.pool.shutdown()
        if app_mod.watcher is not None:
            app_mod.watcher.stop()


if __name__ == '__main__':
    main()
