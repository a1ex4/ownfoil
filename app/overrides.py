import json
import os
import threading

import datetime
from typing import Optional
from flask import abort, Blueprint, request, jsonify, current_app
from sqlalchemy.orm import joinedload
from werkzeug.exceptions import BadRequest, Conflict, NotFound

from auth import access_required
from db import db, Apps, AppOverrides
import titles as titles_lib
from utils import *
from images import *
from constants import *
from cache import (
    compute_overrides_snapshot_hash,
    regenerate_cache,
    is_overrides_snapshot_current,
)
import logging
logger = logging.getLogger('main')

# --- api blueprint ---------------------------------------------------------
overrides_blueprint = Blueprint("overrides_blueprint", __name__, url_prefix="/api/overrides")


# --- routes ----------------------------------------------------------------
@overrides_blueprint.route("", methods=["GET"])
@access_required("shop")
def list_overrides():
    payload, etag_hash = generate_overrides()

    resp = jsonify(payload)
    # Use the same ETag semantics as library: enable cheap 304 revalidation by clients
    resp.set_etag(etag_hash)
    resp.headers["Vary"] = "Authorization"
    resp.headers["Cache-Control"] = "no-cache, private"
    return resp.make_conditional(request)


@overrides_blueprint.get("/<int:oid>")
@access_required('shop')
def get_override(oid: int):
    ov = AppOverrides.query.options(joinedload(AppOverrides.app)).get(oid)
    if not ov:
        raise NotFound("Override not found.")
    return jsonify(_serialize_with_art_urls(ov))


@overrides_blueprint.post("")
@access_required('admin')
def create_override():
    data, banner_file, banner_remove, icon_file, icon_remove = _parse_payload()

    data = data or {}
    data.setdefault("enabled", True)
    if "enabled" in data and isinstance(data["enabled"], str):
        data["enabled"] = data["enabled"].lower() in ("1", "true", "yes", "on")

    # Empty strings → None for text fields
    for k in ("app_id", "name", "region", "description", "content_type", "version", "icon_path", "banner_path", "release_date", "corrected_title_id"):
        if k in data and isinstance(data[k], str) and not data[k].strip():
            data[k] = None

    # Normalize release_date
    if "release_date" in data:
        data["release_date"] = _parse_iso_date_or_none(data["release_date"])
    
    # Normalize corrected_title_id
    if "corrected_title_id" in data:
        data["corrected_title_id"] = normalize_id(data["corrected_title_id"])

    # Require app_id (string) from client, but we map it to the Apps row
    app_id = data.get("app_id")
    if not app_id:
        raise BadRequest("app_id is required.")
    
    app_id = normalize_id(app_id, 'app')
    if not app_id:
        raise BadRequest("Invalid app_id format.")

    # Find the target Apps row for this logical app_id
    app = _resolve_target_app(app_id)
    if not app:
        raise NotFound("No app found for the given app_id.")

    # Enforce uniqueness per Apps row (one override per app_fk)
    if AppOverrides.query.filter_by(app_fk=app.id).first():
        raise Conflict("An override already exists for this app.")

    # Create the override attached to the Apps row
    ov = AppOverrides(app=app)
    _apply_fields(ov, data)

    # Handle explicit removals
    if banner_remove and ov.banner_path:
        delete_art_file_if_owned(ov.banner_path, "banner")
        ov.banner_path = None
    if icon_remove and ov.icon_path:
        delete_art_file_if_owned(ov.icon_path, "icon")
        ov.icon_path = None

    banner_raw = None
    icon_raw = None

    # Validate & read first
    if banner_file:
        validate_upload(banner_file)
        banner_raw = read_upload_bytes(banner_file)

    if icon_file:
        validate_upload(icon_file)
        icon_raw = read_upload_bytes(icon_file)

    # Save uploaded assets (use the related app's app_id for filenames)
    if banner_raw:
        ov.banner_path = save_art_from_bytes(ov.app.app_id, banner_raw, "banner")
    if icon_raw:
        ov.icon_path = save_art_from_bytes(ov.app.app_id, icon_raw, "icon")

    # Derive counterpart if missing
    if banner_raw and not ov.icon_path and not icon_remove:
        ov.icon_path = save_art_from_bytes(ov.app.app_id, banner_raw, "icon")
    if icon_raw and not ov.banner_path and not banner_remove:
        ov.banner_path = save_art_from_bytes(ov.app.app_id, icon_raw, "banner")

    # Timestamps
    ov.created_at = datetime.datetime.utcnow()
    ov.updated_at = datetime.datetime.utcnow()

    db.session.add(ov)
    try:
        db.session.commit()
        _refresh_caches()
    except Exception:
        logger.error("Create override failed")
        db.session.rollback()
        raise BadRequest("Could not create override.")

    return jsonify(_serialize_with_art_urls(ov)), 201


