from flask import Flask, render_template, request, redirect, url_for, jsonify, send_from_directory, Response, has_app_context, has_request_context
from flask_login import LoginManager
from werkzeug.utils import secure_filename
from sqlalchemy import func
from sqlalchemy.orm import joinedload
from scheduler import init_scheduler
from functools import wraps
from file_watcher import Watcher
import threading
import logging
import sys
import copy
import flask.cli
from datetime import timedelta
flask.cli.show_server_banner = lambda *args: None
from constants import *
from settings import *
from downloads import ProwlarrClient, test_torrent_client, run_downloads_job, manual_search_update, queue_download_url, search_update_options, check_completed_downloads, get_downloads_state
from db import *
from shop import *
from auth import *
import titles
from utils import *
from library import *
from library import _get_nsz_exe, _ensure_unique_path
import titledb
from title_requests import create_title_request, list_requests
import requests
import os
import threading
import time
import uuid
import re

from db import add_access_event, get_access_events

try:
    from PIL import Image, ImageOps
except Exception:
    Image = None
    ImageOps = None

# In-process media cache index.
# Avoids repeated os.listdir() and TitleDB lookups for icons/banners that are already cached.
_media_cache_lock = threading.Lock()
_media_cache_index = {
    'icon': {},   # title_id -> filename
    'banner': {}, # title_id -> filename
}

_media_resize_lock = threading.Lock()

_ICON_SIZE = (300, 300)
_BANNER_SIZE = (920, 520)

def _media_variant_dirname(media_kind):
    if media_kind == 'icon':
        return f"icons_{_ICON_SIZE[0]}x{_ICON_SIZE[1]}"
    return f"banners_{_BANNER_SIZE[0]}x{_BANNER_SIZE[1]}"

def _is_jpeg_name(filename):
    return str(filename).lower().endswith(('.jpg', '.jpeg'))

def _resize_image_to_path(src_path, dest_path, size, quality=85):
    if not Image or not ImageOps:
        return False

    try:
        with Image.open(src_path) as im:
            # Normalize orientation if EXIF is present.
            im = ImageOps.exif_transpose(im)
            fitted = ImageOps.fit(im, size, method=Image.Resampling.LANCZOS)

            os.makedirs(os.path.dirname(dest_path), exist_ok=True)

            if _is_jpeg_name(dest_path):
                if fitted.mode not in ('RGB',):
                    fitted = fitted.convert('RGB')
                fitted.save(dest_path, format='JPEG', quality=quality, optimize=True, progressive=True)
            else:
                # PNG etc.
                fitted.save(dest_path, optimize=True)
        return True
    except Exception:
        return False

def _get_variant_path(cache_dir, cached_name, media_kind):
    if not cached_name:
        return None
    size = _ICON_SIZE if media_kind == 'icon' else _BANNER_SIZE
    variant_dir = os.path.join(CACHE_DIR, _media_variant_dirname(media_kind))
    variant_path = os.path.join(variant_dir, cached_name)
    return size, variant_dir, variant_path

def _get_cached_media_filename(cache_dir, title_id, media_kind='icon'):
    """Return cached filename for title_id if present on disk."""
    title_id = (title_id or '').upper()
    if not title_id:
        return None

    with _media_cache_lock:
        cached_name = _media_cache_index.get(media_kind, {}).get(title_id)
    if cached_name:
        path = os.path.join(cache_dir, cached_name)
        if os.path.exists(path):
            return cached_name
        with _media_cache_lock:
            _media_cache_index.get(media_kind, {}).pop(title_id, None)

    try:
        for name in os.listdir(cache_dir):
            if name.startswith(f"{title_id}."):
                with _media_cache_lock:
                    _media_cache_index.setdefault(media_kind, {})[title_id] = name
                return name
    except Exception:
        return None
    return None

def _remember_cached_media_filename(title_id, filename, media_kind='icon'):
    title_id = (title_id or '').upper()
    if not title_id or not filename:
        return
    with _media_cache_lock:
        _media_cache_index.setdefault(media_kind, {})[title_id] = filename

def _ensure_cached_media_file(cache_dir, title_id, remote_url):
    """Compute local cache name/path from remote_url."""
    if not remote_url:
        return None, None
    url = remote_url
    if url.startswith('//'):
        url = 'https:' + url
    clean_url = url.split('?', 1)[0]
    _, ext = os.path.splitext(clean_url)
    if not ext:
        ext = '.jpg'
    cache_name = f"{title_id.upper()}{ext}"
    cache_path = os.path.join(cache_dir, cache_name)
    return cache_name, cache_path
import json

def init():
    global watcher
    global watcher_thread
    # Create and start the file watcher
    logger.info('Initializing File Watcher...')
    watcher = Watcher(on_library_change)
    watcher_thread = threading.Thread(target=watcher.run)
    watcher_thread.daemon = True
    watcher_thread.start()

    # Load initial configuration
    logger.info('Loading initial configuration...')
    reload_conf()

    # init libraries
    library_paths = app_settings['library']['paths']
    init_libraries(app, watcher, library_paths)

    # Initialize job scheduler
    logger.info('Initializing Scheduler...')
    init_scheduler(app)

    def downloads_job():
        run_downloads_job(scan_cb=scan_library, post_cb=post_library_change)

    # Automatic update downloader job
    app.scheduler.add_job(
        job_id='downloads_update_job',
        func=downloads_job,
        interval=timedelta(minutes=5)
    )
    
    # Define update_titledb_job
    def update_titledb_job():
        global is_titledb_update_running
        with titledb_update_lock:
            is_titledb_update_running = True
        logger.info("Starting TitleDB update job...")
        try:
            current_settings = load_settings()
            titledb.update_titledb(current_settings)
            logger.info("TitleDB update job completed.")
        except Exception as e:
            logger.error(f"Error during TitleDB update job: {e}")
        finally:
            with titledb_update_lock:
                is_titledb_update_running = False
        
    # Define scan_library_job
    def scan_library_job():
        global is_titledb_update_running
        with titledb_update_lock:
            if is_titledb_update_running:
                logger.info("Skipping scheduled library scan: update_titledb job is currently in progress. Rescheduling in 5 minutes.")
                # Reschedule the job for 5 minutes later
                app.scheduler.add_job(
                    job_id=f'scan_library_rescheduled_{datetime.now().timestamp()}', # Unique ID
                    func=scan_library_job,
                    run_once=True,
                    start_date=datetime.now().replace(microsecond=0) + timedelta(minutes=5)
                )
                return
        logger.info("Starting scheduled library scan job...")
        global scan_in_progress
        with scan_lock:
            if scan_in_progress:
                logger.info(f'Skipping scheduled library scan: scan already in progress.')
                return # Skip the scan if already in progress
            scan_in_progress = True
        try:
            scan_library()
            post_library_change()
            logger.info("Scheduled library scan job completed.")
        except Exception as e:
            logger.error(f"Error during scheduled library scan job: {e}")
        finally:
            with scan_lock:
                scan_in_progress = False

    # Update job: run update_titledb then scan_library once on startup
    def update_db_and_scan_job():
        logger.info("Running update job (TitleDB update and library scan)...")
        update_titledb_job() # This will set/reset the flag
        scan_library_job() # This will check the flag and run if update_titledb_job is done
        logger.info("Update job completed.")

    # Schedule the update job to run immediately and only once
    app.scheduler.add_job(
        job_id='update_db_and_scan',
        func=update_db_and_scan_job,
        interval=timedelta(hours=2),
        run_first=True
    )

os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

## Global variables
app_settings = {}
# Create a global variable and lock for scan_in_progress
scan_in_progress = False
scan_lock = threading.Lock()
# Global flag for titledb update status
is_titledb_update_running = False
titledb_update_lock = threading.Lock()
conversion_jobs = {}
conversion_jobs_lock = threading.Lock()
conversion_job_limit = 50
library_rebuild_status = {
    'in_progress': False,
    'started_at': 0,
    'updated_at': 0
}
library_rebuild_lock = threading.Lock()
shop_sections_cache = {
    'limit': None,
    'timestamp': 0,
    'payload': None
}
shop_sections_cache_lock = threading.Lock()
shop_sections_refresh_lock = threading.Lock()
shop_sections_refresh_running = False

