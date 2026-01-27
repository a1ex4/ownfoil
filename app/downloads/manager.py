import logging
import os
import shutil
import threading
import time

from constants import APP_TYPE_UPD
from db import get_all_titles, get_all_title_apps, get_libraries_path
from library import _ensure_unique_path, enqueue_organize_paths
import titles as titles_lib
from settings import load_settings
from downloads.prowlarr import ProwlarrClient, pick_best_result
from downloads.torrent_client import add_torrent, list_completed, remove_torrent

logger = logging.getLogger("downloads.manager")

_state_lock = threading.Lock()
_state = {
    "running": False,
    "last_run": 0.0,
    "pending": {},  # key -> info
    "completed": set(),
}


def get_downloads_state():
    with _state_lock:
        pending_items = []
        for key, info in _state["pending"].items():
            pending_items.append({
                "key": key,
                "title_id": info.get("title_id"),
                "version": info.get("version"),
                "expected_name": info.get("expected_name"),
                "hash": info.get("hash")
            })
        return {
            "running": _state["running"],
            "last_run": _state["last_run"],
            "pending": pending_items,
            "completed": sorted(_state["completed"])
        }


def run_downloads_job(scan_cb=None, post_cb=None):
    settings = load_settings()
    downloads = settings.get("downloads", {})
    if not downloads.get("enabled"):
        torrent_cfg = downloads.get("torrent_client", {})
        if torrent_cfg.get("url") and torrent_cfg.get("type"):
            with _state_lock:
                has_pending = bool(_state["pending"])
            if has_pending:
                _check_completed(torrent_cfg, scan_cb=scan_cb, post_cb=post_cb)
        return

    interval_minutes = int(downloads.get("interval_minutes") or 60)
    min_interval = max(interval_minutes, 5) * 60
    now = time.time()

    with _state_lock:
        if _state["running"]:
            return
        if _state["last_run"] and (now - _state["last_run"]) < min_interval:
            return
        _state["running"] = True
        _state["last_run"] = now

    try:
        _process_downloads(downloads, scan_cb=scan_cb, post_cb=post_cb)
    finally:
        with _state_lock:
            _state["running"] = False


def check_completed_downloads(scan_cb=None, post_cb=None):
    settings = load_settings()
    downloads = settings.get("downloads", {})
    torrent_cfg = downloads.get("torrent_client", {})
    if not torrent_cfg.get("url") or not torrent_cfg.get("type"):
        return False, "Torrent client is not configured."
    _check_completed(torrent_cfg, scan_cb=scan_cb, post_cb=post_cb)
    return True, "Checked completed downloads."


def _process_downloads(downloads, scan_cb=None, post_cb=None):
    prowlarr_cfg = downloads.get("prowlarr", {})
    torrent_cfg = downloads.get("torrent_client", {})
    if not prowlarr_cfg.get("url") or not prowlarr_cfg.get("api_key"):
        logger.warning("Downloads enabled, but Prowlarr is not configured.")
        return
    if not torrent_cfg.get("url") or not torrent_cfg.get("type"):
        logger.warning("Downloads enabled, but torrent client is not configured.")
        return

    missing_updates = _get_missing_updates()
    if not missing_updates:
        _check_completed(torrent_cfg, scan_cb=scan_cb, post_cb=post_cb)
        return

    client = ProwlarrClient(prowlarr_cfg["url"], prowlarr_cfg["api_key"])
    indexer_ids = prowlarr_cfg.get("indexer_ids") or []
    required_terms = downloads.get("required_terms") or []
    blacklist_terms = downloads.get("blacklist_terms") or []
    min_seeders = int(downloads.get("min_seeders") or 0)

    for update in missing_updates:
        _search_and_queue(
            client=client,
            update=update,
            downloads=downloads,
            indexer_ids=indexer_ids,
            required_terms=required_terms,
            blacklist_terms=blacklist_terms,
            min_seeders=min_seeders
        )

    _check_completed(torrent_cfg, scan_cb=scan_cb, post_cb=post_cb)


def manual_search_update(title_id, version):
    settings = load_settings()
    downloads = settings.get("downloads", {})
    prowlarr_cfg = downloads.get("prowlarr", {})
    torrent_cfg = downloads.get("torrent_client", {})
    if not prowlarr_cfg.get("url") or not prowlarr_cfg.get("api_key"):
        return False, "Prowlarr is not configured."
    if not torrent_cfg.get("url") or not torrent_cfg.get("type"):
        return False, "Torrent client is not configured."

    title_name = title_id
    titles_lib.load_titledb()
    try:
        info = titles_lib.get_game_info(title_id) or {}
        title_name = info.get("name") or title_id
    finally:
        titles_lib.identification_in_progress_count -= 1
        titles_lib.unload_titledb()

    update = {
        "title_id": title_id,
        "title_name": title_name,
        "version": int(version)
    }
    client = ProwlarrClient(prowlarr_cfg["url"], prowlarr_cfg["api_key"])
    ok, message = _search_and_queue(
        client=client,
        update=update,
        downloads=downloads,
        indexer_ids=prowlarr_cfg.get("indexer_ids") or [],
        required_terms=downloads.get("required_terms") or [],
        blacklist_terms=downloads.get("blacklist_terms") or [],
        min_seeders=int(downloads.get("min_seeders") or 0),
        allow_duplicates=False
    )
    return ok, message


