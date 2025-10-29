from constants import *
from db import *
from overrides import (
    build_override_index,
    load_or_generate_overrides_snapshot
)
import titles as titles_lib
from utils import load_json, save_json
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_OAEP
from Crypto.Hash import SHA256
from Crypto.Cipher import AES
from sqlalchemy import func
from urllib.parse import quote
import zstandard as zstd
import random
import re
import json
import os
import hashlib
import logging
from typing import Optional

logger = logging.getLogger('main')

# https://github.com/blawar/tinfoil/blob/master/docs/files/public.key 1160174fa2d7589831f74d149bc403711f3991e4
TINFOIL_PUBLIC_KEY = '''-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAvPdrJigQ0rZAy+jla7hS
jwen8gkF0gjtl+lZGY59KatNd9Kj2gfY7dTMM+5M2tU4Wr3nk8KWr5qKm3hzo/2C
Gbc55im3tlRl6yuFxWQ+c/I2SM5L3xp6eiLUcumMsEo0B7ELmtnHTGCCNAIzTFzV
4XcWGVbkZj83rTFxpLsa1oArTdcz5CG6qgyVe7KbPsft76DAEkV8KaWgnQiG0Dps
INFy4vISmf6L1TgAryJ8l2K4y8QbymyLeMsABdlEI3yRHAm78PSezU57XtQpHW5I
aupup8Es6bcDZQKkRsbOeR9T74tkj+k44QrjZo8xpX9tlJAKEEmwDlyAg0O5CLX3
CQIDAQAB
-----END PUBLIC KEY-----'''

_TITLE_ID_BRACKET = re.compile(r"\[[0-9A-Fa-f]{16}\]")

def generate_shop():
    snap = load_or_generate_shop_snapshot()
    return snap["payload"], snap["hash"]

def load_or_generate_shop_snapshot():
    saved = load_json(SHOP_CACHE_FILE)
    current_hash = _current_shop_hash()
    if saved and saved.get("hash") == current_hash:
        return saved
    return _generate_shop_snapshot()

def encrypt_shop(shop):
    input = json.dumps(shop).encode('utf-8')
    # random 128-bit AES key (16 bytes), used later for symmetric encryption (AES)
    aesKey = random.randint(0,0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF).to_bytes(0x10, 'big')
    # zstandard compression
    flag = 0xFD
    cctx = zstd.ZstdCompressor(level=22)
    buf = cctx.compress(input)
    sz = len(buf)

    # Encrypt the AES key with RSA, PKCS1_OAEP padding scheme
    pubKey = RSA.importKey(TINFOIL_PUBLIC_KEY)
    cipher = PKCS1_OAEP.new(pubKey, hashAlgo = SHA256, label=b'')
    # Now the AES key can only be decrypted with Tinfoil private key
    sessionKey = cipher.encrypt(aesKey)

    # Encrypting the Data with AES
    cipher = AES.new(aesKey, AES.MODE_ECB)
    buf = cipher.encrypt(buf + (b'\x00' * (0x10 - (sz % 0x10))))

    binary_data = b'TINFOIL' + flag.to_bytes(1, byteorder='little') + sessionKey + sz.to_bytes(8, 'little') + buf
    return binary_data

def _generate_shop_snapshot():
    # Build only what Tinfoil needs
    logger.info("Generating shop snapshot...")

    with titles_lib.titledb_session("generate_shop"):
        files = _gen_shop_files()
        titledb_map = _build_titledb_from_overrides()

        payload = {
            "files": files,
            "titledb": titledb_map,
        }

        
        snap = {"hash": _current_shop_hash(), "payload": payload}
        save_json(snap, SHOP_CACHE_FILE)
        logger.info("Generating shop snapshot done.")
        return snap