def _load_shop_sections_cache_from_disk():
    cache_path = SHOP_SECTIONS_CACHE_FILE
    if not os.path.exists(cache_path):
        return None
    try:
        with open(cache_path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if 'payload' not in data or 'timestamp' not in data or 'limit' not in data:
        return None
    return data

def _save_shop_sections_cache_to_disk(payload, limit, timestamp):
    cache_path = SHOP_SECTIONS_CACHE_FILE
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    data = {
        'payload': payload,
        'limit': limit,
        'timestamp': timestamp
    }
    try:
        with open(cache_path, 'w', encoding='utf-8') as handle:
            json.dump(data, handle)
    except Exception:
        pass

def _build_shop_sections_payload(limit):
    titles.load_titledb()

    apps = Apps.query.options(
        joinedload(Apps.files),
        joinedload(Apps.title)
    ).filter_by(owned=True).all()

    def _safe_int(value, default=0):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _select_file(app):
        if not app.files:
            return None
        return max(app.files, key=lambda f: f.size or 0)

    def _build_item(app):
        file_obj = _select_file(app)
        if not file_obj:
            return None
        title_id = app.title.title_id if app.title else None
        base_info = titles.get_game_info(title_id) if title_id else None
        app_info = None
        if app.app_type == APP_TYPE_DLC:
            app_info = titles.get_game_info(app.app_id)
        name = (app_info or base_info or {}).get('name') or app.app_id
        title_name = (base_info or {}).get('name') or name
        icon_url = f'/api/shop/icon/{title_id}' if title_id else ''
        return {
            'name': name,
            'title_name': title_name,
            'title_id': title_id,
            'app_id': app.app_id,
            'app_version': app.app_version,
            'app_type': app.app_type,
            'category': (base_info or {}).get('category', ''),
            'icon_url': icon_url,
            'url': f'/api/get_game/{file_obj.id}#{file_obj.filename}',
            'size': file_obj.size or 0,
            'file_id': file_obj.id,
            'filename': file_obj.filename,
            'download_count': file_obj.download_count or 0
        }

    base_apps = [app for app in apps if app.app_type == APP_TYPE_BASE]
    update_apps = [app for app in apps if app.app_type == APP_TYPE_UPD]
    dlc_apps = [app for app in apps if app.app_type == APP_TYPE_DLC]

    base_items = [item for item in (_build_item(app) for app in base_apps) if item]
    base_items.sort(key=lambda item: item['file_id'], reverse=True)
    new_items = base_items[:limit]

    recommended_items = sorted(base_items, key=lambda item: item['download_count'], reverse=True)[:limit]
    if not any(item['download_count'] for item in recommended_items):
        recommended_items = new_items[:limit]

    latest_available_update_by_title = {}
    for app in update_apps:
        title_id = app.title.title_id if app.title else None
        if not title_id:
            continue
        version = _safe_int(app.app_version)
        current_available = latest_available_update_by_title.get(title_id)
        if not current_available or version > current_available['version']:
            latest_available_update_by_title[title_id] = {'version': version, 'app': app}

    update_items_full = []
    for title_id, available in latest_available_update_by_title.items():
        item = _build_item(available['app'])
        if item:
            update_items_full.append(item)
    update_items_full.sort(key=lambda item: _safe_int(item['app_version']), reverse=True)
    update_items = update_items_full

    dlc_by_id = {}
    for app in dlc_apps:
        version = _safe_int(app.app_version)
        current = dlc_by_id.get(app.app_id)
        if not current or version > current['version']:
            dlc_by_id[app.app_id] = {'version': version, 'app': app}
    dlc_items_full = [item for item in (_build_item(entry['app']) for entry in dlc_by_id.values()) if item]
    dlc_items_full.sort(key=lambda item: _safe_int(item['app_version']), reverse=True)
    dlc_items = dlc_items_full[:limit]

    all_items = sorted(base_items + update_items_full + dlc_items_full, key=lambda item: item['name'].lower())

    titles.unload_titledb()

    return {
        'sections': [
            {'id': 'new', 'title': 'New', 'items': new_items},
            {'id': 'recommended', 'title': 'Recommended', 'items': recommended_items},
            {'id': 'updates', 'title': 'Updates', 'items': update_items},
            {'id': 'dlc', 'title': 'DLC', 'items': dlc_items},
            {'id': 'all', 'title': 'All', 'items': all_items}
        ]
    }

def _refresh_shop_sections_cache(limit):
    global shop_sections_refresh_running
    with shop_sections_refresh_lock:
        if shop_sections_refresh_running:
            return
        shop_sections_refresh_running = True

    def _run():
        global shop_sections_refresh_running
        try:
            with app.app_context():
                now = time.time()
                payload = _build_shop_sections_payload(limit)
                with shop_sections_cache_lock:
                    shop_sections_cache['payload'] = payload
                    shop_sections_cache['limit'] = limit
                    shop_sections_cache['timestamp'] = now
                _save_shop_sections_cache_to_disk(payload, limit, now)
        finally:
            with shop_sections_refresh_lock:
                shop_sections_refresh_running = False

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

# Configure logging
formatter = ColoredFormatter(
    '[%(asctime)s.%(msecs)03d] %(levelname)s (%(module)s) %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(formatter)

logging.basicConfig(
    level=logging.INFO,
    handlers=[handler]
)

# Create main logger
logger = logging.getLogger('main')
logger.setLevel(logging.DEBUG)

# Apply filter to hide date from http access logs
logging.getLogger('werkzeug').addFilter(FilterRemoveDateFromWerkzeugLogs())

# Suppress specific Alembic INFO logs
logging.getLogger('alembic.runtime.migration').setLevel(logging.WARNING)

@login_manager.user_loader
def load_user(user_id):
    # since the user_id is just the primary key of our user table, use it in the query for the user
    return User.query.filter_by(id=user_id).first()

def reload_conf():
    global app_settings
    global watcher
    app_settings = load_settings()

def on_library_change(events):
    # TODO refactor: group modified and created together
    with app.app_context():
        created_events = [e for e in events if e.type == 'created']
        modified_events = [e for e in events if e.type != 'created']

        for event in modified_events:
            if event.type == 'moved':
                moved_outside_library = not event.dest_path or not event.dest_path.startswith(event.directory)
                if moved_outside_library:
                    delete_file_by_filepath(event.src_path)
                    continue
                if file_exists_in_db(event.src_path):
                    # update the path
                    update_file_path(event.directory, event.src_path, event.dest_path)
                else:
                    # add to the database
                    event.src_path = event.dest_path
                    created_events.append(event)

            elif event.type == 'deleted':
                # delete the file from library if it exists
                delete_file_by_filepath(event.src_path)

            elif event.type == 'modified':
                # can happen if file copy has started before the app was running
                add_files_to_library(event.directory, [event.src_path])

        if created_events:
            directories = list(set(e.directory for e in created_events))
            for library_path in directories:
                new_files = [e.src_path for e in created_events if e.directory == library_path]
                add_files_to_library(library_path, new_files)

    post_library_change()

def create_app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = OWNFOIL_DB
    # TODO: generate random secret_key
    app.config['SECRET_KEY'] = '8accb915665f11dfa15c2db1a4e8026905f57716'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)

    app.register_blueprint(auth_blueprint)

    return app

# Create app
app = create_app()


@app.before_request
def _block_frozen_web_ui():
    """Frozen accounts should only see the MOTD page in the web UI."""
    try:
        # Let Tinfoil/Cyberfoil flows be handled by tinfoil_access.
        if all(header in request.headers for header in TINFOIL_HEADERS):
            return None

        if not current_user.is_authenticated:
            return None

        if not bool(getattr(current_user, 'frozen', False)):
            return None

        path = request.path or '/'
        if path.startswith('/static/'):
            return None
        if path in ('/login', '/logout'):
            return None

        message = (getattr(current_user, 'frozen_message', None) or '').strip() or 'Account is frozen.'
        if path.startswith('/api/'):
            return jsonify({'success': False, 'error': message}), 403
        return render_template('frozen.html', title='Library', message=message)
    except Exception:
        return None


_active_transfers_lock = threading.Lock()
_active_transfers = {}

_connected_clients_lock = threading.Lock()
_connected_clients = {}

_recent_access_lock = threading.Lock()
_recent_access = {}

_transfer_sessions_lock = threading.Lock()
_transfer_sessions = {}

_transfer_finalize_timers_lock = threading.Lock()
_transfer_finalize_timers = {}

_TRANSFER_FINALIZE_GRACE_S = 30


def _get_request_user():
    try:
        if current_user.is_authenticated:
            return current_user.user
    except Exception:
        return None
    auth = request.authorization
    if auth and auth.username:
        return auth.username
    return None


def _client_key():
    user = _get_request_user() or '-'
    remote = request.headers.get('X-Forwarded-For') or request.remote_addr or '-'
    ua = request.headers.get('User-Agent') or '-'
    return f"{user}|{remote}|{ua}"[:512]


def _touch_client():
    now = time.time()
    meta = {
        'last_seen_at': now,
        'user': _get_request_user(),
        'remote_addr': request.headers.get('X-Forwarded-For') or request.remote_addr,
        'user_agent': request.headers.get('User-Agent'),
    }
    key = _client_key()
    with _connected_clients_lock:
        existing = _connected_clients.get(key) or {}
        existing.update(meta)
        _connected_clients[key] = existing


def _is_cyberfoil_request():
    ua = request.headers.get('User-Agent') or ''
    return 'Cyberfoil' in ua


def _log_access(
    kind,
    title_id=None,
    file_id=None,
    filename=None,
    ok=True,
    status_code=200,
    duration_ms=None,
    bytes_sent=None,
    user=None,
    remote_addr=None,
    user_agent=None,
):
    if has_request_context():
        if user is None:
            user = _get_request_user()
        if remote_addr is None:
            remote_addr = request.headers.get('X-Forwarded-For') or request.remote_addr
        if user_agent is None:
            user_agent = request.headers.get('User-Agent')

    def _do_write():
        add_access_event(
            kind=kind,
            user=user,
            remote_addr=remote_addr,
            user_agent=user_agent,
            title_id=title_id,
            file_id=file_id,
            filename=filename,
            bytes_sent=bytes_sent,
            ok=ok,
            status_code=status_code,
            duration_ms=duration_ms,
        )

    try:
        if has_app_context():
            _do_write()
        else:
            # Streaming responses may call this outside request/app context.
            with app.app_context():
                _do_write()
    except Exception:
        try:
            logger.exception('Failed to log access event')
        except Exception:
            pass


def _log_access_dedup(kind, dedupe_key, window_s=15, **kwargs):
    now = time.time()
    key = f"{kind}|{dedupe_key}"[:512]

    with _recent_access_lock:
        last = _recent_access.get(key) or 0
        if now - last < float(window_s):
            return False
        _recent_access[key] = now
        if len(_recent_access) > 5000:
            ordered = sorted(_recent_access.items(), key=lambda kv: kv[1], reverse=True)
            _recent_access.clear()
            for k, ts in ordered[:2000]:
                _recent_access[k] = ts

    _log_access(kind=kind, **kwargs)
    return True


def _dedupe_history(items, window_s=3):
    out = []
    last_seen = {}
    for item in (items or []):
        at = item.get('at') or 0
        key = (
            item.get('kind'),
            item.get('user'),
            item.get('remote_addr'),
            item.get('title_id'),
            item.get('file_id'),
            item.get('filename'),
        )
        prev = last_seen.get(key)
        if prev is not None and abs(prev - at) <= window_s:
            continue
        last_seen[key] = at
        out.append(item)
    return out


def _transfer_session_key(user, remote_addr, user_agent, file_id):
    user = user or '-'
    remote = remote_addr or '-'
    ua = user_agent or '-'
    return f"{user}|{remote}|{ua}|{file_id}"[:512]


def _transfer_session_start(user, remote_addr, user_agent, title_id, file_id, filename, resp_status_code=None):
    now = time.time()
    key = _transfer_session_key(user, remote_addr, user_agent, file_id)
    created = False

    # If we had a pending finalize timer for this session, cancel it (resume / next range request).
    with _transfer_finalize_timers_lock:
        t = _transfer_finalize_timers.pop(key, None)
        if t:
            try:
                t.cancel()
            except Exception:
                pass

    with _transfer_sessions_lock:
        sess = _transfer_sessions.get(key)
        if not sess:
            sess = {
                'started_at': now,
                'last_seen_at': now,
                'open_streams': 0,
                'bytes_sent': 0,
                'bytes_sent_total': 0,
                'bytes_total': 0,
                'ok': True,
                'status_code': None,
                'user': user,
                'remote_addr': remote_addr,
                'user_agent': user_agent,
                'title_id': title_id,
                'file_id': file_id,
                'filename': filename,
            }
            _transfer_sessions[key] = sess
            created = True

        sess['last_seen_at'] = now
        sess['open_streams'] = int(sess.get('open_streams') or 0) + 1
        if title_id and not sess.get('title_id'):
            sess['title_id'] = title_id
        if filename and not sess.get('filename'):
            sess['filename'] = filename

        # Bound memory.
        if len(_transfer_sessions) > 2000:
            ordered = sorted(_transfer_sessions.items(), key=lambda kv: (kv[1] or {}).get('last_seen_at', 0), reverse=True)
            _transfer_sessions.clear()
            for k, v in ordered[:1000]:
                _transfer_sessions[k] = v

    if created:
        _log_access(
            kind='transfer_start',
            title_id=title_id,
            file_id=file_id,
            filename=filename,
            bytes_sent=0,
            ok=True,
            status_code=int(resp_status_code) if resp_status_code is not None else 200,
            duration_ms=0,
            user=user,
            remote_addr=remote_addr,
            user_agent=user_agent,
        )

    return key


def _transfer_session_progress(key, bytes_sent):
    now = time.time()
    with _transfer_sessions_lock:
        sess = _transfer_sessions.get(key)
        if not sess:
            return
        sess['last_seen_at'] = now
        if bytes_sent is not None:
            try:
                sess['bytes_sent'] = max(int(sess.get('bytes_sent') or 0), int(bytes_sent))
            except Exception:
                pass


def _transfer_session_finalize(key):
    # Timer callback; only finalize if no streams reopened during grace.
    sess = None
    with _transfer_sessions_lock:
        sess = _transfer_sessions.get(key)
        if not sess or int(sess.get('open_streams') or 0) != 0:
            return
        sess = _transfer_sessions.pop(key, sess)

    if not sess:
        return

    started_at = float(sess.get('started_at') or time.time())
    duration_ms = int((time.time() - started_at) * 1000)
    code = sess.get('status_code')
    try:
        code = int(code) if code is not None else None
    except Exception:
        code = None

    bytes_total = sess.get('bytes_sent_total')
    if bytes_total is None:
        bytes_total = sess.get('bytes_sent')
    try:
        bytes_total = int(bytes_total) if bytes_total is not None else None
    except Exception:
        bytes_total = None

    ok = bool(sess.get('ok'))
    _log_access(
        kind='transfer',
        title_id=sess.get('title_id'),
        file_id=sess.get('file_id'),
        filename=sess.get('filename'),
        bytes_sent=bytes_total,
        ok=ok if code is None else (ok and code < 400),
        status_code=code if code is not None else 0,
        duration_ms=duration_ms,
        user=sess.get('user'),
        remote_addr=sess.get('remote_addr'),
        user_agent=sess.get('user_agent'),
    )


def _transfer_session_finish(key, ok, status_code, bytes_sent):
    now = time.time()
    with _transfer_sessions_lock:
        sess = _transfer_sessions.get(key)
        if not sess:
            return
        sess['last_seen_at'] = now

        try:
            if bytes_sent is not None:
                bs = int(bytes_sent)
                # Sum response body sizes across sequential range requests.
                sess['bytes_sent_total'] = int(sess.get('bytes_sent_total') or 0) + bs
                # Keep per-response max for debugging / safety.
                sess['bytes_sent'] = max(int(sess.get('bytes_sent') or 0), bs)
        except Exception:
            pass

        try:
            sess['ok'] = bool(sess.get('ok')) and bool(ok)
        except Exception:
            pass

        if status_code is not None:
            try:
                sess['status_code'] = int(status_code)
            except Exception:
                pass

        sess['open_streams'] = max(0, int(sess.get('open_streams') or 0) - 1)
        if sess['open_streams'] != 0:
            return

    # Schedule finalize after grace to merge sequential range requests.
    timer = threading.Timer(_TRANSFER_FINALIZE_GRACE_S, _transfer_session_finalize, args=(key,))
    timer.daemon = True
    with _transfer_finalize_timers_lock:
        prev = _transfer_finalize_timers.pop(key, None)
        if prev:
            try:
                prev.cancel()
            except Exception:
                pass
        _transfer_finalize_timers[key] = timer
    timer.start()


@app.before_request
def _activity_before_request():
    # Track recent clients in-memory for the admin activity page.
    try:
        _touch_client()
    except Exception:
        pass


def tinfoil_error(error):
    return jsonify({
        'error': error
    })

def _create_job(kind, total=0):
    job_id = uuid.uuid4().hex
    job = {
        'id': job_id,
        'kind': kind,
        'status': 'running',
        'cancelled': False,
        'created_at': time.time(),
        'updated_at': time.time(),
        'progress': {
            'done': 0,
            'total': total,
            'percent': 0,
            'message': ''
        },
        'logs': [],
        'errors': [],
        'summary': None
    }
    with conversion_jobs_lock:
        conversion_jobs[job_id] = job
    return job_id

def _job_log(job_id, message):
    if message is None:
        return
    message = _fix_mojibake(str(message)).strip()
    if not message:
        return
    with conversion_jobs_lock:
        job = conversion_jobs.get(job_id)
        if not job:
            return
        percent_match = re.search(r'Compressed\s+([0-9.]+)%', message)
        numeric_match = re.fullmatch(r'\d{1,3}', message)
        convert_match = re.search(r'^\[CONVERT\]\s+(.+?)\s+->\s+(.+)$', message)
        verify_match = re.search(r'^\[VERIFY\]\s+(.+)$', message)

        if numeric_match:
            try:
                percent_value = int(numeric_match.group(0))
                if 0 <= percent_value <= 100:
                    job['progress']['percent'] = float(percent_value)
                    stage = job['progress'].get('stage') or 'converting'
                    label = 'Verifying' if stage == 'verifying' else 'Converting'
                    job['progress']['message'] = f"{label}: {percent_value}%"
                    job['updated_at'] = time.time()
                    return
            except ValueError:
                pass
        if percent_match:
            try:
                job['progress']['percent'] = float(percent_match.group(1))
                stage = job['progress'].get('stage') or 'converting'
                label = 'Verifying' if stage == 'verifying' else 'Converting'
                job['progress']['message'] = f"{label}: {job['progress']['percent']:.0f}%"
                job['updated_at'] = time.time()
                return
            except ValueError:
                pass

        if convert_match:
            input_path = convert_match.group(1)
            display_name = os.path.basename(input_path)
            message = f"Converting {display_name}..."
            job['progress']['stage'] = 'converting'
            job['progress']['file'] = display_name
            job['progress']['message'] = message
        elif verify_match:
            input_path = verify_match.group(1)
            display_name = os.path.basename(input_path)
            message = f"Verifying {display_name}..."
            job['progress']['stage'] = 'verifying'
            job['progress']['file'] = display_name
            job['progress']['message'] = message
        elif message.startswith("Running:"):
            message = "Starting converter..."
            job['progress']['stage'] = 'converting'
            job['progress']['message'] = message

        if message:
            job['logs'].append(message)
        if len(job['logs']) > 500:
            job['logs'] = job['logs'][-500:]
        job['updated_at'] = time.time()

def _fix_mojibake(text):
    if not text:
        return text
    if "Ã" not in text and "â" not in text:
        return text
    try:
        fixed = text.encode("latin-1").decode("utf-8")
        return fixed if fixed else text
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text

def _job_progress(job_id, done, total):
    with conversion_jobs_lock:
        job = conversion_jobs.get(job_id)
        if not job:
            return
        job['progress']['done'] = done
        job['progress']['total'] = total
        job['updated_at'] = time.time()

def _job_finish(job_id, results):
    with conversion_jobs_lock:
        job = conversion_jobs.get(job_id)
        if not job:
            return
        if job.get('cancelled'):
            job['status'] = 'cancelled'
        else:
            job['status'] = 'failed' if results.get('errors') else 'success'
        job['errors'] = results.get('errors', [])
        job['summary'] = {
            'converted': results.get('converted', 0),
            'skipped': results.get('skipped', 0),
            'deleted': results.get('deleted', 0),
            'moved': results.get('moved', 0)
        }
        job['updated_at'] = time.time()
        if len(conversion_jobs) > conversion_job_limit:
            oldest = sorted(conversion_jobs.values(), key=lambda item: item['created_at'])[:len(conversion_jobs) - conversion_job_limit]
            for item in oldest:
                conversion_jobs.pop(item['id'], None)

def _job_is_cancelled(job_id):
    with conversion_jobs_lock:
        job = conversion_jobs.get(job_id)
        return bool(job and job.get('cancelled'))

def tinfoil_access(f):
    @wraps(f)
    def _tinfoil_access(*args, **kwargs):
        reload_conf()
        hauth_success = None
        auth_success = None
        request.verified_host = None
        is_tinfoil_client = all(header in request.headers for header in TINFOIL_HEADERS)
        # Host verification to prevent hotlinking
        #Tinfoil doesn't send Hauth for file grabs, only directories, so ignore get_game endpoints.
        host_verification = (
            is_tinfoil_client
            and "/api/get_game" not in request.path
            and (request.is_secure or request.headers.get("X-Forwarded-Proto") == "https")
        )
        if host_verification:
            request_host = request.host
            request_hauth = request.headers.get('Hauth')
            logger.info(f"Secure Tinfoil request from remote host {request_host}, proceeding with host verification.")
            shop_host = app_settings["shop"].get("host")
            shop_hauth = app_settings["shop"].get("hauth")
            if not shop_host:
                logger.error("Missing shop host configuration, Host verification is disabled.")

            elif request_host != shop_host:
                logger.warning(f"Incorrect URL referrer detected: {request_host}.")
                error = f"Incorrect URL `{request_host}`."
                hauth_success = False

            elif not shop_hauth:
                # Try authentication, if an admin user is logging in then set the hauth
                auth_success, auth_error, auth_is_admin =  basic_auth(request)
                if auth_success and auth_is_admin:
                    shop_settings = app_settings['shop']
                    shop_settings['hauth'] = request_hauth
                    set_shop_settings(shop_settings)
                    logger.info(f"Successfully set Hauth value for host {request_host}.")
                    hauth_success = True
                else:
                    logger.warning(f"Hauth value not set for host {request_host}, Host verification is disabled. Connect to the shop from Tinfoil with an admin account to set it.")

            elif request_hauth != shop_hauth:
                logger.warning(f"Incorrect Hauth detected for host: {request_host}.")
                error = f"Incorrect Hauth for URL `{request_host}`."
                hauth_success = False

            else:
                hauth_success = True
                request.verified_host = shop_host

            if hauth_success is False:
                return tinfoil_error(error)
        
        # Now checking auth if shop is private
        if not app_settings['shop']['public']:
            # Shop is private
            if auth_success is None:
                if current_user.is_authenticated and current_user.has_access('shop'):
                    auth_success = True
                else:
                    auth_success, auth_error, _ = basic_auth(request)
            if not auth_success:
                # If the account is frozen, return safe empty responses so clients can display the MOTD.
                try:
                    if is_tinfoil_client and request.path in ('/', '/api/shop/sections', '/api/frozen/notice'):
                        username = _get_request_user()
                        frozen_user = User.query.filter_by(user=username).first() if username else None
                        if frozen_user is not None and bool(getattr(frozen_user, 'frozen', False)):
                            message = (getattr(frozen_user, 'frozen_message', None) or '').strip() or 'Account is frozen.'
                            if request.path == '/api/shop/sections':
                                placeholder_item = {
                                    'name': 'Account frozen',
                                    'title_name': 'Account frozen',
                                    'title_id': '0000000000000000',
                                    'app_id': '0000000000000000',
                                    'app_version': '0',
                                    'app_type': APP_TYPE_BASE,
                                    'category': '',
                                    'icon_url': '',
                                    'url': '/api/frozen/notice#frozen.txt',
                                    'size': 1,
                                    'file_id': 0,
                                    'filename': 'frozen.txt',
                                    'download_count': 0,
                                }
                                empty_sections = {
                                    'sections': [
                                        {'id': 'new', 'title': 'New', 'items': [placeholder_item]},
                                        {'id': 'recommended', 'title': 'Recommended', 'items': [placeholder_item]},
                                        {'id': 'updates', 'title': 'Updates', 'items': [placeholder_item]},
                                        {'id': 'dlc', 'title': 'DLC', 'items': [placeholder_item]},
                                        {'id': 'all', 'title': 'All', 'items': [placeholder_item]},
                                    ]
                                }
                                return jsonify(empty_sections)

                            placeholder = {"url": "/api/frozen/notice#frozen.txt", "size": 1}
                            shop = {"success": message, "files": [placeholder]}
                            if request.verified_host is not None:
                                shop["referrer"] = f"https://{request.verified_host}"
                            if app_settings['shop']['encrypt']:
                                return Response(encrypt_shop(shop), mimetype='application/octet-stream')
                            return jsonify(shop)
                except Exception:
                    pass
                return tinfoil_error(auth_error)

        # Auth success: block frozen accounts from accessing the library.
        try:
            frozen_user = None
            if current_user.is_authenticated:
                frozen_user = current_user
            else:
                username = _get_request_user()
                frozen_user = User.query.filter_by(user=username).first() if username else None
            if frozen_user is not None and bool(getattr(frozen_user, 'frozen', False)):
                message = (getattr(frozen_user, 'frozen_message', None) or '').strip() or 'Account is frozen.'

                # Allow safe empty responses for the shop root + sections.
                if is_tinfoil_client and request.path in ('/', '/api/shop/sections', '/api/frozen/notice'):
                    if request.path == '/api/shop/sections':
                        placeholder_item = {
                            'name': 'Account frozen',
                            'title_name': 'Account frozen',
                            'title_id': '0000000000000000',
                            'app_id': '0000000000000000',
                            'app_version': '0',
                            'app_type': APP_TYPE_BASE,
                            'category': '',
                            'icon_url': '',
                            'url': '/api/frozen/notice#frozen.txt',
                            'size': 1,
                            'file_id': 0,
                            'filename': 'frozen.txt',
                            'download_count': 0,
                        }
                        empty_sections = {
                            'sections': [
                                {'id': 'new', 'title': 'New', 'items': [placeholder_item]},
                                {'id': 'recommended', 'title': 'Recommended', 'items': [placeholder_item]},
                                {'id': 'updates', 'title': 'Updates', 'items': [placeholder_item]},
                                {'id': 'dlc', 'title': 'DLC', 'items': [placeholder_item]},
                                {'id': 'all', 'title': 'All', 'items': [placeholder_item]},
                            ]
                        }
                        return jsonify(empty_sections)

                    placeholder = {"url": "/api/frozen/notice#frozen.txt", "size": 1}
                    shop = {"success": message, "files": [placeholder]}
                    if request.verified_host is not None:
                        shop["referrer"] = f"https://{request.verified_host}"
                    if app_settings['shop']['encrypt']:
                        return Response(encrypt_shop(shop), mimetype='application/octet-stream')
                    return jsonify(shop)

                return tinfoil_error(message)
        except Exception:
            pass

        # Auth success
        return f(*args, **kwargs)
    return _tinfoil_access


@app.get('/api/frozen/notice')
def frozen_notice_api():
    # Minimal endpoint used to provide a harmless placeholder file
    # for frozen accounts so clients don't reject an empty shop.
    return Response(b' ', mimetype='application/octet-stream')

def access_shop():
    return render_template(
        'index.html',
        title='Library',
        admin_account_created=admin_account_created(),
        valid_keys=app_settings['titles']['valid_keys'],
        identification_disabled=not app_settings['titles']['valid_keys'],
    )

@access_required('shop')
def access_shop_auth():
    return access_shop()

@app.route('/')
def index():

    @tinfoil_access
    def access_tinfoil_shop():
        start_ts = time.time()
        shop = {
            "success": app_settings['shop']['motd']
        }
        
        if request.verified_host is not None:
            # enforce client side host verification
            shop["referrer"] = f"https://{request.verified_host}"
            
        shop["files"] = gen_shop_files(db)

        if _is_cyberfoil_request():
            _log_access(
                kind='shop',
                filename=request.full_path if request.query_string else request.path,
                ok=True,
                status_code=200,
                duration_ms=int((time.time() - start_ts) * 1000),
            )

        if app_settings['shop']['encrypt']:
            return Response(encrypt_shop(shop), mimetype='application/octet-stream')

        return jsonify(shop)
    
    if all(header in request.headers for header in TINFOIL_HEADERS):
    # if True:
        logger.info(f"Tinfoil connection from {request.remote_addr}")
        return access_tinfoil_shop()

    # Frozen accounts: web UI should only show the MOTD message.
    try:
        frozen_user = None
        if current_user.is_authenticated:
            frozen_user = current_user
        else:
            auth = request.authorization
            username = auth.username if auth and auth.username else None
            frozen_user = User.query.filter_by(user=username).first() if username else None

        if frozen_user is not None and bool(getattr(frozen_user, 'frozen', False)):
            message = (getattr(frozen_user, 'frozen_message', None) or '').strip() or 'Account is frozen.'
            return render_template('frozen.html', title='Library', message=message)
    except Exception:
        pass
     
    if not app_settings['shop']['public']:
        return access_shop_auth()
    return access_shop()

@app.route('/settings')
@access_required('admin')
def settings_page():
    with open(os.path.join(TITLEDB_DIR, 'languages.json')) as f:
        languages = json.load(f)
        languages = dict(sorted(languages.items()))
    return render_template(
        'settings.html',
        title='Settings',
        languages_from_titledb=languages,
        admin_account_created=admin_account_created(),
        valid_keys=app_settings['titles']['valid_keys'],
        identification_disabled=not app_settings['titles']['valid_keys'])

@app.route('/manage')
@access_required('admin')
def manage_page():
    return render_template(
        'manage.html',
        title='Manage',
        admin_account_created=admin_account_created())

@app.route('/upload')
@access_required('admin')
def upload_page():
    return render_template(
        'upload.html',
        title='Upload',
        admin_account_created=admin_account_created())


@app.route('/activity')
@access_required('admin')
def activity_page():
    return render_template(
        'activity.html',
        title='Activity',
        admin_account_created=admin_account_created())


@app.route('/users')
@access_required('admin')
def users_page():
    return render_template(
        'users.html',
        title='Users',
        admin_account_created=admin_account_created())


@app.route('/requests')
@access_required('shop')
def requests_page():
    return render_template(
        'requests.html',
        title='Requests',
        admin_account_created=admin_account_created())


@app.post('/api/requests')
@access_required('shop')
def create_title_request_api():
    data = request.json or {}
    title_id = (data.get('title_id') or '').strip().upper()
    title_name = (data.get('title_name') or '').strip() or None

    ok, message, req = create_title_request(current_user.id, title_id, title_name=title_name)
    if ok:
        return jsonify({'success': True, 'message': message, 'request_id': req.id if req else None})
    return jsonify({'success': False, 'message': message})


@app.get('/api/requests')
@access_required('shop')
def list_requests_api():
    include_all = request.args.get('all', '0') == '1'
    if include_all:
        if not current_user.is_admin:
            return jsonify({'success': False, 'message': 'Forbidden'}), 403

    items = list_requests(user_id=current_user.id, include_all=include_all, limit=500)

    out = []
    for r in items:
        out.append({
            'id': r.id,
            'created_at': int(r.created_at.timestamp()) if r.created_at else None,
            'status': r.status,
            'title_id': r.title_id,
            'title_name': r.title_name,
            'user': {
                'id': r.user.id if r.user else None,
                'user': r.user.user if r.user else None,
            } if include_all else None,
        })
    return jsonify({'success': True, 'requests': out})


@app.get('/api/requests/search')
@access_required('admin')
def request_prowlarr_search_api():
    title_id = (request.args.get('title_id') or '').strip().upper()
    title_name = (request.args.get('title_name') or '').strip()
    if not title_id and not title_name:
        return jsonify({'success': False, 'message': 'Missing title_id or title_name.', 'results': []})

    settings = load_settings()
    downloads = settings.get('downloads', {})
    prowlarr_cfg = downloads.get('prowlarr', {})
    if not prowlarr_cfg.get('url') or not prowlarr_cfg.get('api_key'):
        return jsonify({'success': False, 'message': 'Prowlarr is not configured.', 'results': []})

    # Prefer TitleDB name if we can resolve it.
    resolved_name = title_name
    if title_id:
        titles.load_titledb()
        try:
            info = titles.get_game_info(title_id) or {}
            resolved_name = (info.get('name') or '').strip() or resolved_name
        finally:
            titles.identification_in_progress_count -= 1
            titles.unload_titledb()

    base_query = resolved_name or title_id
    prefix = (downloads.get('search_prefix') or '').strip()
    full_query = base_query
    if prefix and not full_query.lower().startswith(prefix.lower()):
        full_query = f"{prefix} {full_query}".strip()

    try:
        client = ProwlarrClient(prowlarr_cfg['url'], prowlarr_cfg['api_key'])
        results = client.search(full_query, indexer_ids=prowlarr_cfg.get('indexer_ids') or [])
        trimmed = [
            {
                'title': r.get('title'),
                'size': r.get('size'),
                'seeders': r.get('seeders'),
                'leechers': r.get('leechers'),
                'download_url': r.get('download_url'),
            }
            for r in (results or [])[:50]
        ]
        return jsonify({'success': True, 'results': trimmed})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e), 'results': []})