def search_update_options(title_id, version, limit=20):
    settings = load_settings()
    downloads = settings.get("downloads", {})
    prowlarr_cfg = downloads.get("prowlarr", {})
    if not prowlarr_cfg.get("url") or not prowlarr_cfg.get("api_key"):
        return False, "Prowlarr is not configured.", []

    title_name = title_id
    titles_lib.load_titledb()
    try:
        info = titles_lib.get_game_info(title_id) or {}
        title_name = info.get("name") or title_id
    finally:
        titles_lib.identification_in_progress_count -= 1
        titles_lib.unload_titledb()

    update = {
        "title_id": title_id,
        "title_name": title_name,
        "version": int(version)
    }
    query_candidates = _build_queries(update)
    client = ProwlarrClient(prowlarr_cfg["url"], prowlarr_cfg["api_key"])
    results = []
    for query in query_candidates:
        results = client.search(query, indexer_ids=prowlarr_cfg.get("indexer_ids") or [])
        if results:
            break
    trimmed = [
        {
            "title": r.get("title"),
            "size": r.get("size"),
            "seeders": r.get("seeders"),
            "leechers": r.get("leechers"),
            "download_url": r.get("download_url")
        }
        for r in (results or [])[:limit]
    ]
    return True, None, trimmed


def queue_download_url(download_url, expected_name=None, update_only=False, expected_version=None):
    settings = load_settings()
    downloads = settings.get("downloads", {})
    torrent_cfg = downloads.get("torrent_client", {})
    if not torrent_cfg.get("url") or not torrent_cfg.get("type"):
        return False, "Torrent client is not configured."
    expected_update_number = None
    ok, message, torrent_hash = add_torrent(
        client_type=torrent_cfg.get("type"),
        url=torrent_cfg.get("url"),
        username=torrent_cfg.get("username"),
        password=torrent_cfg.get("password"),
        download_url=download_url,
        category=torrent_cfg.get("category"),
        download_path=torrent_cfg.get("download_path"),
        expected_name=expected_name,
        update_only=update_only,
        exclude_russian=True,
        expected_update_number=expected_update_number,
        expected_version=expected_version
    )
    if ok:
        key = f"manual:{int(time.time())}"
        update = {
            "title_id": "manual",
            "title_name": expected_name or "Manual download",
            "version": int(time.time())
        }
        _track_pending(key, update, torrent_hash, expected_name=expected_name)
        return True, "Queued download."
    return False, message


def _search_and_queue(client, update, downloads, indexer_ids, required_terms, blacklist_terms, min_seeders, allow_duplicates=True):
    key = f"{update['title_id']}:{update['version']}"
    if not allow_duplicates and _already_tracked(key):
        return False, "Update is already queued."

    query_candidates = _build_queries(update)
    result = None
    for query in query_candidates:
        results = client.search(query, indexer_ids=indexer_ids)
        result = pick_best_result(
            results,
            title_id=update["title_id"],
            version=update["version"],
            min_seeders=min_seeders,
            required_terms=required_terms,
            blacklist_terms=blacklist_terms,
        )
        if result:
            break
    if not result:
        return False, "No matching results found."

    download_url = result.get("download_url")
    if not download_url:
        return False, "Missing download URL."

    torrent_cfg = downloads.get("torrent_client", {})
    ok, message, torrent_hash = add_torrent(
        client_type=torrent_cfg.get("type"),
        url=torrent_cfg.get("url"),
        username=torrent_cfg.get("username"),
        password=torrent_cfg.get("password"),
        download_url=download_url,
        category=torrent_cfg.get("category"),
        download_path=torrent_cfg.get("download_path"),
        expected_name=update.get("search_terms") or result.get("title"),
        update_only=True,
        exclude_russian=True,
        expected_version=update.get("version")
    )
    if ok:
        _track_pending(key, update, torrent_hash, expected_name=result.get("title"))
        logger.info("Queued update %s v%s: %s", update["title_id"], update["version"], result.get("title"))
        return True, "Queued download."
    return False, message




def _build_queries(update):
    title_name = update.get("title_name") or update["title_id"]
    downloads = load_settings().get("downloads", {})
    prefix = (downloads.get("search_prefix") or "").strip()
    suffix = (downloads.get("search_suffix") or "").strip()
    base = f"{prefix} {title_name}".strip() if prefix else title_name
    tail = f" {suffix}".strip() if suffix else ""
    update["search_terms"] = title_name
    return [
        f"{base}{tail}".strip(),
        f"{title_name} update",
    ]


