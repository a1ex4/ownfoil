from db import *
from overrides import (
    build_override_index,
    load_or_generate_overrides_snapshot
)
from titles import APP_TYPE_BASE, APP_TYPE_DLC
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
import logging

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

def gen_shop_files():
    """
    Build the 'files' section for the custom index.
    If a single-content file’s linked app has an enabled override with
    corrected_title_id, present the URL with that [TITLEID] token so Tinfoil
    discovers it under the corrected ID.
    """
    logger.info("Generating Tinfoil Shop Feed")
    shop_files = []

    # Preload relationships to avoid N+1
    rows = (
        db.session.query(Files)
        .options(
            db.joinedload(Files.apps).joinedload(Apps.overrides),
            db.joinedload(Files.apps).joinedload(Apps.title),
        )
        .all()
    )

    for f in rows:
        presented_name = f.filename or os.path.basename(f.filepath) or "file.nsp"        
        presented_tid = None
        # Only attempt an ID correction when the file is not multicontent
        # and we can unambiguously pick the single linked app.
        if getattr(f, 'apps', None) and len(f.apps) == 1:
            app = f.apps[0]
            app_type = getattr(app, "app_type", None)
            # default to the app_id; adjust for base titles if we have Titles linkage
            presented_tid = app.app_id
            if app_type != APP_TYPE_DLC and getattr(app, "title", None):
                # base games: prefer Titles.title_id when available
                presented_tid = app.title.title_id

            if _should_override_title_id(f):
                if app.overrides:
                    presented_tid = app.overrides.corrected_title_id

        if presented_tid:
            presented_name = _with_title_id(presented_name, presented_tid)

        shop_files.append({
            "url": f"/api/get_game/{f.id}#{quote(presented_name)}",
            "size": f.size or 0
        })

    return shop_files

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

def build_titledb_from_overrides():
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

    # Try to pull from the cached overrides snapshot if available
    overrides_by_app = {}
    try:
        snap = load_or_generate_overrides_snapshot()
        items = (snap or {}).get("payload", {}).get("items", []) or []
        for it in items:
            app_id = it.get("app_id", "").strip().upper()
            if not app_id:
                continue
            overrides_by_app[app_id] = {
                "corrected_title_id": (it.get("corrected_title_id") or it.get("correctedTitleId") or None),
                "name": it.get("name"),
                "version": it.get("version"),
                "region": it.get("region"),
                "release_date": it.get("release_date") or it.get("releaseDate"),
                "description": it.get("description"),
            }
    except Exception:
        # Snapshot unavailable/corrupt; we'll fall back
        overrides_by_app = {}

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
        .join(Files.apps)              # Apps <-> Files M2M
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
        if app_type not in (APP_TYPE_BASE, APP_TYPE_DLC):
            # Unknown type; skip to avoid guessing
            continue

        corr_tid = (ov.get("corrected_title_id") or "").strip().upper() or None

        if app_type == APP_TYPE_BASE:
            # BASE → prefer corrected_title_id, else Titles.title_id, else app_id as last resort
            tid_emit = corr_tid or base_tid or app_id_u
        else:
            # DLC → prefer corrected_title_id, else its own app_id
            tid_emit = corr_tid or app_id_u

        if not tid_emit:
            continue

        entry = {"id": tid_emit}

        # Optional overridden fields
        if ov.get("name"):
            entry["name"] = ov["name"]

        vnum = _version_to_int(ov.get("version"))
        if vnum is not None:
            entry["version"] = vnum

        if ov.get("region"):
            entry["region"] = ov["region"]

        rd_int = _yyyymmdd_int(ov.get("release_date"))
        if rd_int:
            entry["releaseDate"] = rd_int

        if ov.get("description"):
            entry["description"] = ov["description"]

        total_bytes = sizes_by_app.get(app_id_u, 0)
        if total_bytes:
            entry["size"] = total_bytes

        titledb_map[tid_emit] = entry  # last-writer wins if collisions

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

def _should_override_title_id(f: Files) -> bool:
    """
    Determine if a single-content file's linked app has an enabled override
    with corrected_title_id. If so, return True; else False.
    """
    # Only attempt an ID correction when the file is not multicontent
    # and we can unambiguously pick the single linked app.
    if (
        getattr(f, "multicontent", False)
        or not getattr(f, "apps", None)
        or len(f.apps) != 1
        or not getattr(f.apps[0], "overrides", None)
    ):
        return False

    app = f.apps[0]

    if app.overrides.enabled and app.overrides.corrected_title_id:
        return True

    return False

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