@app.get('/api/admin/activity')
@access_required('admin')
def admin_activity_api():
    limit = request.args.get('limit', 100)
    try:
        limit = int(limit)
    except Exception:
        limit = 100
    limit = max(1, min(limit, 1000))

    # Snapshot active transfers.
    with _active_transfers_lock:
        live = list(_active_transfers.values())

    # Snapshot connected clients (last 2 minutes).
    cutoff = time.time() - 120
    with _connected_clients_lock:
        clients = [v for v in _connected_clients.values() if (v or {}).get('last_seen_at', 0) >= cutoff]

        # Bound memory: drop oldest if we grow too much.
        if len(_connected_clients) > 2000:
            ordered = sorted(_connected_clients.items(), key=lambda kv: (kv[1] or {}).get('last_seen_at', 0), reverse=True)
            _connected_clients.clear()
            for k, v in ordered[:1000]:
                _connected_clients[k] = v

    clients = sorted(clients, key=lambda item: item.get('last_seen_at', 0), reverse=True)[:250]

    # Recent access events.
    history_error = None
    try:
        history = get_access_events(limit=limit)
    except Exception as e:
        history = []
        history_error = str(e)

    include_starts = request.args.get('include_starts', '1') != '0'
    if not include_starts:
        history = [h for h in history if h.get('kind') != 'transfer_start']
    history = _dedupe_history(history)

    # Hydrate title_name where possible.
    title_ids = set()
    for item in live:
        if item.get('title_id'):
            title_ids.add(item['title_id'])
    for item in history:
        if item.get('title_id'):
            title_ids.add(item['title_id'])

    title_names = {}
    for tid in title_ids:
        try:
            info = titles.get_game_info(tid)
            if info and info.get('name'):
                title_names[tid] = info.get('name')
        except Exception:
            pass

    for item in live:
        tid = item.get('title_id')
        if tid and tid in title_names:
            item['title_name'] = title_names[tid]
    for item in history:
        tid = item.get('title_id')
        if tid and tid in title_names:
            item['title_name'] = title_names[tid]

    return jsonify({
        'success': True,
        'live_transfers': live,
        'connected_clients': clients,
        'access_history': history,
        'access_history_error': history_error,
    })