def _gen_shop_files():
    """
    Build the 'files' section for the custom index.
    If a single-content file’s linked app has an enabled override with
    corrected_title_id, present the URL with that [TITLEID] token so Tinfoil
    discovers it under the corrected ID.
    """
    shop_files = []

    # Preload relationships to avoid N+1
    rows = (
        db.session.query(Files)
        .options(
            db.joinedload(Files.apps).joinedload(Apps.override),
            db.joinedload(Files.apps).joinedload(Apps.title),
        )
        .all()
    )

    for f in rows:
        presented_name = f.filename or os.path.basename(f.filepath) or "file.nsp"        
        presented_tid = None
        # Only attempt an ID correction when we can unambiguously pick
        # the single linked app.
        if getattr(f, 'apps', None) and len(f.apps) == 1:
            app = f.apps[0]
            app_type = getattr(app, "app_type", None)

            # Choose TitleID token per type to avoid collisions in Tinfoil:
            # - BASE: base/family TitleID (optionally corrected via override)
            # - UPD:  its own update TitleID (NEVER inherit base corrected id)
            # - DLC:  its own DLC TitleID
            if app_type == titles_lib.APP_TYPE_DLC:
                corr = _effective_corrected_title_id_for_file(f)  # DLC may use corrected id
                presented_tid = corr or app.app_id
            elif app_type == titles_lib.APP_TYPE_UPD:
                # Try to find a corrected base TitleID to mirror (+0x800)
                base_tid = getattr(getattr(app, "title", None), "title_id", None)
                base_corr = None
                if base_tid:
                    base_app = (
                        db.session
                            .query(Apps)
                            .options(db.joinedload(Apps.override))
                            .filter(Apps.app_id == base_tid, Apps.app_type == titles_lib.APP_TYPE_BASE)
                            .first()
                    )
                    if base_app:
                        base_ov = getattr(base_app, "override", None)
                        if base_ov and getattr(base_ov, "enabled", False) and getattr(base_ov, "corrected_title_id", None):
                            base_corr = base_ov.corrected_title_id.strip().upper()

                if base_corr:
                    # compute the update-family TitleID (+0x800)
                    try:
                        presented_tid = f"{int(base_corr, 16) + 0x800:016X}"
                    except ValueError:
                        presented_tid = app.app_id  # fallback to real app id if malformed
                else:
                    # no corrected base; just use the update's real app id
                    presented_tid = app.app_id
            else:
                corr = _effective_corrected_title_id_for_file(f)  # BASE may use corrected id
                presented_tid = corr or (app.title.title_id if getattr(app, "title", None) else app.app_id)

        if presented_tid:
            presented_name = _with_title_id(presented_name, presented_tid)

        shop_files.append({
            "url": f"/api/get_game/{f.id}#{quote(presented_name)}",
            "size": f.size or 0
        })

    return shop_files

