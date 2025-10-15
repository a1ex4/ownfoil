import os
import re

import datetime
from flask import abort, Blueprint, request, jsonify, current_app
from io import BytesIO
from PIL import Image, ImageOps
from werkzeug.exceptions import BadRequest, Conflict, NotFound
from werkzeug.utils import secure_filename

from auth import access_required
from db import db, AppOverrides

# --- api blueprint ---------------------------------------------------------
overrides_blueprint = Blueprint("overrides_blueprint", __name__, url_prefix="/api/overrides")

# --- routes ----------------------------------------------------------------

@overrides_blueprint.get("")
@access_required('shop')
def list_overrides():
    # filters (optional)
    app_id = request.args.get("app_id")
    enabled = request.args.get("enabled")

    q = AppOverrides.query
    if app_id:
        q = q.filter(AppOverrides.app_id == app_id)
    if enabled is not None and str(enabled).strip() != "":
        # treat "true"/"1" as True, "false"/"0" as False
        enabled_bool = str(enabled).lower() in ("1", "true", "yes", "on")
        q = q.filter(AppOverrides.enabled.is_(enabled_bool))

    # simple pagination
    try:
        page = max(1, int(request.args.get("page", 1)))
        page_size = min(500, max(1, int(request.args.get("page_size", 100))))
    except ValueError:
        raise BadRequest("Invalid pagination params.")

    rows = (
        q.order_by(AppOverrides.updated_at.desc())
        .paginate(page=page, per_page=page_size, error_out=False)
    )

    return jsonify({
        "items": [_serialize_with_art_urls(r) for r in rows.items],
        "page": rows.page,
        "pages": rows.pages,
        "page_size": page_size,
        "total": rows.total,
    })


@overrides_blueprint.get("/<int:oid>")
@access_required('shop')
def get_override(oid: int):
    ov = AppOverrides.query.get(oid)
    if not ov:
        raise NotFound("Override not found.")
    return jsonify(_serialize_with_art_urls(ov))


@overrides_blueprint.post("")
@access_required('admin')
def create_override():
    data, banner_file, banner_remove, icon_file, icon_remove = _parse_payload()

    # Defaults / normalization
    data = data or {}
    data.setdefault("enabled", True)
    if "enabled" in data and isinstance(data["enabled"], str):
        data["enabled"] = data["enabled"].lower() in ("1", "true", "yes", "on")

    # Empty strings → None for text fields
    for k in (
        "app_id", "name", "region", "description", "content_type",
        "version", "icon_path", "banner_path", "release_date"
    ):
        if k in data and isinstance(data[k], str) and not data[k].strip():
            data[k] = None

    # Normalize release_date using helper
    if "release_date" in data:
        data["release_date"] = _parse_iso_date_or_none(data["release_date"])

    # Require app_id (per-app model). app_id is READ-ONLY after creation.
    app_id = data.get("app_id")
    if not app_id:
        raise BadRequest("app_id is required.")
    _safe_app_id_or_badreq(app_id)

    # Enforce uniqueness early (friendlier than DB error)
    if AppOverrides.query.filter_by(app_id=app_id).first():
        raise Conflict("An override already exists for this app_id.")

    # Create + apply fields (app_id set explicitly, not through _apply_fields)
    ov = AppOverrides(app_id=app_id)
    _apply_fields(ov, data)

    # Handle explicit removals
    if banner_remove and ov.banner_path:
        _delete_art_file_if_owned(ov.banner_path, "banner")
        ov.banner_path = None
    if icon_remove and ov.icon_path:
        _delete_art_file_if_owned(ov.icon_path, "icon")
        ov.icon_path = None

    banner_raw = None
    icon_raw = None

    # Validate & read first
    if banner_file:
        _validate_upload(banner_file)
        banner_raw = _read_upload_bytes(banner_file)

    if icon_file:
        _validate_upload(icon_file)
        icon_raw = _read_upload_bytes(icon_file)

    # Save uploaded assets
    if banner_raw:
        ov.banner_path = _save_art_from_bytes(app_id, banner_raw, "banner")
    if icon_raw:
        ov.icon_path = _save_art_from_bytes(app_id, icon_raw, "icon")

    # Derive counterpart if missing
    if banner_raw and not ov.icon_path and not icon_remove:
        ov.icon_path = _save_art_from_bytes(app_id, banner_raw, "icon")
    if icon_raw and not ov.banner_path and not banner_remove:
        ov.banner_path = _save_art_from_bytes(app_id, icon_raw, "banner")

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
        "version", "icon_path", "banner_path", "release_date"
    ):
        if k in data and isinstance(data[k], str) and not data[k].strip():
            data[k] = None

    # app_id is immutable; reject attempts to change it
    if "app_id" in data:
        abort(400, description="app_id is read-only and cannot be changed.")

    # Normalize release_date using helper
    if "release_date" in data:
        data["release_date"] = _parse_iso_date_or_none(data["release_date"])

    ov = AppOverrides.query.get(oid)
    if not ov:
        raise NotFound("Override not found.")

    _apply_fields(ov, data)

    # Handle explicit removals
    if banner_remove and ov.banner_path:
        _delete_art_file_if_owned(ov.banner_path, "banner")
        ov.banner_path = None
    if icon_remove and ov.icon_path:
        _delete_art_file_if_owned(ov.icon_path, "icon")
        ov.icon_path = None

    banner_raw = None
    icon_raw = None

    if banner_file:
        _validate_upload(banner_file)
        banner_raw = _read_upload_bytes(banner_file)
    if icon_file:
        _validate_upload(icon_file)
        icon_raw = _read_upload_bytes(icon_file)

    # Save uploaded assets
    if banner_raw:
        ov.banner_path = _save_art_from_bytes(ov.app_id, banner_raw, "banner")
    if icon_raw:
        ov.icon_path = _save_art_from_bytes(ov.app_id, icon_raw, "icon")

    # Derive counterpart if missing
    if banner_raw and not ov.icon_path and not icon_remove:
        ov.icon_path = _save_art_from_bytes(ov.app_id, banner_raw, "icon")
    if icon_raw and not ov.banner_path and not banner_remove:
        ov.banner_path = _save_art_from_bytes(ov.app_id, icon_raw, "banner")

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
        _delete_art_file_if_owned(ov.banner_path, "banner")
    if ov.icon_path:
        _delete_art_file_if_owned(ov.icon_path, "icon")

    try:
        db.session.delete(ov)
        db.session.commit()
    except Exception:
        current_app.logger.exception("Delete override failed")
        db.session.rollback()
        raise BadRequest("Could not delete override.")
    return jsonify({"ok": True, "deleted_id": oid})