@app.get('/api/settings')
@access_required('admin')
def get_settings_api():
    reload_conf()
    settings = copy.deepcopy(app_settings)
    hauth_value = settings['shop'].get('hauth')
    settings['shop']['hauth_value'] = hauth_value or ''
    settings['shop']['hauth'] = bool(hauth_value)
    return jsonify(settings)

@app.post('/api/settings/titles')
@access_required('admin')
def set_titles_settings_api():
    settings = request.json
    region = settings['region']
    language = settings['language']
    with open(os.path.join(TITLEDB_DIR, 'languages.json')) as f:
        languages = json.load(f)
        languages = dict(sorted(languages.items()))

    if region not in languages or language not in languages[region]:
        resp = {
            'success': False,
            'errors': [{
                    'path': 'titles',
                    'error': f"The region/language pair {region}/{language} is not available."
                }]
        }
        return jsonify(resp)
    
    set_titles_settings(region, language)
    reload_conf()
    titledb.update_titledb(app_settings)
    post_library_change()
    resp = {
        'success': True,
        'errors': []
    } 
    return jsonify(resp)

@app.post('/api/settings/shop')
def set_shop_settings_api():
    data = request.json
    set_shop_settings(data)
    reload_conf()
    resp = {
        'success': True,
        'errors': []
    } 
    return jsonify(resp)

