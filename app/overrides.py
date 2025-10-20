import hashlib
import json

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

# --- api blueprint ---------------------------------------------------------
overrides_blueprint = Blueprint("overrides_blueprint", __name__, url_prefix="/api/overrides")

# --- routes ----------------------------------------------------------------
@overrides_blueprint.route("", methods=["GET"])
@access_required("shop")
def list_overrides():
    rows = (
        db.session.query(AppOverrides)
        .order_by(AppOverrides.created_at.desc())
        .all()
    )

    # Serialize overrides (cheap; no TitleDB work here)
    items = [_serialize_with_art_urls(r) for r in rows]

    # Build a "redirects_key" map that only includes corrected IDs (cheap)
    redirects_key = {}
    for ov in rows:
        corr = getattr(ov, "corrected_title_id", None)
        appid = getattr(getattr(ov, "app", None), "app_id", None) or getattr(ov, "app_id", None)
        if appid and corr:
            redirects_key[appid] = corr

    # ---- Phase A: pre-ETag (no TitleDB load) -------------------------------
    titledb_commit = titles_lib.get_titledb_commit_hash()

    pre_payload_for_hash = {
        "items": items,
        "redirects_key": redirects_key,   # only { app_id: corrected_title_id }
        "titledb_commit": titledb_commit, # bumps when local TitleDB updates
    }
    pre_etag = hashlib.sha256(
        json.dumps(pre_payload_for_hash, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    # If client already has this snapshot, short-circuit BEFORE heavy work
    if pre_etag in request.if_none_match:
        resp = jsonify({})
        resp.set_etag(pre_etag)
        resp.headers["Vary"] = "Authorization"
        resp.headers["Cache-Control"] = "no-cache, private"
        return resp.make_conditional(request)

    # ---- Phase B: only now load TitleDB and build projections --------------
    redirects = {}
    try:
        titles_lib.load_titledb()
        for ov in rows:
            corr = getattr(ov, "corrected_title_id", None)
            appid = getattr(getattr(ov, "app", None), "app_id", None) or getattr(ov, "app_id", None)
            if not (appid and corr):
                continue
            projection = _project_titledb_block(corr)  # (no "versions" key anymore)
            redirects[appid] = {
                "corrected_title_id": corr,
                "projection": projection,
            }
    finally:
        try:
            titles_lib.unload_titledb()
        except Exception:
            pass

    # Full payload (use the SAME pre_etag so revalidation is cheap next time)
    payload = {
        "items": items,
        "redirects": redirects,
    }

    resp = jsonify(payload)
    resp.set_etag(pre_etag)
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
    except Exception:
        current_app.logger.exception("Create override failed")
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
        ov.icon_path  = save_art_from_bytes(ov.app.app_id, icon_raw, "icon")

    # Derive counterpart if missing
    if banner_raw and not ov.icon_path and not icon_remove:
        ov.icon_path = save_art_from_bytes(ov.app.app_id, banner_raw, "icon")
    if icon_raw and not ov.banner_path and not banner_remove:
        ov.banner_path = save_art_from_bytes(ov.app.app_id, icon_raw, "banner")

    ov.updated_at = datetime.datetime.utcnow()

    try:
        db.session.commit()
    except Exception:
        current_app.logger.exception("Update override failed")
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
    except Exception:
        current_app.logger.exception("Delete override failed")
        db.session.rollback()
        raise BadRequest("Could not delete override.")
    return jsonify({"ok": True, "deleted_id": oid})


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
        banner_file = request.files.get("banner")
        icon_file   = request.files.get("icon")
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
    d["bannerUrl"] = d.get("banner_path")
    d["iconUrl"]   = d.get("icon_path")
    return d