def _build_titledb_from_overrides():
    """
    Build `titledb` from enabled AppOverrides, using on disk cache.

    Rules:
      - BASE override:
          * Keyed by corrected_title_id if provided, else by the base TitleID from Titles.
          * entry["id"] == that same key (Tinfoil expects base id == TitleID).
      - DLC override:
          * Keyed by corrected_title_id if provided, else by the DLC's app_id (DLC TitleID).
          * entry["id"] == that same key (use the DLC's own TitleID).
      - Include any overridden fields: name, version (int), region, releaseDate (yyyymmdd), description, size.
      - One node per override; DLCs are NOT nested under the base.
    """
    def _yyyymmdd_int(iso_date_or_none):
        # Accepts 'YYYY-MM-DD' or date/datetime; returns int yyyymmdd or None
        if not iso_date_or_none:
            return None
        rd = iso_date_or_none
        if hasattr(rd, "strftime"):
            return int(rd.strftime("%Y%m%d"))
        # cheap normalization: 'YYYY-MM-DD' -> 'YYYYMMDD'
        s = str(rd).replace("-", "")
        return int(s) if s.isdigit() and len(s) == 8 else None

    def _version_to_int(v):
        return _version_str_to_int(v)

    def _first_value(*values):
        """
        Return the first non-empty/non-null value, preserving the original text.
        """
        for v in values:
            if v is None:
                continue
            if isinstance(v, str):
                if v.strip():
                    return v
            else:
                return v
        return None

    # Try to pull from the cached overrides snapshot if available
    overrides_by_app = {}
    redirect_meta_by_app = {}
    try:
        snap = load_or_generate_overrides_snapshot()
        payload = (snap or {}).get("payload", {}) or {}
        items = payload.get("items", []) or []
        redirects_payload = payload.get("redirects", {}) or {}

        for raw_app_id, redirect_info in redirects_payload.items():
            app_id = (raw_app_id or "").strip().upper()
            if not app_id or not isinstance(redirect_info, dict):
                continue
            corr = redirect_info.get("corrected_title_id") or redirect_info.get("correctedTitleId")
            corr = (corr or "").strip().upper() or None
            projection = redirect_info.get("projection") if isinstance(redirect_info.get("projection"), dict) else {}
            redirect_meta_by_app[app_id] = {
                "corrected_title_id": corr,
                "projection": projection,
            }

        for it in items:
            if it.get("enabled") is False:
                continue
            app_id = (it.get("app_id") or "").strip().upper()
            if not app_id:
                continue
            overrides_by_app[app_id] = {
                "corrected_title_id": _first_value(
                    it.get("corrected_title_id"),
                    it.get("correctedTitleId"),
                    redirect_meta_by_app.get(app_id, {}).get("corrected_title_id"),
                ),
                "name": it.get("name"),
                "version": it.get("version"),
                "region": it.get("region"),
                "release_date": _first_value(it.get("release_date"), it.get("releaseDate")),
                "description": it.get("description"),
                "bannerUrl": _first_value(it.get("bannerUrl"), it.get("banner_path")),
                "iconUrl": _first_value(it.get("iconUrl"), it.get("icon_path")),
                "category": it.get("category"),
            }
    except Exception:
        # Snapshot unavailable/corrupt; we'll fall back
        overrides_by_app = {}
        redirect_meta_by_app = {}

    # Fallback (or augment) from the lightweight index if needed
    if not overrides_by_app:
        idx = build_override_index(include_disabled=False)
        for app_id, ov in idx.get("by_app", {}).items():
            app_id_u = (app_id or "").strip().upper()
            if not app_id_u:
                continue
            overrides_by_app[app_id_u] = {
                "corrected_title_id": ov.get("corrected_title_id"),
                "name": ov.get("name"),
                "version": ov.get("version"),  # may be None if index doesn't include it
                "region": ov.get("region"),
                "release_date": ov.get("release_date"),
                "description": ov.get("description"),
                "bannerUrl": ov.get("banner_path"),
                "iconUrl": ov.get("icon_path"),
                "category": ov.get("category"),
            }
            corr = (ov.get("corrected_title_id") or "").strip().upper()
            if corr:
                info = titles_lib.get_game_info(corr) or {}
                redirect_meta_by_app[app_id_u] = {
                    "corrected_title_id": corr,
                    "projection": {
                        "name": (info.get("name") or "").strip() or None,
                        "description": info.get("description"),
                        "region": info.get("region"),
                        "release_date": info.get("release_date"),
                        "bannerUrl": info.get("bannerUrl"),
                        "iconUrl": info.get("iconUrl"),
                        "category": info.get("category"),
                    },
                }

    if not overrides_by_app:
        return {}

    app_ids = list(overrides_by_app.keys())

    # One bulk query for app_type + base TitleID
    meta_rows = (
        db.session.query(
            Apps.app_id,
            Apps.app_type,
            Titles.title_id,   # may be None for DLC/homebrew without a Titles row
        )
        .outerjoin(Apps.title)
        .filter(Apps.app_id.in_(app_ids))
        .all()
    )
    meta_by_app = {
        (app_id or "").strip().upper(): (
            app_type,
            (title_id or "").strip().upper() if title_id else None,
        )
        for app_id, app_type, title_id in meta_rows
    }

    # Bulk aggregate sizes per app
    size_rows = (
        db.session
        .query(Apps.app_id, func.sum(Files.size))
        .join(Apps.files)  # Apps -> Files
        .filter(Files.size.isnot(None))
        .filter(Apps.app_id.in_(app_ids))
        .group_by(Apps.app_id)
        .all()
    )
    sizes_by_app = {
        (app_id or "").strip().upper(): int(total or 0)
        for app_id, total in size_rows
    }

    # Build the map
    titledb_map = {}

    for app_id_u, ov in overrides_by_app.items():
        app_type, base_tid = meta_by_app.get(app_id_u, (None, None))
        if app_type not in (titles_lib.APP_TYPE_BASE, titles_lib.APP_TYPE_DLC):
            # Unknown type; skip to avoid guessing
            continue

        redirect_meta = redirect_meta_by_app.get(app_id_u, {})
        corr_tid = _first_value(
            (ov.get("corrected_title_id") or None),
            redirect_meta.get("corrected_title_id"),
        )
        corr_tid = (corr_tid or "").strip().upper() or None

        if app_type == titles_lib.APP_TYPE_BASE:
            # BASE → prefer corrected_title_id, else Titles.title_id, else app_id as last resort
            tid_emit = corr_tid or base_tid or app_id_u
        else:
            # DLC → prefer corrected_title_id, else its own app_id
            tid_emit = corr_tid or app_id_u

        if not tid_emit:
            continue

        projection = redirect_meta.get("projection") if isinstance(redirect_meta.get("projection"), dict) else {}

        entry = {"id": tid_emit}

        # Optional overridden fields
        name = _first_value(ov.get("name"), projection.get("name"))
        if name:
            entry["name"] = name

        vnum = _version_to_int(_first_value(ov.get("version"), projection.get("version")))
        if vnum is not None:
            entry["version"] = vnum

        region = _first_value(ov.get("region"), projection.get("region"))
        if region:
            entry["region"] = region

        rd_int = _yyyymmdd_int(_first_value(ov.get("release_date"), projection.get("release_date")))
        if rd_int:
            entry["releaseDate"] = rd_int

        description = _first_value(ov.get("description"), projection.get("description"))
        if description:
            entry["description"] = description

        banner_url = _first_value(ov.get("bannerUrl"), projection.get("bannerUrl"))
        if banner_url:
            entry["bannerUrl"] = banner_url

        icon_url = _first_value(ov.get("iconUrl"), projection.get("iconUrl"))
        if icon_url:
            entry["iconUrl"] = icon_url

        category = _first_value(ov.get("category"), projection.get("category"))
        if category:
            entry["category"] = category

        total_bytes = sizes_by_app.get(app_id_u, 0)
        if total_bytes:
            entry["size"] = total_bytes

        titledb_map[tid_emit] = entry  # last-writer wins if collisions

        # Also publish metadata keyed by the original (non-redirected) TitleID so
        # Tinfoil can resolve installed titles that retain their original IDs.
        if corr_tid:
            if app_type == titles_lib.APP_TYPE_BASE:
                source_tid = (base_tid or app_id_u or "").strip().upper()
            else:
                source_tid = app_id_u  # DLC original id is its app_id
            source_tid = (source_tid or "").strip().upper()
            if source_tid and source_tid != tid_emit:
                source_entry = dict(entry)
                source_entry["id"] = source_tid
                titledb_map[source_tid] = source_entry

    return titledb_map