@app.post('/api/settings/downloads')
@access_required('admin')
def set_download_settings_api():
    data = request.json or {}
    set_download_settings(data)
    reload_conf()
    resp = {
        'success': True,
        'errors': []
    }
    return jsonify(resp)


@app.post('/api/settings/media-cache/refresh')
@access_required('admin')
def refresh_media_cache_api():
    data = request.json or {}
    refresh_icons = data.get('icons', True)
    refresh_banners = data.get('banners', True)

    def _clear_cache_dir(dirname):
        cache_dir = os.path.join(CACHE_DIR, dirname)
        if not os.path.isdir(cache_dir):
            return 0
        removed = 0
        for filename in os.listdir(cache_dir):
            path = os.path.join(cache_dir, filename)
            if os.path.isfile(path):
                try:
                    os.remove(path)
                    removed += 1
                except Exception:
                    continue
        return removed

    removed_icons = _clear_cache_dir('icons') if refresh_icons else 0
    removed_banners = _clear_cache_dir('banners') if refresh_banners else 0

    return jsonify({
        'success': True,
        'removed_icons': removed_icons,
        'removed_banners': removed_banners
    })


@app.post('/api/settings/media-cache/prefetch-icons')
@access_required('admin')
def prefetch_media_icons_api():
    titles_library = generate_library()
    cache_dir = os.path.join(CACHE_DIR, 'icons')
    os.makedirs(cache_dir, exist_ok=True)

    fetched = 0
    skipped = 0
    missing = 0
    failed = 0
    failures = []
    headers = {'User-Agent': 'Ownfoil/1.0'}

    titles.load_titledb()
    try:
        for entry in titles_library:
            title_id = (entry.get('title_id') or entry.get('id') or '').upper()
            if not title_id:
                missing += 1
                continue
            info = titles.get_game_info(title_id)
            icon_url = (info or {}).get('iconUrl') or ''
            if not icon_url:
                missing += 1
                continue
            if icon_url.startswith('//'):
                icon_url = 'https:' + icon_url
            clean_url = icon_url.split('?', 1)[0]
            _, ext = os.path.splitext(clean_url)
            if not ext:
                ext = '.jpg'
            cache_name = f"{title_id}{ext}"
            cache_path = os.path.join(cache_dir, cache_name)
            if os.path.exists(cache_path):
                skipped += 1
                continue
            try:
                response = requests.get(icon_url, timeout=10, headers=headers)
                if response.status_code == 200:
                    with open(cache_path, 'wb') as handle:
                        handle.write(response.content)
                    # Generate a smaller variant for faster web UI loads.
                    size, variant_dir, variant_path = _get_variant_path(cache_dir, cache_name, media_kind='icon')
                    if variant_path:
                        with _media_resize_lock:
                            _resize_image_to_path(cache_path, variant_path, size=size)
                    fetched += 1
                else:
                    failed += 1
                    if len(failures) < 5:
                        failures.append({
                            'title_id': title_id,
                            'status': response.status_code,
                            'url': icon_url
                        })
            except Exception as e:
                failed += 1
                if len(failures) < 5:
                    failures.append({
                        'title_id': title_id,
                        'status': 'error',
                        'url': icon_url,
                        'message': str(e)
                    })
    finally:
        titles.unload_titledb()

    return jsonify({
        'success': True,
        'fetched': fetched,
        'skipped': skipped,
        'missing': missing,
        'failed': failed,
        'failures': failures
    })


@app.post('/api/settings/media-cache/prefetch-banners')
@access_required('admin')
def prefetch_media_banners_api():
    titles_library = generate_library()
    cache_dir = os.path.join(CACHE_DIR, 'banners')
    os.makedirs(cache_dir, exist_ok=True)

    fetched = 0
    skipped = 0
    missing = 0
    failed = 0
    failures = []
    headers = {'User-Agent': 'Ownfoil/1.0'}

    titles.load_titledb()
    try:
        for entry in titles_library:
            title_id = (entry.get('title_id') or entry.get('id') or '').upper()
            if not title_id:
                missing += 1
                continue
            info = titles.get_game_info(title_id)
            banner_url = (info or {}).get('bannerUrl') or ''
            if not banner_url:
                missing += 1
                continue
            if banner_url.startswith('//'):
                banner_url = 'https:' + banner_url
            clean_url = banner_url.split('?', 1)[0]
            _, ext = os.path.splitext(clean_url)
            if not ext:
                ext = '.jpg'
            cache_name = f"{title_id}{ext}"
            cache_path = os.path.join(cache_dir, cache_name)
            if os.path.exists(cache_path):
                skipped += 1
                continue
            try:
                response = requests.get(banner_url, timeout=10, headers=headers)
                if response.status_code == 200:
                    with open(cache_path, 'wb') as handle:
                        handle.write(response.content)
                    # Generate a smaller variant for faster web UI loads.
                    size, variant_dir, variant_path = _get_variant_path(cache_dir, cache_name, media_kind='banner')
                    if variant_path:
                        with _media_resize_lock:
                            _resize_image_to_path(cache_path, variant_path, size=size)
                    fetched += 1
                else:
                    failed += 1
                    if len(failures) < 5:
                        failures.append({
                            'title_id': title_id,
                            'status': response.status_code,
                            'url': banner_url
                        })
            except Exception as e:
                failed += 1
                if len(failures) < 5:
                    failures.append({
                        'title_id': title_id,
                        'status': 'error',
                        'url': banner_url,
                        'message': str(e)
                    })
    finally:
        titles.unload_titledb()

    return jsonify({
        'success': True,
        'fetched': fetched,
        'skipped': skipped,
        'missing': missing,
        'failed': failed,
        'failures': failures
    })

@app.post('/api/settings/downloads/test-prowlarr')
@access_required('admin')
def test_downloads_prowlarr_api():
    data = request.json or {}
    url = data.get('url', '')
    api_key = data.get('api_key', '')
    try:
        client = ProwlarrClient(url, api_key)
        status = client.system_status()
        indexer_ids = data.get('indexer_ids') or []
        warning = None
        if indexer_ids:
            indexers = client.list_indexers()
            available_ids = {item.get('id') for item in (indexers or [])}
            missing = [idx for idx in indexer_ids if idx not in available_ids]
            if missing:
                warning = f"Missing indexer IDs: {', '.join(str(x) for x in missing)}"
        return jsonify({
            'success': True,
            'message': f"Prowlarr OK ({status.get('version', 'unknown')})",
            'warning': warning
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.post('/api/settings/downloads/test-client')
@access_required('admin')
def test_downloads_client_api():
    data = request.json or {}
    ok, message = test_torrent_client(
        client_type=data.get('type', ''),
        url=data.get('url', ''),
        username=data.get('username', ''),
        password=data.get('password', '')
    )
    download_path = (data.get('download_path') or '').strip()
    warning = None
    if ok and download_path:
        if not os.path.isdir(download_path):
            warning = f"Download path not found: {download_path}"
        elif not os.access(download_path, os.W_OK):
            warning = f"Download path not writable: {download_path}"
    return jsonify({'success': ok, 'message': message, 'warning': warning})

@app.post('/api/downloads/manual')
@access_required('admin')
def manual_download_update():
    data = request.json or {}
    title_id = data.get('title_id')
    version = data.get('version')
    if not title_id or version is None:
        return jsonify({'success': False, 'message': 'Missing title ID or version.'})
    try:
        ok, message = manual_search_update(title_id=title_id, version=version)
        return jsonify({'success': ok, 'message': message})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.post('/api/downloads/manual-search')
@access_required('admin')
def manual_search_update_options():
    data = request.json or {}
    title_id = data.get('title_id')
    version = data.get('version')
    if not title_id or version is None:
        return jsonify({'success': False, 'message': 'Missing title ID or version.', 'results': []})
    try:
        ok, message, results = search_update_options(title_id=title_id, version=version)
        return jsonify({'success': ok, 'message': message, 'results': results})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e), 'results': []})

