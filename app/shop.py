from db import *
from titles import (
    APP_TYPE_BASE,
    APP_TYPE_DLC,
    identify_app_id,
    load_titledb,
    unload_titledb
)
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
    shop_files = []

    # Preload relationships to avoid N+1
    rows = (
        db.session.query(Files)
        .options(
            db.joinedload(Files.apps).joinedload(Apps.overrides)
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
            try:
                _, app_type = identify_app_id(app.app_id)
            except Exception:
                app_type = None

            presented_tid = app.app_id
            
            if not app_type == APP_TYPE_DLC and getattr(app, "title", None):
                # base games: app_id == title_id, but prefer title.title_id for clarity if it's available
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
    Build `titledb` from enabled AppOverrides.

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
    # could probably do this cheaper from the cached overrides json now, without titledb.
    titledb_map = {}
    try:
        load_titledb()

        rows = (
            db.session.query(AppOverrides)
            .options(
                db.joinedload(AppOverrides.app).joinedload(Apps.title),
                db.joinedload(AppOverrides.app).joinedload(Apps.files),
            )
            .filter(AppOverrides.enabled.is_(True))
            .all()
        )

        # Build the set of relevant app_ids from the fetched overrides
        ov_app_ids = {
            (getattr(ov.app, "app_id", None) or ov.app_id or "").strip().upper()
            for ov in rows
            if (getattr(ov, "app", None) and (getattr(ov.app, "app_id", None) or getattr(ov, "app_id", None)))
        }

        if ov_app_ids:
            size_rows = (
                db.session
                    .query(Apps.app_id, func.sum(Files.size))
                    .join(Files.apps)
                    .filter(Files.size.isnot(None))
                    .filter(Apps.app_id.in_(ov_app_ids))
                    .group_by(Apps.app_id)
                    .all()
            )
        else:
            size_rows = []

        sizes_by_app = {
            (app_id or "").strip().upper(): int(total or 0)
            for app_id, total in size_rows
        }

        for ov in rows:
            app = getattr(ov, "app", None)
            if not app:
                continue

            # Family/base TitleID from Titles row when present; otherwise app.app_id
            base_tid = app.title.title_id if getattr(app, "title", None) else app.app_id
            app_id = app.app_id
            if not base_tid or not app_id:
                continue

            base_tid = base_tid.strip().upper()
            app_id = app_id.strip().upper()

            # Determine type (BASE/DLC relevant here)
            try:
                _, app_type = identify_app_id(app_id)
            except Exception:
                app_type = None
            if app_type not in (APP_TYPE_BASE, APP_TYPE_DLC):
                continue

            # Compute the key to emit and the entry["id"]
            if app_type == APP_TYPE_BASE:
                # BASE: corrected_title_id (if set) becomes the emitted TitleID; otherwise use base title id
                tid_emit = (ov.corrected_title_id.strip().upper() if ov.corrected_title_id else base_tid)
            else:
                # DLC: corrected_title_id (if set) becomes the emitted TitleID; otherwise use the DLC app_id
                tid_emit = (ov.corrected_title_id.strip().upper() if ov.corrected_title_id else app_id)

            # id field mirrors the emitted key for both base and dlc
            entry = {
                "id": tid_emit,
            }

            # Optional overridden fields
            if ov.name:
                entry["name"] = ov.name

            vnum = _version_str_to_int(ov.version)
            if vnum is not None:
                entry["version"] = vnum

            if ov.region:
                entry["region"] = ov.region

            if ov.release_date:
                rd = ov.release_date
                # handle date/datetime; skip/convert if string
                if hasattr(rd, "strftime"):
                    entry["releaseDate"] = int(rd.strftime("%Y%m%d"))

            if ov.description:
                entry["description"] = ov.description

            # Aggregate file sizes for this *app* (base or dlc)
            total_bytes = sizes_by_app.get(app_id, 0)
            if total_bytes:
                entry["size"] = total_bytes

            # Emit/overwrite this node (last writer wins — deterministic enough for our use)
            titledb_map[tid_emit] = entry

    finally:
        unload_titledb()

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