# --- helpers ---------------------------------------------------------------
ALLOWED_IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp'}
_SAFE_APP_ID = re.compile(r'^(?:[0-9A-Fa-f]{16}|[0-9A-Fa-f]{32})$')  # accept 16/32 hex strings

# Note: UI sends multipart only when a banner or icon upload/removal is requested; otherwise JSON.
# This keeps existing JSON flows working while enabling binary upload.
def _parse_payload():
    """
    Accept either JSON (application/json) or multipart/form-data.
    Returns (data_dict, banner_file, banner_remove_flag, icon_file, icon_remove_flag).
    """
    banner_file = None
    banner_remove = False
    icon_file = None
    icon_remove = False

    ctype = (request.content_type or "").lower()

    if ctype.startswith("multipart/form-data"):
        form = request.form or {}
        data = {k: form.get(k) for k in form.keys()}

        for k in ("enabled", ):
            if k in data and isinstance(data[k], str):
                data[k] = data[k].lower() in ("1", "true", "yes", "on")

        banner_remove = (form.get("banner_remove", "").lower() in ("1", "true", "yes", "on"))
        icon_remove   = (form.get("icon_remove", "").lower()   in ("1", "true", "yes", "on"))

        banner_file = request.files.get("banner_file") or request.files.get("file")
        icon_file   = request.files.get("icon_file")

        return data, banner_file, banner_remove, icon_file, icon_remove

    data = request.get_json(silent=True)
    if data is None:
        raise BadRequest("Expected application/json or multipart/form-data body.")

    banner_remove = bool(data.get("banner_remove")) if isinstance(data, dict) else False
    icon_remove   = bool(data.get("icon_remove"))   if isinstance(data, dict) else False
    return data, None, banner_remove, None, icon_remove