@app.get('/api/downloads/search')
@access_required('admin')
def downloads_search():
    query = request.args.get('query', '').strip()
    if not query:
        return jsonify({'success': False, 'message': 'Missing query.'})
    settings = load_settings()
    downloads = settings.get('downloads', {})
    prowlarr_cfg = downloads.get('prowlarr', {})
    if not prowlarr_cfg.get('url') or not prowlarr_cfg.get('api_key'):
        return jsonify({'success': False, 'message': 'Prowlarr is not configured.'})
    try:
        prefix = (downloads.get('search_prefix') or '').strip()
        suffix = (downloads.get('search_suffix') or '').strip()
        full_query = query
        if prefix and not full_query.lower().startswith(prefix.lower()):
            full_query = f"{prefix} {full_query}".strip()
        if suffix and not full_query.lower().endswith(suffix.lower()):
            full_query = f"{full_query} {suffix}".strip()
        client = ProwlarrClient(prowlarr_cfg['url'], prowlarr_cfg['api_key'])
        results = client.search(full_query, indexer_ids=prowlarr_cfg.get('indexer_ids') or [])
        trimmed = [
            {
                'title': r.get('title'),
                'size': r.get('size'),
                'seeders': r.get('seeders'),
                'leechers': r.get('leechers'),
                'download_url': r.get('download_url')
            }
            for r in (results or [])[:50]
        ]
        return jsonify({'success': True, 'results': trimmed})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.post('/api/downloads/queue')
@access_required('admin')
def downloads_queue():
    data = request.json or {}
    download_url = data.get('download_url')
    expected_name = data.get('title')
    update_only = bool(data.get('update_only', False))
    expected_version = data.get('expected_version')
    if not download_url:
        return jsonify({'success': False, 'message': 'Missing download URL.'})
    ok, message = queue_download_url(
        download_url,
        expected_name=expected_name,
        update_only=update_only,
        expected_version=expected_version
    )
    return jsonify({'success': ok, 'message': message})

@app.post('/api/manage/organize')
@access_required('admin')
def manage_organize_library():
    data = request.json or {}
    dry_run = bool(data.get('dry_run', False))
    verbose = bool(data.get('verbose', False))
    results = organize_library(dry_run=dry_run, verbose=verbose)
    if results.get('success') and not dry_run:
        post_library_change()
    return jsonify(results)

@app.post('/api/manage/delete-updates')
@access_required('admin')
def manage_delete_updates():
    data = request.json or {}
    dry_run = bool(data.get('dry_run', False))
    verbose = bool(data.get('verbose', False))
    results = delete_older_updates(dry_run=dry_run, verbose=verbose)
    if results.get('success') and not dry_run:
        post_library_change()
    return jsonify(results)

@app.post('/api/manage/check-downloads')
@access_required('admin')
def manage_check_downloads():
    ok, message = check_completed_downloads(scan_cb=scan_library, post_cb=post_library_change)
    return jsonify({'success': ok, 'message': message})


@app.get('/api/manage/downloads-queue')
@access_required('admin')
def manage_downloads_queue():
    state = get_downloads_state()
    return jsonify({'success': True, 'state': state})

@app.post('/api/manage/convert')
@access_required('admin')
def manage_convert_nsz():
    data = request.json or {}
    dry_run = bool(data.get('dry_run', False))
    delete_original = bool(data.get('delete_original', True))
    verbose = bool(data.get('verbose', False))
    threads = data.get('threads')
    command = data.get('command')
    results = convert_to_nsz(
        command_template=command,
        delete_original=delete_original,
        dry_run=dry_run,
        verbose=verbose,
        threads=threads
    )
    if results.get('success') and not dry_run:
        post_library_change()
    return jsonify(results)

@app.get('/api/manage/convertibles')
@access_required('admin')
def manage_convertible_files():
    library_id = request.args.get('library_id')
    files = list_convertible_files(library_id=int(library_id)) if library_id else list_convertible_files()
    return jsonify({'success': True, 'files': files})

@app.post('/api/manage/convert-single')
@access_required('admin')
def manage_convert_single():
    data = request.json or {}
    file_id = data.get('file_id')
    dry_run = bool(data.get('dry_run', False))
    delete_original = bool(data.get('delete_original', True))
    verbose = bool(data.get('verbose', False))
    threads = data.get('threads')
    command = data.get('command')
    if not file_id:
        return jsonify({'success': False, 'errors': ['Missing file id.'], 'converted': 0, 'skipped': 0, 'details': []})
    results = convert_single_to_nsz(
        file_id=int(file_id),
        command_template=command,
        delete_original=delete_original,
        dry_run=dry_run,
        verbose=verbose,
        threads=threads
    )
    if results.get('success') and not dry_run:
        post_library_change()
    return jsonify(results)

@app.post('/api/manage/convert-job')
@access_required('admin')
def manage_convert_job():
    data = request.json or {}
    dry_run = bool(data.get('dry_run', False))
    delete_original = bool(data.get('delete_original', True))
    verbose = bool(data.get('verbose', False))
    threads = data.get('threads')
    library_id = data.get('library_id')
    timeout_seconds = data.get('timeout_seconds')
    command = data.get('command')

    job_id = _create_job('convert')

    def _run_job():
        with app.app_context():
            results = convert_to_nsz(
                command_template=command,
                delete_original=delete_original,
                dry_run=dry_run,
                verbose=verbose,
                log_cb=lambda msg: _job_log(job_id, msg),
                progress_cb=lambda done, total: _job_progress(job_id, done, total),
                stream_output=True,
                threads=threads,
                library_id=library_id,
                cancel_cb=lambda: _job_is_cancelled(job_id),
                timeout_seconds=timeout_seconds,
                min_size_bytes=200 * 1024 * 1024
            )
            if results.get('success') and not dry_run:
                post_library_change()
            _job_finish(job_id, results)

    thread = threading.Thread(target=_run_job, daemon=True)
    thread.start()
    return jsonify({'success': True, 'job_id': job_id})

@app.post('/api/manage/convert-single-job')
@access_required('admin')
def manage_convert_single_job():
    data = request.json or {}
    file_id = data.get('file_id')
    dry_run = bool(data.get('dry_run', False))
    delete_original = bool(data.get('delete_original', True))
    verbose = bool(data.get('verbose', False))
    threads = data.get('threads')
    timeout_seconds = data.get('timeout_seconds')
    command = data.get('command')
    if not file_id:
        return jsonify({'success': False, 'errors': ['Missing file id.']})

    job_id = _create_job('convert-single', total=1)

    def _run_job():
        with app.app_context():
            results = convert_single_to_nsz(
                file_id=int(file_id),
                command_template=command,
                delete_original=delete_original,
                dry_run=dry_run,
                verbose=verbose,
                log_cb=lambda msg: _job_log(job_id, msg),
                progress_cb=lambda done, total: _job_progress(job_id, done, total),
                stream_output=True,
                threads=threads,
                cancel_cb=lambda: _job_is_cancelled(job_id),
                timeout_seconds=timeout_seconds
            )
            if results.get('success') and not dry_run:
                post_library_change()
            _job_finish(job_id, results)

    thread = threading.Thread(target=_run_job, daemon=True)
    thread.start()
    return jsonify({'success': True, 'job_id': job_id})

@app.get('/api/manage/convert-job/<job_id>')
@access_required('admin')
def manage_convert_job_status(job_id):
    with conversion_jobs_lock:
        job = conversion_jobs.get(job_id)
        if not job:
            return jsonify({'success': False, 'error': 'Job not found.'}), 404
        return jsonify({'success': True, 'job': job})

@app.post('/api/manage/convert-job/<job_id>/cancel')
@access_required('admin')
def manage_convert_job_cancel(job_id):
    with conversion_jobs_lock:
        job = conversion_jobs.get(job_id)
        if not job:
            return jsonify({'success': False, 'error': 'Job not found.'}), 404
        job['cancelled'] = True
        job['status'] = 'cancelled'
        job['updated_at'] = time.time()
    return jsonify({'success': True})

@app.get('/api/manage/jobs')
@access_required('admin')
def manage_jobs_list():
    limit = request.args.get('limit', 20)
    try:
        limit = int(limit)
    except ValueError:
        limit = 20
    with conversion_jobs_lock:
        jobs = sorted(conversion_jobs.values(), key=lambda item: item['created_at'], reverse=True)[:limit]
        return jsonify({'success': True, 'jobs': jobs})

@app.get('/api/manage/health')
@access_required('admin')
def manage_health():
    nsz_path = None
    try:
        nsz_path = _get_nsz_exe()
    except NameError:
        nsz_path = None
    keys_file = KEYS_FILE
    keys_ok = os.path.exists(KEYS_FILE)
    return jsonify({
        'success': True,
        'nsz_exe': nsz_path,
        'keys_file': keys_file,
        'keys_present': keys_ok
    })

@app.get('/api/manage/libraries')
@access_required('admin')
def manage_libraries_list():
    libraries = get_libraries()
    return jsonify({
        'success': True,
        'libraries': [{'id': lib.id, 'path': lib.path} for lib in libraries]
    })

@app.route('/api/settings/library/paths', methods=['GET', 'POST', 'DELETE'])
@access_required('admin')
def library_paths_api():
    global watcher
    if request.method == 'POST':
        data = request.json
        success, errors = add_library_complete(app, watcher, data['path'])
        if success:
            reload_conf()
            post_library_change()
        resp = {
            'success': success,
            'errors': errors
        }
    elif request.method == 'GET':
        reload_conf()
        resp = {
            'success': True,
            'errors': [],
            'paths': app_settings['library']['paths']
        }    
    elif request.method == 'DELETE':
        data = request.json
        success, errors = remove_library_complete(app, watcher, data['path'])
        if success:
            reload_conf()
            post_library_change()
        resp = {
            'success': success,
            'errors': errors
        }
    return jsonify(resp)


@app.get('/api/titledb/search')
@access_required('shop')
def titledb_search_api():
    query = request.args.get('q', '').strip()
    limit = request.args.get('limit', 20)
    try:
        limit = int(limit)
    except Exception:
        limit = 20

    if not query:
        return jsonify({'success': True, 'results': []})

    titles.load_titledb()
    try:
        results = titles.search_titles(query, limit=limit)
        # mark items already in library
        existing = set((t.title_id or '').upper() for t in Titles.query.with_entities(Titles.title_id).all())
        for r in results:
            r['in_library'] = (r.get('id') or '').upper() in existing
        return jsonify({'success': True, 'results': results})
    finally:
        titles.identification_in_progress_count -= 1
        titles.unload_titledb()

@app.post('/api/upload')
@access_required('admin')
def upload_file():
    errors = []
    success = False

    file = request.files['file']
    if file and allowed_file(file.filename):
        # filename = secure_filename(file.filename)
        file.save(KEYS_FILE + '.tmp')
        logger.info(f'Validating {file.filename}...')
        valid = load_keys(KEYS_FILE + '.tmp')
        if valid:
            os.rename(KEYS_FILE + '.tmp', KEYS_FILE)
            success = True
            logger.info('Successfully saved valid keys.txt')
            reload_conf()
            post_library_change()
        else:
            os.remove(KEYS_FILE + '.tmp')
            logger.error(f'Invalid keys from {file.filename}')

    resp = {
        'success': success,
        'errors': errors
    } 
    return jsonify(resp)