@overrides_blueprint.put("/<int:oid>")
@overrides_blueprint.patch("/<int:oid>")
@access_required('admin')
def update_override(oid: int):
    data, banner_file, banner_remove, icon_file, icon_remove = _parse_payload()
    data = data or {}

    if "enabled" in data and isinstance(data["enabled"], str):
        data["enabled"] = data["enabled"].lower() in ("1", "true", "yes", "on")

    # Empty strings → None for text fields
    for k in (
        "name", "region", "description", "content_type",
        "version", "icon_path", "banner_path", "release_date", "corrected_title_id"
    ):
        if k in data and isinstance(data[k], str) and not data[k].strip():
            data[k] = None

    # app_id is immutable; reject attempts to change it
    if "app_id" in data:
        abort(400, description="app_id is read-only and cannot be changed.")

    # Normalize release_date using helper
    if "release_date" in data:
        data["release_date"] = _parse_iso_date_or_none(data["release_date"])

    # Normalize corrected_title_id
    if "corrected_title_id" in data:
        data["corrected_title_id"] = normalize_id(data["corrected_title_id"])

    ov = AppOverrides.query.get(oid)
    if not ov:
        raise NotFound("Override not found.")

    _apply_fields(ov, data)

    # Handle explicit removals
    if banner_remove and ov.banner_path:
        delete_art_file_if_owned(ov.banner_path, "banner")
        ov.banner_path = None
    if icon_remove and ov.icon_path:
        delete_art_file_if_owned(ov.icon_path, "icon")
        ov.icon_path = None

    banner_raw = None
    icon_raw = None

    if banner_file:
        validate_upload(banner_file)
        banner_raw = read_upload_bytes(banner_file)
    if icon_file:
        validate_upload(icon_file)
        icon_raw = read_upload_bytes(icon_file)

    # Save uploaded assets
    if banner_raw:
        ov.banner_path = save_art_from_bytes(ov.app.app_id, banner_raw, "banner")
    if icon_raw:
        ov.icon_path = save_art_from_bytes(ov.app.app_id, icon_raw, "icon")

    # Derive counterpart if missing
    if banner_raw and not ov.icon_path and not icon_remove:
        ov.icon_path = save_art_from_bytes(ov.app.app_id, banner_raw, "icon")
    if icon_raw and not ov.banner_path and not banner_remove:
        ov.banner_path = save_art_from_bytes(ov.app.app_id, icon_raw, "banner")

    ov.updated_at = datetime.datetime.utcnow()

    try:
        db.session.commit()
        _refresh_caches()
    except Exception:
        logger.error("Update override failed")
        db.session.rollback()
        raise BadRequest("Could not update override.")

    return jsonify(_serialize_with_art_urls(ov))


@overrides_blueprint.delete("/<int:oid>")
@access_required('admin')
def delete_override(oid: int):
    ov = AppOverrides.query.get(oid)
    if not ov:
        raise NotFound("Override not found.")

    if ov.banner_path:
        delete_art_file_if_owned(ov.banner_path, "banner")
    if ov.icon_path:
        delete_art_file_if_owned(ov.icon_path, "icon")

    try:
        db.session.delete(ov)
        db.session.commit()
        _refresh_caches()
    except Exception:
        logger.error("Delete override failed")
        db.session.rollback()
        raise BadRequest("Could not delete override.")
    return jsonify({"ok": True, "deleted_id": oid})

