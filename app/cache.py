import datetime
import hashlib
import json
import logging
import os
from typing import Callable, Dict, Optional

from constants import LIBRARY_CACHE_FILE, LIBRARY_SNAPSHOT_VERSION, OVERRIDES_CACHE_FILE, SHOP_CACHE_FILE
from db import AppOverrides, Files, db, get_all_apps
from utils import load_json
import titles as titles_lib

logger = logging.getLogger("main")

CacheValidator = Callable[[Optional[dict]], bool]


def compute_library_apps_hash() -> str:
    """
    Computes a hash of all Apps table content to detect changes in library state.
    """
    hash_md5 = hashlib.md5()
    apps = get_all_apps()

    for app in sorted(apps, key=lambda x: (x["app_id"] or "", x["app_version"] or "")):
        hash_md5.update((app["app_id"] or "").encode())
        hash_md5.update((app["app_version"] or "").encode())
        hash_md5.update((app["app_type"] or "").encode())
        hash_md5.update(str(app["owned"] or False).encode())
        hash_md5.update((app["title_id"] or "").encode())
    return hash_md5.hexdigest()


def is_library_snapshot_current(saved_library: Optional[dict]) -> bool:
    if not saved_library or not saved_library.get("hash"):
        return False

    if saved_library.get("snapshot_version") != LIBRARY_SNAPSHOT_VERSION:
        return False

    current_apps_hash = compute_library_apps_hash()
    if saved_library.get("hash") != current_apps_hash:
        return False

    current_tdb = titles_lib.get_titledb_commit_hash() or ""
    saved_tdb = saved_library.get("titledb_commit")
    if saved_tdb is None:
        return False

    return saved_tdb == current_tdb


def compute_overrides_fingerprint_rows() -> list[tuple]:
    rows = (
        db.session.query(
            AppOverrides.id,
            AppOverrides.updated_at,
            AppOverrides.corrected_title_id,
            AppOverrides.banner_path,
            AppOverrides.icon_path,
            AppOverrides.enabled,
        )
        .order_by(AppOverrides.id.asc())
        .all()
    )

    normalized = []
    for row in rows:
        updated_at = row[1]
        if isinstance(updated_at, datetime.datetime):
            updated_at_str = updated_at.isoformat(timespec="seconds")
        else:
            updated_at_str = str(updated_at)

        normalized.append(
            (
                row[0],
                updated_at_str,
                row[2] or None,
                row[3] or None,
                row[4] or None,
                bool(row[5]),
            )
        )
    return normalized


def compute_overrides_snapshot_hash() -> str:
    payload_for_hash = {
        "rows": compute_overrides_fingerprint_rows(),
        "titledb_commit": titles_lib.get_titledb_commit_hash(),
    }
    return hashlib.sha256(
        json.dumps(payload_for_hash, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def is_overrides_snapshot_current(saved_snapshot: Optional[dict]) -> bool:
    if not saved_snapshot or not isinstance(saved_snapshot, dict):
        return False
    stored_hash = saved_snapshot.get("hash")
    if not stored_hash:
        return False
    return stored_hash == compute_overrides_snapshot_hash()


def compute_shop_files_fingerprint_rows() -> list[tuple[int, int, str]]:
    rows = (
        db.session.query(Files.id, Files.size, Files.filepath)
        .order_by(Files.id.asc())
        .all()
    )
    fingerprint = []
    for fid, size, path in rows:
        base = os.path.basename(path or "") if path else ""
        fingerprint.append((int(fid), int(size or 0), base))
    return fingerprint


def compute_shop_snapshot_hash() -> str:
    from overrides import load_or_generate_overrides_snapshot

    overrides_snapshot = load_or_generate_overrides_snapshot() or {}
    ov_hash = overrides_snapshot.get("hash") or ""

    library_snapshot = load_json(LIBRARY_CACHE_FILE) or {}
    lib_hash = library_snapshot.get("hash") or ""

    files_fp = compute_shop_files_fingerprint_rows()

    payload = {
        "overrides_hash": ov_hash,
        "library_hash": lib_hash,
        "files": files_fp,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def is_shop_snapshot_current(saved_snapshot: Optional[dict]) -> bool:
    if not saved_snapshot or not isinstance(saved_snapshot, dict):
        return False
    stored_hash = saved_snapshot.get("hash")
    if not stored_hash:
        return False
    return stored_hash == compute_shop_snapshot_hash()


_CACHE_VALIDATORS: Dict[str, CacheValidator] = {
    LIBRARY_CACHE_FILE: is_library_snapshot_current,
    OVERRIDES_CACHE_FILE: is_overrides_snapshot_current,
    SHOP_CACHE_FILE: is_shop_snapshot_current,
}

def generate_snapshot(path: str):
    """
    Regenerate a known cache snapshot given its file path.
    Dispatches to the correct builder so the cache is warm for next request.
    """
    try:
        if path == LIBRARY_CACHE_FILE:
            from library import load_or_generate_library_snapshot

            load_or_generate_library_snapshot()
            logger.info(f"Regenerated library snapshot: {path}")
        elif path == OVERRIDES_CACHE_FILE:
            from overrides import load_or_generate_overrides_snapshot

            load_or_generate_overrides_snapshot()
            logger.info(f"Regenerated overrides snapshot: {path}")
        elif path == SHOP_CACHE_FILE:
            from shop import load_or_generate_shop_snapshot

            load_or_generate_shop_snapshot()
            logger.info(f"Regenerated shop snapshot: {path}")
        else:
            logger.warning(f"Unknown snapshot path: {path}")
    except Exception as e:
        logger.error(f"Failed to regenerate {path}: {e}")


def regenerate_cache(*paths: str):
    """
    Force regeneration of one or more known cache snapshots.

    Accepts either a sequence of paths, or a single iterable of paths. The
    existing cache file is left in place until the snapshot builder finishes,
    so callers keep a fallback if regeneration fails.
    """
    if len(paths) == 1 and not isinstance(paths[0], str):
        candidate_paths = paths[0]
    else:
        candidate_paths = paths

    for path in candidate_paths:
        if not isinstance(path, str):
            logger.warning(f"Skipping non-string cache path: {path!r}")
            continue
        generate_snapshot(path)


def regenerate_all_caches():
    """
    Ensure all known cache snapshots are up-to-date without forcing rebuilds.
    """
    for path in (LIBRARY_CACHE_FILE, OVERRIDES_CACHE_FILE, SHOP_CACHE_FILE):
        validator = _CACHE_VALIDATORS.get(path)
        if not validator:
            logger.warning(f"No validator registered for {path}; forcing regeneration.")
            generate_snapshot(path)
            continue

        name = os.path.basename(path)
        try:
            saved = load_json(path, default=None)
        except Exception as exc:
            logger.warning(f"Failed to load cache snapshot {path}: {exc}")
            saved = None

        if validator(saved):
            logger.debug(f"{name} cache is up-to-date; skipping regeneration.")
            continue

        logger.info(f"Refreshing {name}")
        generate_snapshot(path)