@app.post('/api/upload/library')
@access_required('admin')
def upload_library_files():
    files = request.files.getlist('files')
    if not files:
        return jsonify({'success': False, 'message': 'No files uploaded.', 'uploaded': 0, 'skipped': 0, 'errors': []})

    library_id = request.form.get('library_id')
    library_path = None
    if library_id:
        library_path = get_library_path(library_id)
    if not library_path:
        library_paths = get_libraries_path()
        library_path = library_paths[0] if library_paths else None

    if not library_path:
        return jsonify({'success': False, 'message': 'No library path configured.', 'uploaded': 0, 'skipped': 0, 'errors': []})

    os.makedirs(library_path, exist_ok=True)
    allowed_exts = {'nsp', 'nsz', 'xci', 'xcz'}
    uploaded = 0
    skipped = 0
    errors = []
    saved_paths = []

    for file in files:
        filename = secure_filename(file.filename or '')
        if not filename:
            skipped += 1
            continue
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        if ext not in allowed_exts:
            skipped += 1
            continue
        dest_path = _ensure_unique_path(os.path.join(library_path, filename))
        try:
            file.save(dest_path)
            uploaded += 1
            saved_paths.append(dest_path)
        except Exception as e:
            errors.append(str(e))

    if uploaded:
        scan_library_path(library_path)
        enqueue_organize_paths(saved_paths)
        post_library_change()

    return jsonify({
        'success': uploaded > 0,
        'uploaded': uploaded,
        'skipped': skipped,
        'errors': errors
    })


@app.route('/api/titles', methods=['GET'])
@access_required('shop')
def get_all_titles_api():
    start_ts = time.time()
    titles_library = generate_library()

    if _is_cyberfoil_request():
        _log_access(
            kind='shop_titles',
            filename=request.full_path if request.query_string else request.path,
            ok=True,
            status_code=200,
            duration_ms=int((time.time() - start_ts) * 1000),
        )

    return jsonify({
        'total': len(titles_library),
        'games': titles_library
    })

@app.get('/api/library/size')
@access_required('shop')
def get_library_size_api():
    total = db.session.query(func.sum(Files.size)).scalar() or 0
    return jsonify({'success': True, 'total_bytes': int(total)})

@app.get('/api/library/status')
@access_required('shop')
def get_library_status_api():
    with library_rebuild_lock:
        status = dict(library_rebuild_status)
    with scan_lock:
        scan_active = bool(scan_in_progress)
    with titledb_update_lock:
        titledb_active = bool(is_titledb_update_running)
    status.update({
        'scan_in_progress': scan_active,
        'titledb_updating': titledb_active
    })
    return jsonify({'success': True, 'status': status})

@app.route('/api/get_game/<int:id>')
@tinfoil_access
def serve_game(id):
    start_ts = time.time()
    remote_addr = request.headers.get('X-Forwarded-For') or request.remote_addr
    user_agent = request.headers.get('User-Agent')
    username = _get_request_user()

    try:
        Files.query.filter_by(id=id).update({Files.download_count: Files.download_count + 1})
        db.session.commit()
    except Exception:
        db.session.rollback()
    filepath = db.session.query(Files.filepath).filter_by(id=id).first()[0]
    filedir, filename = os.path.split(filepath)

    title_id = None
    try:
        file_obj = Files.query.filter_by(id=id).first()
        if file_obj and getattr(file_obj, 'apps', None):
            app_obj = file_obj.apps[0] if file_obj.apps else None
            if app_obj and getattr(app_obj, 'title', None):
                title_id = app_obj.title.title_id
    except Exception:
        title_id = None

    transfer_id = uuid.uuid4().hex
    meta = {
        'id': transfer_id,
        'started_at': start_ts,
        'user': username,
        'remote_addr': remote_addr,
        'user_agent': user_agent,
        'file_id': id,
        'filename': filename,
        'title_id': title_id,
        'bytes_sent': 0,
    }

    with _active_transfers_lock:
        _active_transfers[transfer_id] = meta

    resp = send_from_directory(filedir, filename, conditional=True)

    session_key = _transfer_session_start(
        user=username,
        remote_addr=remote_addr,
        user_agent=user_agent,
        title_id=title_id,
        file_id=id,
        filename=filename,
        resp_status_code=getattr(resp, 'status_code', 200),
    )

    # Wrap response iterable to track bytes sent while preserving Range support.
    original_iterable = resp.response
    status_code = getattr(resp, 'status_code', None)

    state = {
        'sent': 0,
        'ok': True,
        'finished': False,
    }

    _finish_lock = threading.Lock()

    def _finish_once():
        with _finish_lock:
            if state.get('finished'):
                return
            state['finished'] = True

        code = getattr(resp, 'status_code', None) or status_code
        try:
            _transfer_session_finish(
                session_key,
                ok=bool(state.get('ok')),
                status_code=int(code) if code is not None else None,
                bytes_sent=int(state.get('sent') or 0),
            )
        except Exception:
            try:
                logger.exception('Failed to finalize transfer session')
            except Exception:
                pass

    def _on_close():
        _finish_once()

    resp.call_on_close(_on_close)

    def _generate_wrapped():
        try:
            for chunk in original_iterable:
                try:
                    state['sent'] = int(state.get('sent') or 0) + len(chunk)
                    if state['sent'] % (1024 * 1024) < len(chunk):
                        _transfer_session_progress(session_key, state['sent'])
                        with _active_transfers_lock:
                            if transfer_id in _active_transfers:
                                _active_transfers[transfer_id]['bytes_sent'] = state['sent']
                except Exception:
                    pass
                yield chunk
        except Exception:
            state['ok'] = False
            raise
        finally:
            # Ensure we close the session even if call_on_close doesn't fire.
            _finish_once()
            with _active_transfers_lock:
                _active_transfers.pop(transfer_id, None)

    resp.response = _generate_wrapped()
    resp.direct_passthrough = False
    return resp


@app.get('/api/shop/sections')
@tinfoil_access
def shop_sections_api():
    start_ts = time.time()
    limit = request.args.get('limit', 50)
    try:
        limit = int(limit)
    except ValueError:
        limit = 50

    now = time.time()
    payload = None
    with shop_sections_cache_lock:
        cache_hit = (
            shop_sections_cache['payload'] is not None
            and shop_sections_cache['limit'] == limit
        )
        if cache_hit:
            payload = shop_sections_cache['payload']

    if payload is None:
        disk_cache = _load_shop_sections_cache_from_disk()
        if disk_cache and disk_cache.get('limit') == limit:
            disk_payload = disk_cache.get('payload')
            if disk_payload:
                payload = disk_payload
                with shop_sections_cache_lock:
                    shop_sections_cache['payload'] = payload
                    shop_sections_cache['limit'] = limit
                    shop_sections_cache['timestamp'] = disk_cache.get('timestamp', 0)

    if payload is None:
        payload = _build_shop_sections_payload(limit)
        with shop_sections_cache_lock:
            shop_sections_cache['payload'] = payload
            shop_sections_cache['limit'] = limit
            shop_sections_cache['timestamp'] = now
        _save_shop_sections_cache_to_disk(payload, limit, now)

    if _is_cyberfoil_request():
        _log_access(
            kind='shop_sections',
            filename=request.full_path if request.query_string else request.path,
            ok=True,
            status_code=200,
            duration_ms=int((time.time() - start_ts) * 1000),
        )

    return jsonify(payload)


@app.get('/api/shop/icon/<title_id>')
@tinfoil_access
def shop_icon_api(title_id):
    start_ts = time.time()
    title_id = (title_id or '').upper()
    if not title_id:
        return Response(status=404)

    cache_dir = os.path.join(CACHE_DIR, 'icons')
    os.makedirs(cache_dir, exist_ok=True)

    # Fast path: serve cached file without TitleDB lookup.
    cached_name = _get_cached_media_filename(cache_dir, title_id, media_kind='icon')
    if cached_name:
        src_path = os.path.join(cache_dir, cached_name)
        size, variant_dir, variant_path = _get_variant_path(cache_dir, cached_name, media_kind='icon')
        if variant_path and os.path.exists(variant_path) and os.path.getmtime(variant_path) >= os.path.getmtime(src_path):
            response = send_from_directory(variant_dir, cached_name)
            response.headers['Cache-Control'] = 'public, max-age=604800, immutable'
            if _is_cyberfoil_request():
                _log_access(
                    kind='shop_media',
                    title_id=title_id,
                    filename=f"icon:{cached_name}",
                    ok=True,
                    status_code=getattr(response, 'status_code', 200),
                    duration_ms=int((time.time() - start_ts) * 1000),
                )
            return response
        if variant_path and os.path.exists(src_path):
            with _media_resize_lock:
                if os.path.exists(src_path) and (not os.path.exists(variant_path) or os.path.getmtime(variant_path) < os.path.getmtime(src_path)):
                    _resize_image_to_path(src_path, variant_path, size=size)
            if os.path.exists(variant_path):
                response = send_from_directory(variant_dir, cached_name)
                response.headers['Cache-Control'] = 'public, max-age=604800, immutable'
                if _is_cyberfoil_request():
                    _log_access(
                        kind='shop_media',
                        title_id=title_id,
                        filename=f"icon:{cached_name}",
                        ok=True,
                        status_code=getattr(response, 'status_code', 200),
                        duration_ms=int((time.time() - start_ts) * 1000),
                    )
                return response
        response = send_from_directory(cache_dir, cached_name)
        response.headers['Cache-Control'] = 'public, max-age=604800, immutable'
        if _is_cyberfoil_request():
            _log_access(
                kind='shop_media',
                title_id=title_id,
                filename=f"icon:{cached_name}",
                ok=True,
                status_code=getattr(response, 'status_code', 200),
                duration_ms=int((time.time() - start_ts) * 1000),
            )
        return response

    titles.load_titledb()
    info = titles.get_game_info(title_id)
    titles.unload_titledb()
    icon_url = info.get('iconUrl') if info else ''
    if not icon_url:
        response = send_from_directory(app.static_folder, 'placeholder-icon.svg')
        response.headers['Cache-Control'] = 'public, max-age=3600'
        if _is_cyberfoil_request():
            _log_access(
                kind='shop_media',
                title_id=title_id,
                filename='icon:placeholder',
                ok=True,
                status_code=getattr(response, 'status_code', 200),
                duration_ms=int((time.time() - start_ts) * 1000),
            )
        return response

    cache_name, cache_path = _ensure_cached_media_file(cache_dir, title_id, icon_url)
    if not cache_path:
        response = send_from_directory(app.static_folder, 'placeholder-icon.svg')
        response.headers['Cache-Control'] = 'public, max-age=3600'
        if _is_cyberfoil_request():
            _log_access(
                kind='shop_media',
                title_id=title_id,
                filename='icon:placeholder',
                ok=True,
                status_code=getattr(response, 'status_code', 200),
                duration_ms=int((time.time() - start_ts) * 1000),
            )
        return response

    if not os.path.exists(cache_path):
        try:
            resp = requests.get(icon_url, timeout=10)
            if resp.status_code == 200:
                with open(cache_path, 'wb') as handle:
                    handle.write(resp.content)
        except Exception:
            cache_path = None

    if cache_path and os.path.exists(cache_path):
        _remember_cached_media_filename(title_id, cache_name, media_kind='icon')
        size, variant_dir, variant_path = _get_variant_path(cache_dir, cache_name, media_kind='icon')
        if variant_path:
            with _media_resize_lock:
                if not os.path.exists(variant_path) or os.path.getmtime(variant_path) < os.path.getmtime(cache_path):
                    _resize_image_to_path(cache_path, variant_path, size=size)
            if os.path.exists(variant_path):
                response = send_from_directory(variant_dir, cache_name)
                response.headers['Cache-Control'] = 'public, max-age=604800, immutable'
                if _is_cyberfoil_request():
                    _log_access(
                        kind='shop_media',
                        title_id=title_id,
                        filename=f"icon:{cache_name}",
                        ok=True,
                        status_code=getattr(response, 'status_code', 200),
                        duration_ms=int((time.time() - start_ts) * 1000),
                    )
                return response

        response = send_from_directory(cache_dir, cache_name)
        response.headers['Cache-Control'] = 'public, max-age=604800, immutable'
        if _is_cyberfoil_request():
            _log_access(
                kind='shop_media',
                title_id=title_id,
                filename=f"icon:{cache_name}",
                ok=True,
                status_code=getattr(response, 'status_code', 200),
                duration_ms=int((time.time() - start_ts) * 1000),
            )
        return response

    response = send_from_directory(app.static_folder, 'placeholder-icon.svg')
    response.headers['Cache-Control'] = 'public, max-age=3600'
    if _is_cyberfoil_request():
        _log_access(
            kind='shop_media',
            title_id=title_id,
            filename='icon:placeholder',
            ok=True,
            status_code=getattr(response, 'status_code', 200),
            duration_ms=int((time.time() - start_ts) * 1000),
        )
    return response


