from db import *
from titles import (
    APP_TYPE_BASE,
    APP_TYPE_DLC,
    identify_appId,
    load_titledb,
    unload_titledb
)
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_OAEP
from Crypto.Hash import SHA256
from Crypto.Cipher import AES
from sqlalchemy import func
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

        # Only attempt an ID correction when the file is not multicontent
        # and we can unambiguously pick the single linked app.
        corrected_tid = None
        if not f.multicontent and getattr(f, "apps", None) and len(f.apps) == 1:
            app = f.apps[0]
            ov = getattr(app, "overrides", None)
            if ov and ov.enabled and ov.corrected_title_id:
                corrected_tid = ov.corrected_title_id

        if corrected_tid:
            presented_name = _with_corrected_title_id(presented_name, corrected_tid)

        shop_files.append({
            "url": f"/api/get_game/{f.id}#{presented_name}",
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
    Build top-level `titledb` from enabled AppOverrides.
    Keys are Title IDs; values per Tinfoil:
      id (AppID/TitleID per type), name, version (int), region, releaseDate (yyyymmdd), description, size.

    If an override has corrected_title_id, we emit under that Title ID. For BASE, we also set entry["id"]
    to the corrected Title ID (Tinfoil expects base 'id' == Title ID). For DLC, 'id' remains the DLC app id.
    """
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

        for ov in rows:
            app = getattr(ov, "app", None)
            if not app:
                continue

            tid_db = (app.title.title_id if getattr(app, "title", None) else app.title_id)
            app_id = app.app_id
            if not tid_db or not app_id:
                continue

            tid_db = tid_db.strip().upper()
            app_id = app_id.strip().upper()

            # Determine type (BASE/DLC only relevant here)
            try:
                _, app_type = identify_appId(app_id)
            except Exception:
                app_type = None
            if app_type not in (APP_TYPE_BASE, APP_TYPE_DLC):
                continue

            # >>> Use corrected Title ID when provided
            tid_emit = (ov.corrected_title_id or tid_db).strip().upper()

            entry = {}

            # For BASE, Tinfoil treats 'id' as the base Title ID
            if app_type == APP_TYPE_BASE:
                entry["id"] = tid_emit
            else:
                # For DLC, keep the DLC app id
                entry["id"] = app_id

            if ov.name:
                entry["name"] = ov.name

            vnum = _version_str_to_int(ov.version)
            if vnum is not None:
                entry["version"] = vnum

            if ov.region:
                entry["region"] = ov.region

            if ov.release_date:
                entry["releaseDate"] = int(ov.release_date.strftime("%Y%m%d"))

            if ov.description:
                entry["description"] = ov.description

            total_bytes = (
                db.session.query(func.sum(Files.size))
                .join(Files.apps)
                .filter(Apps.app_id == app_id)
                .scalar()
            )
            if total_bytes:
                entry["size"] = int(total_bytes)

            # Keep one entry per Title ID; prefer BASE over DLC
            existing = titledb_map.get(tid_emit)
            if not existing:
                titledb_map[tid_emit] = {"entry": entry, "type": app_type}
            else:
                if existing["type"] == APP_TYPE_DLC and app_type == APP_TYPE_BASE:
                    titledb_map[tid_emit] = {"entry": entry, "type": app_type}

    finally:
        unload_titledb()

    return {tid: data["entry"] for tid, data in titledb_map.items()}

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

def _with_corrected_title_id(presented_name: str, corrected_tid: str) -> str:
    """
    Ensure the presented filename contains [CORRECTED_TID] before the extension.
    If a [16-hex] token already exists, replace it; else insert it.
    """
    if not presented_name:
        return presented_name
    root, ext = os.path.splitext(presented_name)
    token = f"[{corrected_tid.upper()}]"
    if _TITLE_ID_BRACKET.search(presented_name):
        return _TITLE_ID_BRACKET.sub(token, presented_name)
    # no token → append before extension
    return f"{root} {token}{ext or ''}"