def _apply_fields(ov: AppOverrides, data: dict):
    # Only touch known fields; ignore extras to keep it robust.
    fields = [
        "name", "release_date", "region", "description", "content_type", "version",
        "enabled",
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


def _allowed_image(filename: str) -> bool:
    if not filename:
        return False
    _, ext = os.path.splitext(filename.lower())
    return ext in ALLOWED_IMAGE_EXTS


def _ext_for_content_type(content_type: str) -> str:
    # Best-effort fallback if the filename extension is missing/untrusted
    if content_type == 'image/jpeg':
        return '.jpg'
    if content_type == 'image/png':
        return '.png'
    if content_type == 'image/webp':
        return '.webp'
    return ''


def _serialize_with_art_urls(ov: AppOverrides) -> dict:
    d = ov.as_dict()
    # expose camelCase read-only fields the UI expects
    d["bannerUrl"] = d.get("banner_path")
    d["iconUrl"]   = d.get("icon_path")
    return d


def _validate_upload(file_storage) -> None:
    filename = secure_filename(file_storage.filename or "")
    if not _allowed_image(filename):
        ext = _ext_for_content_type(getattr(file_storage, "mimetype", "") or "")
        if not ext or ext not in ALLOWED_IMAGE_EXTS:
            raise BadRequest("Unsupported file type. Allowed: .jpg .jpeg .png .webp")


def _read_upload_bytes(file_storage) -> bytes:
    stream = getattr(file_storage, "stream", None)
    try:
        if stream: stream.seek(0)
    except Exception:
        pass
    if hasattr(file_storage, "read"):
        data = file_storage.read()
    elif stream and hasattr(stream, "read"):
        data = stream.read()
    else:
        raise BadRequest("Invalid upload object.")
    try:
        if stream: stream.seek(0)
    except Exception:
        pass
    return data


def _save_art_from_bytes(app_id: str, raw: bytes, kind: str) -> str:
    """
    Save banner or icon artwork from raw bytes.

    Args:
        app_id: 16- or 32-character hex app id (title/content id).
        raw: Raw image data.
        kind: Either "banner" or "icon".

    Returns:
        The public URL path to the saved image.
    """
    if kind not in ("banner", "icon"):
        raise ValueError(f"Unknown art kind: {kind}. Expected 'banner' or 'icon'.")

    # Determine target parameters
    if kind == "banner":
        out_name = f"{app_id}_banner.png"
        upload_dir = current_app.config["BANNERS_UPLOAD_DIR"]
        url_prefix = current_app.config["BANNERS_UPLOAD_URL_PREFIX"].rstrip('/')
        target_w, target_h = 400, 225
    else:  # icon
        out_name = f"{app_id}_icon.png"
        upload_dir = current_app.config.get("ICONS_UPLOAD_DIR") or current_app.config["BANNERS_UPLOAD_DIR"]
        url_prefix = (current_app.config.get("ICONS_UPLOAD_URL_PREFIX")
                      or current_app.config["BANNERS_UPLOAD_URL_PREFIX"]).rstrip('/')
        target_w = target_h = 400

    os.makedirs(upload_dir, exist_ok=True)
    dst_path = os.path.join(upload_dir, out_name)

    # Remove any older variants (e.g., .jpg/.webp/.png)
    for old_ext in ALLOWED_IMAGE_EXTS.union({".png"}):
        old_path = os.path.join(upload_dir, f"{app_id}_{kind}{old_ext}")
        if old_path != dst_path and os.path.exists(old_path):
            try:
                os.remove(old_path)
            except OSError:
                pass

    # Open, resize, crop, and save
    with Image.open(BytesIO(raw)) as im:
        im = ImageOps.exif_transpose(im)
        src_w, src_h = im.size
        if src_w == 0 or src_h == 0:
            raise BadRequest("Invalid image.")

        # Compute scale and resize
        scale = max(target_w / src_w, target_h / src_h)
        new_w, new_h = int(round(src_w * scale)), int(round(src_h * scale))
        if (new_w, new_h) != (src_w, src_h):
            im = im.resize((new_w, new_h), Image.Resampling.LANCZOS)

        # Center-crop to target size
        left = max(0, (im.width - target_w) // 2)
        top = max(0, (im.height - target_h) // 2)
        im = im.crop((left, top, left + target_w, top + target_h))

        # Convert to RGB(A)
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA" if "A" in im.getbands() else "RGB")

        im.save(dst_path, format="PNG", optimize=True, compress_level=9)

    return f"{url_prefix}/{out_name}"


def _delete_art_file_if_owned(public_path: str, kind: str) -> None:
    """
    Delete a banner or icon file if the given public URL points inside our managed upload area.

    Args:
        public_path: The public URL of the art file (banner/icon) to delete.
        kind: Either "banner" or "icon" (determines directory/prefix rules).
    """
    if not public_path:
        return

    # Determine the correct URL prefix and directory
    if kind == "banner":
        prefix = current_app.config['BANNERS_UPLOAD_URL_PREFIX'].rstrip('/') + '/'
        upload_dir = current_app.config['BANNERS_UPLOAD_DIR']
    elif kind == "icon":
        prefix = (current_app.config.get('ICONS_UPLOAD_URL_PREFIX')
                  or current_app.config['BANNERS_UPLOAD_URL_PREFIX']).rstrip('/') + '/'
        upload_dir = current_app.config.get('ICONS_UPLOAD_DIR') or current_app.config['BANNERS_UPLOAD_DIR']
    else:
        raise ValueError(f"Unknown kind: {kind}. Expected 'banner' or 'icon'.")

    # Only delete if the file lives inside our configured prefix
    if not public_path.startswith(prefix):
        return

    rel_name = public_path[len(prefix):]
    file_path = os.path.join(upload_dir, rel_name)

    if os.path.exists(file_path):
        try:
            os.remove(file_path)
        except OSError:
            pass


def _safe_app_id_or_badreq(app_id: str) -> str:
    if not app_id or not _SAFE_APP_ID.match(app_id):
        raise BadRequest("Invalid app_id. Expected 16 or 32 hex characters.")
    return app_id