def _version_str_to_int(version_str):
    """
    Convert '1.2.3' -> 10203 (A*10000 + B*100 + C).
    Returns None if not parseable. Tinfoil wants numeric `version`.
    """
    if not version_str:
        return None
    parts = re.findall(r"\d+", str(version_str))
    if not parts:
        return None
    a, b, c = (int(p) for p in (parts + ["0", "0"])[:3])
    return a * 10000 + b * 100 + c

def _effective_corrected_title_id_for_file(f: Files) -> Optional[str]:
    """
    Return the corrected TitleID to present for this file, if any.
    Rules:
      - If the single linked App has an enabled override with corrected_title_id → use it.
      - If the App is an UPDATE, inherit the BASE app's override (same Title family).
      - DLCs do NOT inherit from BASE (only use their own override).
    """
    if not getattr(f, "apps", None) or len(f.apps) != 1:
        return None
    app = f.apps[0]

    # direct override on this app?
    ov = getattr(app, "override", None)
    if ov and getattr(ov, "enabled", False) and getattr(ov, "corrected_title_id", None):
        return ov.corrected_title_id.strip().upper()

    # UPDATE inherits BASE override
    if getattr(app, "app_type", None) == titles_lib.APP_TYPE_UPD:
        base = None
        # We already joined Titles; ask it for the base id and fetch the BASE app row
        base_tid = getattr(getattr(app, "title", None), "title_id", None)
        if base_tid:
            base = (
                db.session.query(Apps)
                .options(db.joinedload(Apps.override))
                .filter(Apps.app_id == base_tid, Apps.app_type == titles_lib.APP_TYPE_BASE)
                .first()
            )
        if base:
            bov = getattr(base, "override", None)
            if bov and getattr(bov, "enabled", False) and getattr(bov, "corrected_title_id", None):
                return bov.corrected_title_id.strip().upper()

    # DLCs do not inherit base override
    return None

def _with_title_id(presented_name: str, tid: str) -> str:
    """
    Ensure the presented filename contains [TITLE_ID] before the extension.
    If a [16-hex] token already exists, replace it; else insert it.
    """
    if not presented_name or not tid:
        return presented_name
    tid = tid.strip().upper()
    if not re.fullmatch(r"[0-9A-F]{16}", tid):
        return presented_name  # refuse to write a bad token

    root, ext = os.path.splitext(presented_name)
    token = f"[{tid}]"
    if _TITLE_ID_BRACKET.search(presented_name):
        return _TITLE_ID_BRACKET.sub(token, presented_name)
    # insert before extension (handles no-ext too)
    sep = "" if not root else " "
    return f"{root}{sep}{token}{ext or ''}"

def _compute_files_fingerprint_rows():
    """
    Tiny, stable summary of things that affect the shop 'files' section:
      - Files.id (stable order key)
      - size (emitted in the feed)
      - basename (affects the presented URL fragment)
    """
    rows = (
        db.session.query(Files.id, Files.size, Files.filepath)
        .order_by(Files.id.asc())
        .all()
    )
    fp = []
    for fid, size, path in rows:
        base = os.path.basename(path or "") if path else ""
        fp.append((int(fid), int(size or 0), base))
    return fp

def _current_shop_hash():
    # Overrides snapshot (hash + titledb_commit inside it)
    ov_snap = load_or_generate_overrides_snapshot()
    ov_hash = ov_snap.get("hash") or ""

    # Library snapshot (hash + titledb_commit inside it)
    lib_snap = load_json(LIBRARY_CACHE_FILE) or {}
    lib_hash = lib_snap.get("hash") or ""

    # Files fingerprint (sizes & basenames)
    files_fp = _compute_files_fingerprint_rows()

    shop_hash = {
        "overrides_hash": ov_hash,
        "library_hash": lib_hash,
        "files": files_fp,
    }
    return hashlib.sha256(
        json.dumps(shop_hash, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