def generate_overrides():
    """
    Public entry-point for routes.
    Always returns the latest cached payload (regenerating when needed).
    """
    snap = load_or_generate_overrides_snapshot()
    return snap["payload"], snap["hash"]

def load_or_generate_overrides_snapshot():
    """
    Load from disk if hash unchanged, otherwise regenerate + save.
    """
    saved = load_json(OVERRIDES_CACHE_FILE)
    if saved and is_overrides_snapshot_current(saved):
        return saved

    # Cache missing or stale → regenerate
    return _generate_overrides_snapshot()

def _generate_overrides_snapshot():
    """
    Build the final payload:
      {
        "items": [...override rows serialized...],
        "redirects": {
          "<app_id>": {
            "corrected_title_id": "...",
            "projection": {...}   # from TitleDB
          },
          ...
        }
      }

    Includes TitleDB projections; writes to disk with a 'hash' top-level key.
    """
    logger.info("Generating overrides snapshot...")

    with titles_lib.titledb_session("generate_overrides"):
        # Query rows once
        rows = (
            db.session.query(AppOverrides)
            .order_by(AppOverrides.created_at.desc())
            .all()
        )
        items = [_serialize_with_art_urls(r) for r in rows]
        redirects = {}
        for ov in rows:
            if not getattr(ov, "enabled", False):
                continue
            corr = getattr(ov, "corrected_title_id", None)
            appid = getattr(getattr(ov, "app", None), "app_id", None)
            if not (appid and corr):
                continue
            projection = _project_titledb_block(corr)
            redirects[appid] = {
                "corrected_title_id": corr,
                "projection": projection,
            }

        current_hash = compute_overrides_snapshot_hash()
        snapshot = {
            "hash": current_hash,
            "payload": {
                "items": items,
                "redirects": redirects,
            }
        }
        save_json(snapshot, OVERRIDES_CACHE_FILE)
        logger.info("Generating overrides snapshot done.")
        return snapshot

def build_override_index(include_disabled: bool = False) -> dict:
    """
    Build a lightweight index of overrides keyed by app_id.
    Only fields needed by the library merge path are included.
    Structure:
        {
          "by_app": {
            "<APP_ID>": {
               "id": <override id>,
               "app_fk": <apps.id>,
               "enabled": true/false,
               "corrected_title_id": "0100....",
               # (optional) a few display fields if you want them downstream:
               "name": "...",
               "description": "...",
               "release_date": "yyyy-mm-dd" | None,
               "banner_path": "...",
               "icon_path": "..."
            },
            ...
          },
          "count": <number of entries>
        }
    """
    q = AppOverrides.query.options(joinedload(AppOverrides.app))
    if not include_disabled:
        q = q.filter(AppOverrides.enabled.is_(True))

    by_app = {}
    for ov in q.all():
        app_id = ov.app.app_id if ov.app else None
        if not app_id:
            continue
        by_app[app_id] = {
            "id": ov.id,
            "app_fk": ov.app_fk,
            "enabled": bool(ov.enabled),
            "corrected_title_id": ov.corrected_title_id,
            # optional extras that can be handy for UI merges (not required):
            "name": ov.name,
            "description": ov.description,
            "release_date": ov.release_date.isoformat() if ov.release_date else None,
            "banner_path": ov.banner_path,
            "icon_path": ov.icon_path,
        }

    return {"by_app": by_app, "count": len(by_app)}

def _refresh_caches():
    """
    Regenerate overrides + shop caches without blocking the request.
    Falls back to synchronous regeneration if we have no app context.
    """
    cache_paths = (OVERRIDES_CACHE_FILE, SHOP_CACHE_FILE)

    try:
        app = current_app._get_current_object()
    except RuntimeError:
        regenerate_cache(*cache_paths)
        return

    def _job():
        with app.app_context():
            try:
                regenerate_cache(*cache_paths)
            except Exception:
                logger.exception("Background cache regeneration failed.")

    threading.Thread(target=_job, name="refresh-caches", daemon=True).start()

