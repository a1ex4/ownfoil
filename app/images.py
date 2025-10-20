import os

from flask import current_app
from io import BytesIO
from PIL import Image, ImageOps
from werkzeug.exceptions import BadRequest
from werkzeug.utils import secure_filename

from utils import *

ALLOWED_IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp'}

def validate_upload(file_storage) -> None:
    filename = secure_filename(file_storage.filename or "")
    if not _allowed_image(filename):
        ext = _ext_for_content_type(getattr(file_storage, "mimetype", "") or "")
        if not ext or ext not in ALLOWED_IMAGE_EXTS:
            raise BadRequest("Unsupported file type. Allowed: .jpg .jpeg .png .webp")

def read_upload_bytes(file_storage) -> bytes:
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

def save_art_from_bytes(app_id: str, raw: bytes, kind: str) -> str:
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

def delete_art_file_if_owned(public_path: str, kind: str) -> None:
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

def garbage_collect_orphan_art_files():
    """Remove banner/icon files on disk with no matching override record."""
    from db import AppOverrides  # local import to avoid cycles
    existing = {ov.banner_path for ov in AppOverrides.query if ov.banner_path} \
             | {ov.icon_path for ov in AppOverrides.query if ov.icon_path}

    for kind, dir_key, url_key in (
        ("banner", "BANNERS_UPLOAD_DIR", "BANNERS_UPLOAD_URL_PREFIX"),
        ("icon",   "ICONS_UPLOAD_DIR",  "ICONS_UPLOAD_URL_PREFIX"),
    ):
        upload_dir = current_app.config.get(dir_key) or current_app.config["BANNERS_UPLOAD_DIR"]
        if not os.path.isdir(upload_dir):
            continue
        for name in os.listdir(upload_dir):
            if not name.endswith((".png", ".jpg", ".jpeg", ".webp")):
                continue
            path = os.path.join(upload_dir, name)
            url_prefix = (current_app.config.get(url_key) or current_app.config["BANNERS_UPLOAD_URL_PREFIX"]).rstrip("/")
            public = f"{url_prefix}/{name}"
            if public not in existing:
                try: os.remove(path)
                except OSError: pass

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