@app.get('/api/shop/banner/<title_id>')
@tinfoil_access
def shop_banner_api(title_id):
    start_ts = time.time()
    title_id = (title_id or '').upper()
    if not title_id:
        return Response(status=404)

    cache_dir = os.path.join(CACHE_DIR, 'banners')
    os.makedirs(cache_dir, exist_ok=True)

    # Fast path: serve cached file without TitleDB lookup.
    cached_name = _get_cached_media_filename(cache_dir, title_id, media_kind='banner')
    if cached_name:
        src_path = os.path.join(cache_dir, cached_name)
        size, variant_dir, variant_path = _get_variant_path(cache_dir, cached_name, media_kind='banner')
        if variant_path and os.path.exists(variant_path) and os.path.getmtime(variant_path) >= os.path.getmtime(src_path):
            response = send_from_directory(variant_dir, cached_name)
            response.headers['Cache-Control'] = 'public, max-age=604800, immutable'
            if _is_cyberfoil_request():
                _log_access(
                    kind='shop_media',
                    title_id=title_id,
                    filename=f"banner:{cached_name}",
                    ok=True,
                    status_code=getattr(response, 'status_code', 200),
                    duration_ms=int((time.time() - start_ts) * 1000),
                )
            return response
        if variant_path and os.path.exists(src_path):
            with _media_resize_lock:
                if os.path.exists(src_path) and (not os.path.exists(variant_path) or os.path.getmtime(variant_path) < os.path.getmtime(src_path)):
                    _resize_image_to_path(src_path, variant_path, size=size)
            if os.path.exists(variant_path):
                response = send_from_directory(variant_dir, cached_name)
                response.headers['Cache-Control'] = 'public, max-age=604800, immutable'
                if _is_cyberfoil_request():
                    _log_access(
                        kind='shop_media',
                        title_id=title_id,
                        filename=f"banner:{cached_name}",
                        ok=True,
                        status_code=getattr(response, 'status_code', 200),
                        duration_ms=int((time.time() - start_ts) * 1000),
                    )
                return response
        response = send_from_directory(cache_dir, cached_name)
        response.headers['Cache-Control'] = 'public, max-age=604800, immutable'
        if _is_cyberfoil_request():
            _log_access(
                kind='shop_media',
                title_id=title_id,
                filename=f"banner:{cached_name}",
                ok=True,
                status_code=getattr(response, 'status_code', 200),
                duration_ms=int((time.time() - start_ts) * 1000),
            )
        return response

    titles.load_titledb()
    info = titles.get_game_info(title_id)
    titles.unload_titledb()
    banner_url = info.get('bannerUrl') if info else ''
    if not banner_url:
        response = send_from_directory(app.static_folder, 'placeholder-banner.svg')
        response.headers['Cache-Control'] = 'public, max-age=3600'
        if _is_cyberfoil_request():
            _log_access(
                kind='shop_media',
                title_id=title_id,
                filename='banner:placeholder',
                ok=True,
                status_code=getattr(response, 'status_code', 200),
                duration_ms=int((time.time() - start_ts) * 1000),
            )
        return response

    cache_name, cache_path = _ensure_cached_media_file(cache_dir, title_id, banner_url)
    if not cache_path:
        response = send_from_directory(app.static_folder, 'placeholder-banner.svg')
        response.headers['Cache-Control'] = 'public, max-age=3600'
        if _is_cyberfoil_request():
            _log_access(
                kind='shop_media',
                title_id=title_id,
                filename='banner:placeholder',
                ok=True,
                status_code=getattr(response, 'status_code', 200),
                duration_ms=int((time.time() - start_ts) * 1000),
            )
        return response

    if not os.path.exists(cache_path):
        try:
            resp = requests.get(banner_url, timeout=10)
            if resp.status_code == 200:
                with open(cache_path, 'wb') as handle:
                    handle.write(resp.content)
        except Exception:
            cache_path = None

    if cache_path and os.path.exists(cache_path):
        _remember_cached_media_filename(title_id, cache_name, media_kind='banner')
        size, variant_dir, variant_path = _get_variant_path(cache_dir, cache_name, media_kind='banner')
        if variant_path:
            with _media_resize_lock:
                if not os.path.exists(variant_path) or os.path.getmtime(variant_path) < os.path.getmtime(cache_path):
                    _resize_image_to_path(cache_path, variant_path, size=size)
            if os.path.exists(variant_path):
                response = send_from_directory(variant_dir, cache_name)
                response.headers['Cache-Control'] = 'public, max-age=604800, immutable'
                if _is_cyberfoil_request():
                    _log_access(
                        kind='shop_media',
                        title_id=title_id,
                        filename=f"banner:{cache_name}",
                        ok=True,
                        status_code=getattr(response, 'status_code', 200),
                        duration_ms=int((time.time() - start_ts) * 1000),
                    )
                return response

        response = send_from_directory(cache_dir, cache_name)
        response.headers['Cache-Control'] = 'public, max-age=604800, immutable'
        if _is_cyberfoil_request():
            _log_access(
                kind='shop_media',
                title_id=title_id,
                filename=f"banner:{cache_name}",
                ok=True,
                status_code=getattr(response, 'status_code', 200),
                duration_ms=int((time.time() - start_ts) * 1000),
            )
        return response

    response = send_from_directory(app.static_folder, 'placeholder-banner.svg')
    response.headers['Cache-Control'] = 'public, max-age=3600'
    if _is_cyberfoil_request():
        _log_access(
            kind='shop_media',
            title_id=title_id,
            filename='banner:placeholder',
            ok=True,
            status_code=getattr(response, 'status_code', 200),
            duration_ms=int((time.time() - start_ts) * 1000),
        )
    return response


@debounce(10)
def post_library_change():
    with library_rebuild_lock:
        if not library_rebuild_status['in_progress']:
            library_rebuild_status['started_at'] = time.time()
        library_rebuild_status['in_progress'] = True
        library_rebuild_status['updated_at'] = time.time()
    with app.app_context():
        try:
            titles.load_titledb()
            process_library_identification(app)
            add_missing_apps_to_db()
            update_titles() # Ensure titles are updated after identification
            # remove missing files
            remove_missing_files_from_db()
            organize_pending_downloads()
            # The process_library_identification already handles updating titles and generating library
            # So, we just need to ensure titles_library is updated from the generated library
            generate_library()
            titles.identification_in_progress_count -= 1
            titles.unload_titledb()
            with shop_sections_cache_lock:
                shop_sections_cache['payload'] = None
                shop_sections_cache['timestamp'] = 0
                shop_sections_cache['limit'] = None

            # Media cache index can be repopulated on demand.
            with _media_cache_lock:
                _media_cache_index['icon'].clear()
                _media_cache_index['banner'].clear()
            now = time.time()
            payload = _build_shop_sections_payload(50)
            with shop_sections_cache_lock:
                shop_sections_cache['payload'] = payload
                shop_sections_cache['limit'] = 50
                shop_sections_cache['timestamp'] = now
            _save_shop_sections_cache_to_disk(payload, 50, now)
        finally:
            with library_rebuild_lock:
                library_rebuild_status['in_progress'] = False
                library_rebuild_status['updated_at'] = time.time()

@app.post('/api/library/scan')
@access_required('admin')
def scan_library_api():
    data = request.json
    path = data['path']
    success = True
    errors = []

    global scan_in_progress
    with scan_lock:
        if scan_in_progress:
            logger.info('Skipping scan_library_api call: Scan already in progress')
            return {'success': False, 'errors': []}
    # Set the scan status to in progress
    scan_in_progress = True

    try:
        if path is None:
            scan_library()
        else:
            scan_library_path(path)
    except Exception as e:
        errors.append(e)
        success = False
        logger.error(f"Error during library scan: {e}")
    finally:
        with scan_lock:
            scan_in_progress = False

    post_library_change()
    resp = {
        'success': success,
        'errors': errors
    } 
    return jsonify(resp)

def scan_library():
    logger.info(f'Scanning whole library ...')
    libraries = get_libraries()
    for library in libraries:
        scan_library_path(library.path) # Only scan, identification will be done globally

if __name__ == '__main__':
    logger.info('Starting initialization of Ownfoil...')
    init_db(app)
    init_users(app)
    init()
    logger.info('Initialization steps done, starting server...')
    # Enable threading so admin activity polling keeps working during transfers.
    app.run(debug=False, use_reloader=False, host="0.0.0.0", port=8465, threaded=True)
    # Shutdown server
    logger.info('Shutting down server...')
    watcher.stop()
    watcher_thread.join()
    logger.debug('Watcher thread terminated.')
    # Shutdown scheduler
    app.scheduler.shutdown()
    logger.debug('Scheduler terminated.')
