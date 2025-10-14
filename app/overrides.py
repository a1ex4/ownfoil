import os
import re

import datetime
from flask import Blueprint, request, jsonify, current_app
from io import BytesIO
from PIL import Image, ImageOps
from werkzeug.exceptions import BadRequest, NotFound
from werkzeug.utils import secure_filename

from utils import admin_required
from db import db, TitleOverrides

# --- api blueprint ---------------------------------------------------------
overrides_blueprint = Blueprint("overrides_blueprint", __name__, url_prefix="/api/overrides")

# --- routes ----------------------------------------------------------------

@overrides_blueprint.get("")
@admin_required
def list_overrides():

    # filters (optional)
    title_id = request.args.get("title_id")
    file_basename = request.args.get("file_basename")
    app_id = request.args.get("app_id")
    enabled = request.args.get("enabled")

    q = TitleOverrides.query
    if title_id:
        q = q.filter(TitleOverrides.title_id == title_id)
    if file_basename:
        q = q.filter(TitleOverrides.file_basename == file_basename)
    if app_id:
        q = q.filter(TitleOverrides.app_id == app_id)
    if enabled is not None and enabled.strip() != "":
        # treat "true"/"1" as True, "false"/"0" as False
        enabled_bool = enabled.lower() in ("1", "true", "yes", "on")
        q = q.filter(TitleOverrides.enabled.is_(enabled_bool))

    # simple pagination
    try:
        page = max(1, int(request.args.get("page", 1)))
        page_size = min(500, max(1, int(request.args.get("page_size", 100))))
    except ValueError:
        raise BadRequest("Invalid pagination params.")

    rows = (
        q.order_by(TitleOverrides.updated_at.desc())
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
@admin_required
def get_override(oid: int):
    uo = TitleOverrides.query.get(oid)
    if not uo:
        raise NotFound("Override not found.")
    return jsonify(_serialize_with_art_urls(uo))

@overrides_blueprint.post("")
@admin_required
def create_override():
    data, banner_file, banner_remove, icon_file, icon_remove = _parse_payload()

    # Defaults / normalization
    data = data or {}
    data.setdefault("enabled", True)
    if "enabled" in data and isinstance(data["enabled"], str):
        data["enabled"] = data["enabled"].lower() in ("1", "true", "yes", "on")
    
    # Empty strings → None for text fields
    for k in (
        "file_basename", "name", "title_id", "app_id",
        "publisher", "region", "description", "content_type",
        "version", "icon_path", "banner_path"
    ):
        if k in data and isinstance(data[k], str) and not data[k].strip():
            data[k] = None

    # Normalize app_version & version (int or None)
    for key in ("app_version", "version"):
        if key in data:
            try:
                data[key] = int(data[key]) if data[key] not in (None, "") else None
            except (TypeError, ValueError):
                data[key] = None

    # Require at least one targeting key after normalization
    if not any(data.get(k) for k in ("title_id", "file_basename", "app_id")):
        raise BadRequest("Provide at least one target: title_id, file_basename, or app_id.")

    # Create + apply fields
    uo = TitleOverrides()
    _apply_fields(uo, data)

    # Handle explicit removals first
    if banner_remove and uo.banner_path:
        _delete_art_file_if_owned(uo.banner_path, "banner")
        uo.banner_path = None
    if icon_remove and uo.icon_path:
        _delete_art_file_if_owned(uo.icon_path, "icon")
        uo.icon_path = None

    title_id_for_assets = uo.title_id or data.get("title_id")
    if banner_file or icon_file:
        title_id_for_assets = _safe_title_id_or_badreq(title_id_for_assets)
    
    banner_raw = None
    icon_raw = None

    # Validate & read first (so we can reuse the original bytes)
    if banner_file:
        _validate_upload(banner_file)
        banner_raw = _read_upload_bytes(banner_file)

    if icon_file:
        _validate_upload(icon_file)
        icon_raw = _read_upload_bytes(icon_file)

    # 1) Save the *uploaded* assets
    if banner_raw:
        uo.banner_path = _save_art_from_bytes(title_id_for_assets, banner_raw, "banner")
    if icon_raw:
        uo.icon_path = _save_art_from_bytes(title_id_for_assets, icon_raw, "icon")

    # 2) Only if the counterpart is STILL missing (and not explicitly removed),
    #    derive it from the same raw bytes we just read.
    if banner_raw and not uo.icon_path and not icon_remove:
        uo.icon_path = _save_art_from_bytes(title_id_for_assets, banner_raw, "icon")

    if icon_raw and not uo.banner_path and not banner_remove:
        uo.banner_path = _save_art_from_bytes(title_id_for_assets, icon_raw, "banner")

    # Timestamps
    uo.created_at = datetime.datetime.utcnow()
    uo.updated_at = datetime.datetime.utcnow()

    # Persist
    db.session.add(uo)
    try:
        db.session.commit()
    except Exception as e:
        current_app.logger.exception("Create override failed")
        db.session.rollback()
        raise BadRequest("Could not create override.")

    return jsonify(_serialize_with_art_urls(uo)), 201

@overrides_blueprint.put("/<int:oid>")
@admin_required
def update_override(oid: int):
    data, banner_file, banner_remove, icon_file, icon_remove = _parse_payload()
    data = data or {}

    if "enabled" in data and isinstance(data["enabled"], str):
        data["enabled"] = data["enabled"].lower() in ("1", "true", "yes", "on")

    # Empty strings → None for text fields
    for k in (
        "file_basename", "name", "title_id", "app_id",
        "publisher", "region", "description", "content_type",
        "version", "icon_path", "banner_path"
    ):
        if k in data and isinstance(data[k], str) and not data[k].strip():
            data[k] = None

    # Normalize app_version (int or None)
    for key in ("app_version", "version"):
        if key in data:
            try:
                data[key] = int(data[key]) if data[key] not in (None, "") else None
            except (TypeError, ValueError):
                data[key] = None

    uo = TitleOverrides.query.get(oid)
    if not uo:
        raise NotFound("Override not found.")

    # Capture the old title_id to detect changes.
    old_title_id = uo.title_id

    # Apply updated fields (title_id may change)
    _apply_fields(uo, data)

    # Handle explicit removals first
    if banner_remove and uo.banner_path:
        _delete_art_file_if_owned(uo.banner_path, "banner")
        uo.banner_path = None
    if icon_remove and uo.icon_path:
        _delete_art_file_if_owned(uo.icon_path, "icon")
        uo.icon_path = None

    # If title_id changed but there are no new uploads, keep existing owned art in sync by renaming.
    # (Runs before computing title_id_for_assets; safe even if paths are None.)
    if old_title_id and uo.title_id and (uo.title_id != old_title_id) and not banner_file and not icon_file:
        safe_new_tid = _safe_title_id_or_badreq(uo.title_id)
        if uo.banner_path:
            uo.banner_path = _rename_owned_art_if_needed(uo.banner_path, "banner", safe_new_tid)
        if uo.icon_path:
            uo.icon_path = _rename_owned_art_if_needed(uo.icon_path, "icon", safe_new_tid)

    # New uploads (respect possibly updated title_id)
    title_id_for_assets = uo.title_id or data.get("title_id")
    if banner_file or icon_file:
        title_id_for_assets = _safe_title_id_or_badreq(title_id_for_assets)
    
    banner_raw = None
    icon_raw = None

    # Validate & read first
    if banner_file:
        _validate_upload(banner_file)
        banner_raw = _read_upload_bytes(banner_file)

    if icon_file:
        _validate_upload(icon_file)
        icon_raw = _read_upload_bytes(icon_file)

    # 1) Save direct uploads
    if banner_raw:
        uo.banner_path = _save_art_from_bytes(title_id_for_assets, banner_raw, "banner")
    if icon_raw:
        uo.icon_path = _save_art_from_bytes(title_id_for_assets, icon_raw, "icon")

    # 2) Only derive if counterpart is missing *after* direct saves and not removed
    if banner_raw and not uo.icon_path and not icon_remove:
        uo.icon_path = _save_art_from_bytes(title_id_for_assets, banner_raw, "icon")

    if icon_raw and not uo.banner_path and not banner_remove:
        uo.banner_path = _save_art_from_bytes(title_id_for_assets, icon_raw, "banner")

    uo.updated_at = datetime.datetime.utcnow()

    try:
        db.session.commit()
    except Exception as e:
        current_app.logger.exception("Update override failed")
        db.session.rollback()
        raise BadRequest("Could not update override.")

    return jsonify(_serialize_with_art_urls(uo))

@overrides_blueprint.delete("/<int:oid>")
@admin_required
def delete_override(oid: int):
    uo = TitleOverrides.query.get(oid)
    if not uo:
        raise NotFound("Override not found.")

    if uo.banner_path:
        _delete_art_file_if_owned(uo.banner_path, "banner")
    if uo.icon_path:
        _delete_art_file_if_owned(uo.icon_path, "icon")

    try:
        db.session.delete(uo)
        db.session.commit()
    except Exception as e:
        current_app.logger.exception("Delete override failed")
        db.session.rollback()
        raise BadRequest("Could not delete override.")
    return jsonify({"ok": True, "deleted_id": oid})

# --- helpers ---------------------------------------------------------------
ALLOWED_IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp'}
_SAFE_TID = re.compile(r'^(?:[0-9A-Fa-f]{16}|[0-9A-Fa-f]{32})$')  # accept 16-hex Title IDs or 32-hex content/rights IDs

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

def _apply_fields(uo: TitleOverrides, data: dict):
    # Only touch known fields; ignore extras to keep it robust.
    fields = [
        "title_id", "file_basename", "app_id", "app_version",
        "name", "publisher", "region", "description", "content_type", "version",
        "enabled",
    ]
    for f in fields:
        if f in data:
            setattr(uo, f, data[f])

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

def _serialize_with_art_urls(uo: TitleOverrides) -> dict:
    d = uo.as_dict()
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

def _save_art_from_bytes(title_id: str, raw: bytes, kind: str) -> str:
    """
    Save banner or icon artwork from raw bytes.

    Args:
        title_id: 16- or 32-character hex Title ID or content ID.
        raw: Raw image data.
        kind: Either "banner" or "icon".

    Returns:
        The public URL path to the saved image.
    """
    if kind not in ("banner", "icon"):
        raise ValueError(f"Unknown art kind: {kind}. Expected 'banner' or 'icon'.")

    # Determine target parameters
    if kind == "banner":
        out_name = f"{title_id}_banner.png"
        upload_dir = current_app.config["BANNERS_UPLOAD_DIR"]
        url_prefix = current_app.config["BANNERS_UPLOAD_URL_PREFIX"].rstrip('/')
        target_w, target_h = 400, 225
    else:  # icon
        out_name = f"{title_id}_icon.png"
        upload_dir = current_app.config.get("ICONS_UPLOAD_DIR") or current_app.config["BANNERS_UPLOAD_DIR"]
        url_prefix = (current_app.config.get("ICONS_UPLOAD_URL_PREFIX")
                      or current_app.config["BANNERS_UPLOAD_URL_PREFIX"]).rstrip('/')
        target_w = target_h = 400

    os.makedirs(upload_dir, exist_ok=True)
    dst_path = os.path.join(upload_dir, out_name)

    # Remove any older variants (e.g., .jpg/.webp/.png)
    for old_ext in ALLOWED_IMAGE_EXTS.union({".png"}):
        old_path = os.path.join(upload_dir, f"{title_id}_{kind}{old_ext}")
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

def _safe_title_id_or_badreq(tid: str) -> str:
    if not tid or not _SAFE_TID.match(tid):
        raise BadRequest("Invalid title_id format.")
    return tid

def _rename_owned_art_if_needed(public_path: str, kind: str, new_title_id: str) -> str:
    """
    If art is owned (under our prefixes) and title_id changed but no new upload is provided,
    rename the file so on-disk and URL reflect the new title_id. Returns the (possibly) new public URL.
    """
    if not public_path:
        return public_path

    # Determine prefix/dir by kind, mirroring delete helpers above.
    if kind == "banner":
        prefix = current_app.config['BANNERS_UPLOAD_URL_PREFIX'].rstrip('/') + '/'
        if not public_path.startswith(prefix):
            return public_path  # not ours
        rel_name = public_path[len(prefix):]
        src_dir = current_app.config['BANNERS_UPLOAD_DIR']
        dst_dir = src_dir
        new_name = f"{new_title_id}_banner.png"
        new_url_prefix = current_app.config['BANNERS_UPLOAD_URL_PREFIX'].rstrip('/')
    else:
        prefix = (current_app.config.get('ICONS_UPLOAD_URL_PREFIX')
                  or current_app.config['BANNERS_UPLOAD_URL_PREFIX']).rstrip('/') + '/'
        if not public_path.startswith(prefix):
            return public_path  # not ours
        rel_name = public_path[len(prefix):]
        dst_dir = current_app.config.get('ICONS_UPLOAD_DIR') or current_app.config['BANNERS_UPLOAD_DIR']
        src_dir = dst_dir
        new_name = f"{new_title_id}_icon.png"
        new_url_prefix = (current_app.config.get('ICONS_UPLOAD_URL_PREFIX')
                          or current_app.config['BANNERS_UPLOAD_URL_PREFIX']).rstrip('/')

    src_path = os.path.join(src_dir, rel_name)
    dst_path = os.path.join(dst_dir, new_name)

    # If source doesn't exist, nothing to do.
    if not os.path.exists(src_path):
        return public_path

    # Avoid unnecessary rename when already correct.
    if os.path.abspath(src_path) == os.path.abspath(dst_path):
        return public_path

    # Ensure destination directory exists and remove any pre-existing same-name file.
    os.makedirs(dst_dir, exist_ok=True)
    try:
        if os.path.exists(dst_path):
            os.remove(dst_path)
    except OSError:
        pass

    try:
        os.rename(src_path, dst_path)
        return f"{new_url_prefix}/{new_name}"
    except OSError:
        # If rename fails (e.g., cross-device), fall back to copy+remove.
        try:
            import shutil
            shutil.copy2(src_path, dst_path)
            os.remove(src_path)
            return f"{new_url_prefix}/{new_name}"
        except Exception:
            # As a last resort keep the old path untouched.
            return public_path
