from db import *
from titles import identify_appId, APP_TYPE_BASE
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_OAEP
from Crypto.Hash import SHA256
from Crypto.Cipher import AES
from sqlalchemy import func
import zstandard as zstd
import random
import re
import json

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

def build_titledb_from_overrides():
    """
    Build top-level `titledb` from enabled AppOverrides,
    but include only BASE app overrides (ignore DLC/Updates).
    Keys are Title IDs; values are Tinfoil fields:
      id, name, version(int), region, releaseDate(int yyyymmdd), description, size
    """
    titledb_map = {}

    # Preload joins to avoid N+1 queries
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
        if not ov.app:      # safety: should exist due to FK, but be defensive
            continue
        app = ov.app
        tid = (app.title.title_id if getattr(app, "title", None) else app.title_id)
        if not tid or not app.app_id:
            continue

        tid = tid.strip().upper()
        app_id = app.app_id.strip().upper()

        # Identify app type
        try:
            _, app_type = identify_appId(app_id)
        except Exception:
            app_type = None

        # Skip non-base apps (DLCs, updates, etc.)
        # For base apps, TitleID == AppID by definition
        if app_type != APP_TYPE_BASE or tid != app_id:
            continue

        entry = {"id": tid}

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

        # Sum all file sizes for this base app_id (do it in SQL)
        total_bytes = (
            db.session.query(func.sum(Files.size))
            .join(Files.apps)
            .filter(Apps.app_id == app_id)
            .scalar()
        )
        if total_bytes:
            entry["size"] = int(total_bytes)

        # Keep the first base override we see for this TitleID
        if tid not in titledb_map:
            titledb_map[tid] = entry

    return titledb_map

def gen_shop_files():
    shop_files = []
    files = get_shop_files()
    for file in files:
        shop_files.append({
            "url": f'/api/get_game/{file["id"]}#{file["filename"]}',
            'size': file["size"]
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