# Note: UI sends multipart only when a banner or icon upload/removal is requested; otherwise JSON.
# This keeps existing JSON flows working while enabling binary upload.
def _parse_payload():
    """
    Accept either JSON (application/json) or multipart/form-data.
    Returns: (data_dict, banner_file, banner_remove, icon_file, icon_remove)
    """
    def _to_bool(value):
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        s = str(value).strip().lower()
        return s in {"1", "true", "yes", "on"}

    if request.is_json:
        data = request.get_json(silent=True) or {}
        banner_file = None
        icon_file = None
        banner_remove = _to_bool(data.get("banner_remove"))
        icon_remove   = _to_bool(data.get("icon_remove"))
    else:
        data = request.form.to_dict()
        banner_file = request.files.get("banner_file")
        icon_file   = request.files.get("icon_file")
        banner_remove = _to_bool(data.get("banner_remove"))
        icon_remove   = _to_bool(data.get("icon_remove"))

    # Conflict resolution: if a new file is uploaded, ignore the corresponding remove flag
    if banner_file:
        banner_remove = False
    if icon_file:
        icon_remove = False

    return data, banner_file, banner_remove, icon_file, icon_remove

def _apply_fields(ov: AppOverrides, data: dict):
    # Only touch known fields; ignore extras to keep it robust.
    fields = [
        "name", "release_date", "region", "description", "content_type", "version",
        "enabled", "corrected_title_id",
    ]
    for f in fields:
        if f in data:
            setattr(ov, f, data[f])

def _parse_iso_date_or_none(value):
    if not value:
        return None
    try:
        # Accept strict yyyy-MM-dd
        return datetime.date.fromisoformat(value)
    except Exception:
        abort(400, description="Invalid release_date. Expected format: yyyy-MM-dd.")

def _resolve_target_app(app_id: str) -> Optional[Apps]:
    """
    Map a logical 16/32-hex app_id string to a specific Apps row.
    Preference order:
      1) highest numeric app_version among rows with app_type == 'BASE'
      2) otherwise highest numeric app_version among all rows
    """
    q = Apps.query.filter(Apps.app_id == app_id)

    # Try BASE first
    base_rows = q.filter((Apps.app_type == 'BASE') | (Apps.app_type == 'Base')).all()
    if base_rows:
        def v(a): 
            try: return int(a.app_version or 0)
            except: return 0
        return sorted(base_rows, key=v, reverse=True)[0]

    rows = q.all()
    if not rows:
        return None

    def v2(a):
        try: return int(a.app_version or 0)
        except: return 0
    return sorted(rows, key=v2, reverse=True)[0]

def _project_titledb_block(corrected_id: str) -> dict:
    """
    Build the projected block for a corrected TitleID using TitleDB.
    Includes: name, description, region, normalized release_date, bannerUrl, iconUrl, category.
    """
    info = titles_lib.get_game_info(corrected_id) or {}

    return {
        "name":         (info.get("name") or "").strip() or None,
        "description":  info.get("description"),
        "region":       info.get("region"),
        "release_date": info.get("release_date"),
        "bannerUrl":    info.get("bannerUrl"),
        "iconUrl":      info.get("iconUrl"),
        "category":     info.get("category"),
    }

def _serialize_with_art_urls(ov: AppOverrides) -> dict:
    d = ov.as_dict()
    # Ensure app_id string is present even though the model uses app_fk
    try:
        if "app_id" not in d or not d["app_id"]:
            d["app_id"] = ov.app.app_id if ov.app else None
    except Exception:
        d.setdefault("app_id", None)
    d["bannerUrl"] = d.get("banner_path")
    d["iconUrl"]   = d.get("icon_path")
    return d