def _get_missing_updates():
    titles_lib.load_titledb()
    try:
        titles = get_all_titles()
        missing = []
        for title in titles:
            if not title.have_base:
                continue
            title_id = title.title_id
            title_info = titles_lib.get_game_info(title_id) or {}
            title_name = title_info.get("name") or title_id
            versions = titles_lib.get_all_existing_versions(title_id) or []
            owned_updates = [
                app for app in get_all_title_apps(title_id)
                if app.get("app_type") == APP_TYPE_UPD and app.get("owned")
            ]
            owned_versions = {
                int(app.get("app_version") or 0) for app in owned_updates
                if app.get("app_version") is not None
            }
            available_versions = [
                int(version_info.get("version") or 0)
                for version_info in versions
                if int(version_info.get("version") or 0) > 0
            ]
            if not available_versions:
                continue
            highest_available = max(available_versions)
            highest_owned = max(owned_versions) if owned_versions else 0
            if highest_owned >= highest_available:
                continue
            missing.append({
                "title_id": title_id,
                "title_name": title_name,
                "version": highest_available,
            })
        return missing
    finally:
        titles_lib.identification_in_progress_count -= 1
        titles_lib.unload_titledb()


def _already_tracked(key):
    with _state_lock:
        return key in _state["pending"] or key in _state["completed"]


def _track_pending(key, update, torrent_hash, expected_name=None):
    with _state_lock:
        _state["pending"][key] = {
            "title_id": update["title_id"],
            "version": update["version"],
            "hash": torrent_hash,
            "expected_name": expected_name or update.get("title_name"),
        }


def _check_completed(torrent_cfg, scan_cb=None, post_cb=None):
    completed_items = list_completed(
        client_type=torrent_cfg.get("type"),
        url=torrent_cfg.get("url"),
        username=torrent_cfg.get("username"),
        password=torrent_cfg.get("password"),
        category=torrent_cfg.get("category"),
    )
    if not completed_items:
        logger.info("No completed torrents detected for category/tag.")
        return
    newly_completed = False
    moved_paths = []
    matched_hashes = set()
    with _state_lock:
        for key, info in list(_state["pending"].items()):
            torrent_hash = info.get("hash")
            match = None
            if torrent_hash:
                match = next((item for item in completed_items if item.get("hash") == torrent_hash), None)
            if not match:
                expected = (info.get("expected_name") or "").lower()
                if expected:
                    match = next((item for item in completed_items if expected in (item.get("name") or "").lower()), None)
            if match:
                if match.get("hash"):
                    matched_hashes.add(match.get("hash"))
                _state["pending"].pop(key, None)
                _state["completed"].add(key)
                moved_path = _move_completed(match)
                if moved_path:
                    moved_paths.append(moved_path)
                    torrent_hash = match.get("hash")
                    if torrent_hash:
                        ok, message = remove_torrent(
                            client_type=torrent_cfg.get("type"),
                            url=torrent_cfg.get("url"),
                            username=torrent_cfg.get("username"),
                            password=torrent_cfg.get("password"),
                            torrent_hash=torrent_hash,
                        )
                        if not ok:
                            logger.warning("Failed to remove torrent %s: %s", torrent_hash, message)
                newly_completed = True
        for item in completed_items:
            torrent_hash = item.get("hash")
            if torrent_hash and torrent_hash in matched_hashes:
                continue
            moved_path = _move_completed(item)
            if moved_path:
                moved_paths.append(moved_path)
                if torrent_hash:
                    ok, message = remove_torrent(
                        client_type=torrent_cfg.get("type"),
                        url=torrent_cfg.get("url"),
                        username=torrent_cfg.get("username"),
                        password=torrent_cfg.get("password"),
                        torrent_hash=torrent_hash,
                    )
                    if not ok:
                        logger.warning("Failed to remove torrent %s: %s", torrent_hash, message)
                newly_completed = True

    if newly_completed and scan_cb:
        logger.info("New downloads completed. Scanning library.")
        scan_cb()
        if post_cb:
            post_cb()
        if moved_paths:
            enqueue_organize_paths(moved_paths)


def _move_completed(item):
    library_paths = get_libraries_path()
    if not library_paths:
        logger.warning("No library paths configured; cannot move download.")
        return
    dest_root = library_paths[0]
    src_path = item.get("path")
    if not src_path or not os.path.exists(src_path):
        logger.warning("Completed download path not found: %s", src_path)
        return

    dest_path = os.path.join(dest_root, os.path.basename(src_path))
    if os.path.abspath(os.path.dirname(src_path)) == os.path.abspath(dest_root):
        return src_path
    dest_path = _ensure_unique_path(dest_path)
    try:
        shutil.move(src_path, dest_path)
        logger.info("Moved download to library: %s", dest_path)
        return dest_path
    except Exception as e:
        logger.warning("Failed to move download %s: %s", src_path, e)
        return None
