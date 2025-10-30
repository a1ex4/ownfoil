import os
import re
import json
import string
import threading
import contextlib
from typing import Optional
import titledb
from constants import *
from utils import *
from settings import *
from pathlib import Path
import logging
from functools import lru_cache

from nsz.Fs import Pfs0, Xci, Nsp, Nca, Type, factory
from nsz.nut import Keys

# Retrieve main logger
logger = logging.getLogger('main')

Pfs0.Print.silent = True

app_id_regex = FILENAME_APP_ID_RE.pattern
version_regex = VERSION_RE.pattern

# Global variables for TitleDB data
identification_in_progress_count = 0
_titles_db_loaded = False
_cnmts_db = None
_titles_db = None
_versions_db = None
_versions_txt_db = None
_titles_by_title_id = None
_ident_lock = threading.RLock()

_overrides_lock = threading.RLock()
_override_app_ids_cache: Optional[set[str]] = None
_override_corrected_ids_cache: Optional[set[str]] = None
_overrides_snapshot_mtime: Optional[float] = None
_overrides_cache_path: Optional[str] = None

_TRAILING_BRACKET_RE = re.compile(r"\s*\[[^\]]*\]\s*$")
_PUNCT_TRANSLATION = str.maketrans({ch: " " for ch in string.punctuation})

def _load_override_id_sets() -> tuple[set[str], set[str]]:
    """
    Load and cache the set of Title IDs that have active overrides.
    Uses the generated overrides snapshot to stay lightweight and avoid
    introducing a DB dependency in this module.
    """
    global _override_app_ids_cache
    global _override_corrected_ids_cache
    global _overrides_snapshot_mtime
    global _overrides_cache_path

    if _overrides_cache_path is None:
        candidates = [OVERRIDES_CACHE_FILE]
        # Allow deployments that keep data/ alongside app/ instead of under it.
        project_data_path = os.path.join(os.path.dirname(APP_DIR), "data", "cache", "overrides.json")
        if project_data_path not in candidates:
            candidates.append(project_data_path)

        for candidate in candidates:
            if os.path.exists(candidate):
                _overrides_cache_path = candidate
                break
        else:
            # Fall back to the primary location even if it does not exist yet.
            _overrides_cache_path = OVERRIDES_CACHE_FILE

    with _overrides_lock:
        try:
            stat = os.stat(_overrides_cache_path)
            current_mtime = stat.st_mtime
        except FileNotFoundError:
            _override_app_ids_cache = set()
            _override_corrected_ids_cache = set()
            _overrides_snapshot_mtime = None
            return _override_app_ids_cache, _override_corrected_ids_cache

        if (
            _override_app_ids_cache is not None
            and _override_corrected_ids_cache is not None
            and _overrides_snapshot_mtime == current_mtime
        ):
            return _override_app_ids_cache, _override_corrected_ids_cache

        try:
            with open(_overrides_cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            # Snapshot may be mid-write; default to empty and try again later.
            _override_app_ids_cache = set()
            _override_corrected_ids_cache = set()
            _overrides_snapshot_mtime = current_mtime
            return _override_app_ids_cache, _override_corrected_ids_cache

        payload = data.get("payload", {})
        items = payload.get("items", []) if isinstance(payload, dict) else []

        app_ids: set[str] = set()
        corrected_ids: set[str] = set()

        for item in items:
            if not isinstance(item, dict):
                continue
            if not item.get("enabled", False):
                continue

            app_id_raw = item.get("app_id")
            if isinstance(app_id_raw, str):
                normalized_app_id = normalize_id(app_id_raw, "app")
                if normalized_app_id:
                    app_ids.add(normalized_app_id[:16])

            corrected_raw = item.get("corrected_title_id")
            if isinstance(corrected_raw, str):
                normalized_corrected = normalize_id(corrected_raw, "title")
                if normalized_corrected:
                    corrected_ids.add(normalized_corrected)

        _override_app_ids_cache = app_ids
        _override_corrected_ids_cache = corrected_ids
        _overrides_snapshot_mtime = current_mtime
        return app_ids, corrected_ids

def _override_exists_for_title_id(title_id: str) -> bool:
    tid = normalize_id(title_id, "title")
    if not tid:
        return False
    app_ids, corrected_ids = _load_override_id_sets()
    return tid in app_ids or tid in corrected_ids

def identification_in_progress() -> bool:
    with _ident_lock:
        return identification_in_progress_count > 0

@contextlib.contextmanager
def identification_session(tag: str = ""):
    """Use this to bracket any identification work. Guarantees decrement."""
    global identification_in_progress_count
    with _ident_lock:
        identification_in_progress_count += 1
    try:
        yield
    finally:
        with _ident_lock:
            identification_in_progress_count -= 1

@contextlib.contextmanager
def titledb_session(tag: str = ""):
    """Wrap TitleDB work in a scoped session that loads then unloads cleanly."""
    loaded_here = False
    with identification_session(tag):
        with _ident_lock:
            already_loaded = _titles_db_loaded
        load_titledb()
        loaded_here = not already_loaded
        yield
    if loaded_here:
        unload_titledb()

def get_dirs_and_files(path):
    entries = os.listdir(path)
    allFiles = []
    allDirs = []

    for entry in entries:
        fullPath = os.path.join(path, entry)
        if os.path.isdir(fullPath):
            allDirs.append(fullPath)
            dirs, files = get_dirs_and_files(fullPath)
            allDirs += dirs
            allFiles += files
        elif fullPath.split('.')[-1] in ALLOWED_EXTENSIONS:
            allFiles.append(fullPath)
    return allDirs, allFiles

def get_app_id_from_filename(filename):
    app_id_match = re.search(app_id_regex, filename)
    return app_id_match[1] if app_id_match is not None else None

def get_version_from_filename(filename):
    version_match = re.search(version_regex, filename)
    return version_match[1] if version_match is not None else None

def get_title_id_from_app_id(app_id, app_type):
    base_id = app_id[:-3]
    if app_type == APP_TYPE_UPD:
        title_id = base_id + '000'
    elif app_type == APP_TYPE_DLC:
        title_id = hex(int(base_id, base=16) - 1)[2:].rjust(len(base_id), '0') + '000'
    return title_id.upper()

def get_file_size(filepath):
    return os.path.getsize(filepath)

def get_file_info(filepath):
    filedir, filename = os.path.split(filepath)
    extension = filename.split('.')[-1]
    
    compressed = False
    if extension in ['nsz', 'xcz']:
        compressed = True

    return {
        'filepath': filepath,
        'filedir': filedir,
        'filename': filename,
        'extension': extension,
        'compressed': compressed,
        'size': get_file_size(filepath),
    }

def identify_app_id(app_id):
    app_id = app_id.lower()
    
    global _cnmts_db
    if _cnmts_db is None:
        logger.error("cnmts_db is not loaded. Call load_titledb first.")
        return None, None

    if app_id in _cnmts_db:
        app_id_keys = list(_cnmts_db[app_id].keys())
        if len(app_id_keys):
            app = _cnmts_db[app_id][app_id_keys[-1]]
            
            if app['titleType'] == 128:
                app_type = APP_TYPE_BASE
                title_id = app_id.upper()
            elif app['titleType'] == 129:
                app_type = APP_TYPE_UPD
                if 'otherApplicationId' in app:
                    title_id = app['otherApplicationId'].upper()
                else:
                    title_id = get_title_id_from_app_id(app_id, app_type)
            elif app['titleType'] == 130:
                app_type = APP_TYPE_DLC
                if 'otherApplicationId' in app:
                    title_id = app['otherApplicationId'].upper()
                else:
                    title_id = get_title_id_from_app_id(app_id, app_type)
        else:
            logger.warning(f'{app_id} has no keys in cnmts_db, fallback to default identification.')
            if app_id.endswith('000'):
                app_type = APP_TYPE_BASE
                title_id = app_id
            elif app_id.endswith('800'):
                app_type = APP_TYPE_UPD
                title_id = get_title_id_from_app_id(app_id, app_type)
            else:
                app_type = APP_TYPE_DLC
                title_id = get_title_id_from_app_id(app_id, app_type)
    else:
        logger.warning(f'{app_id} not in cnmts_db, fallback to default identification.')
        if app_id.endswith('000'):
            app_type = APP_TYPE_BASE
            title_id = app_id
        elif app_id.endswith('800'):
            app_type = APP_TYPE_UPD
            title_id = get_title_id_from_app_id(app_id, app_type)
        else:
            app_type = APP_TYPE_DLC
            title_id = get_title_id_from_app_id(app_id, app_type)
    
    return title_id.upper(), app_type

@lru_cache(maxsize=4096)
def _lookup_title_id_by_normalized(norm: str) -> Optional[str]:
    """Scan TitleDB for a normalized name match."""
    global _titles_by_title_id
    if _titles_by_title_id is None:
        logger.error("titles_by_title_id is not loaded. Call load_titledb first.")
        return None
    if not norm:
        return None
    best_match = None
    for tid, rec in _titles_by_title_id.items():
        if not tid or not isinstance(rec, dict):
            continue
        name_raw = (
            rec.get("name")
            or rec.get("Name")
            or rec.get("title")
            or rec.get("Title")
            or None
        )
        if not name_raw:
            continue
        candidate = normalize_display_name(name_raw)
        if candidate == norm:
            if best_match is None or tid < best_match:
                best_match = tid
    return best_match

def load_titledb():
    global _cnmts_db
    global _titles_db
    global _versions_db
    global _versions_txt_db
    global _titles_db_loaded
    global _titles_by_title_id

    with _ident_lock:
        if _titles_db_loaded:
            return
        logger.info("Loading TitleDBs into memory...")
        app_settings = load_settings()
        with open(os.path.join(TITLEDB_DIR, 'cnmts.json')) as f:
            _cnmts_db = json.load(f)

        with open(os.path.join(TITLEDB_DIR, titledb.get_region_titles_file(app_settings))) as f:
            _titles_db = json.load(f)

        with open(os.path.join(TITLEDB_DIR, 'versions.json')) as f:
            _versions_db = json.load(f)

        _versions_txt_db = {}
        with open(os.path.join(TITLEDB_DIR, 'versions.txt')) as f:
            for line in f:
                line_strip = line.rstrip("\n")
                app_id, rightsId, version = line_strip.split('|')
                if not version:
                    version = "0"
                _versions_txt_db[app_id] = version

        # build fast by_title_id map from _titles_db ----
        _titles_by_title_id = {}
        for _k, rec in _titles_db.items():
            tid = (rec.get('id') or rec.get('title_id') or '').upper()
            if len(tid) == 16:
                _titles_by_title_id[tid] = rec

        _lookup_title_id_by_normalized.cache_clear()
        _titles_db_loaded = True
        logger.info("TitleDBs loaded.")

@debounce(30)
def unload_titledb():
    global _cnmts_db
    global _titles_db
    global _versions_db
    global _versions_txt_db
    global _titles_db_loaded
    global _titles_by_title_id

    if identification_in_progress():
        logger.debug('Identification still in progress, not unloading TitleDB.')
        return

    with _ident_lock:
        if identification_in_progress():
            # Identification may have started during debounce delay
            logger.debug('Identification restarted during unload debounce, skipping unload.')
            return
        logger.info("Unloading TitleDBs from memory...")
        _cnmts_db = None
        _titles_db = None
        _versions_db = None
        _versions_txt_db = None
        _titles_by_title_id = None
        _lookup_title_id_by_normalized.cache_clear()
        _titles_db_loaded = False
    logger.info("TitleDBs unloaded.")

def identify_file_from_filename(filename):
    title_id = None
    app_id = None
    app_type = None
    version = None
    errors = []

    app_id = get_app_id_from_filename(filename)
    if app_id is None:
        errors.append('Could not determine App ID from filename, pattern [APPID] not found. Title ID and Type cannot be derived.')
    else:
        title_id, app_type = identify_app_id(app_id)

    version = get_version_from_filename(filename)
    if version is None:
        errors.append('Could not determine version from filename, pattern [vVERSION] not found.')
    
    error = ' '.join(errors)
    return app_id, title_id, app_type, version, error

def get_cnmts(container):
    cnmts = []
    if isinstance(container, Nsp.Nsp):
        try:
            cnmt = container.cnmt()
            cnmts.append(cnmt)
        except Exception as e:
            logger.warning('CNMT section not found in Nsp.')

    elif isinstance(container, Xci.Xci):
        container = container.hfs0['secure']
        for nspf in container:
            if isinstance(nspf, Nca.Nca) and nspf.header.contentType == Type.Content.META:
                cnmts.append(nspf)

    return cnmts

def extract_meta_from_cnmt(cnmt_sections):
    contents = []
    for section in cnmt_sections:
        if isinstance(section, Pfs0.Pfs0):
            Cnmt = section.getCnmt()
            titleType = APP_TYPE_MAP[Cnmt.titleType]
            titleId = Cnmt.titleId.upper()
            version = Cnmt.version
            contents.append((titleType, titleId, version))
    return contents

def identify_file_from_cnmt(filepath):
    contents = []
    container = factory(Path(filepath).resolve())
    container.open(filepath, 'rb', meta_only=True)
    try:
        for cnmt_sections in get_cnmts(container):
            contents += extract_meta_from_cnmt(cnmt_sections)

    finally:
        container.close()

    return contents

def identify_file(filepath):
    filename = os.path.split(filepath)[-1]
    contents = []
    success = True
    error = ''
    if Keys.keys_loaded:
        identification = 'cnmt'
        try:
            cnmt_contents = identify_file_from_cnmt(filepath)
            if not cnmt_contents:
                error = 'No content found in NCA containers.'
                success = False
            else:
                for content in cnmt_contents:
                    app_type, app_id, version = content
                    if app_type != APP_TYPE_BASE:
                        # need to get the title ID from cnmts
                        title_id, app_type = identify_app_id(app_id)
                    else:
                        title_id = app_id
                    contents.append((title_id, app_type, app_id, version))
        except Exception as e:
            logger.error(f'Could not identify file {filepath} from metadata: {e}')
            error = str(e)
            success = False

    else:
        identification = 'filename'
        app_id, title_id, app_type, version, error = identify_file_from_filename(filename)
        if not error:
            contents.append((title_id, app_type, app_id, version))
        else:
            success = False

    if contents:
        contents = [{
            'title_id': c[0],
            'app_id': c[2],
            'type': c[1],
            'version': c[3],
            } for c in contents]
    return identification, success, contents, error

def title_id_exists(title_id: Optional[str]) -> bool:
    """True if TitleDB has a record for this Title ID."""
    global _titles_by_title_id
    if _titles_by_title_id is None:
        logger.error("titles_by_title_id is not loaded. Call load_titledb first.")
        return False
    tid = normalize_id(title_id, "title")
    if not tid:
        return False
    return tid in _titles_by_title_id

def clean_display_name(raw: Optional[str]) -> str:
    """
    Produce a display-friendly name from filenames or TitleDB strings.
    Strips trailing bracket segments and collapses whitespace.
    """
    if not raw:
        return ""
    text = str(raw)
    while True:
        trimmed = _TRAILING_BRACKET_RE.sub("", text)
        if trimmed == text:
            break
        text = trimmed
    # Collapse internal whitespace
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def normalize_display_name(raw: Optional[str]) -> str:
    """
    Deterministic normalization used for comparisons.
    - Uses clean_display_name base
    - Strips punctuation
    - Collapses whitespace
    - Uppercases
    """
    cleaned = clean_display_name(raw)
    if not cleaned:
        return ""
    no_punct = cleaned.translate(_PUNCT_TRANSLATION)
    collapsed = re.sub(r"\s+", " ", no_punct).strip()
    return collapsed.upper()

def find_title_id_by_normalized_name(name: Optional[str]) -> Optional[str]:
    """
    Look up a Title ID by normalized name (already cleaned/uppercase optional).
    Returns the Title ID string if found.
    """
    if not name:
        return None
    key = str(name).strip().upper()
    if not key:
        return None
    return _lookup_title_id_by_normalized(key)

def get_game_info(title_id: Optional[str]):
    """
    Retrieve a TitleDB record for a given Title ID.

    - Normalizes ID (accepts '0x' prefix, lowercase, etc.)
    - Returns a dict with name, bannerUrl, iconUrl, id, category, region, description, release_date (normalized).
    - Returns minimal fallback if not found/invalid.
    """
    global _titles_by_title_id
    if _titles_by_title_id is None:
        logger.error("titles_by_title_id is not loaded. Call load_titledb first.")
        return None

    tid = normalize_id(title_id, "title")
    if not tid:
        logger.error(f"Invalid Title ID format: {title_id!r}")
        return {
            "name": None,
            "id": title_id,
            "category": "",
            "region": None,
            "description": None,
            "release_date": None,
        }

    try:
        title_info = _titles_by_title_id.get(tid)
        if not title_info:
            fallback = {
                "name": None,
                "id": tid,
                "category": "",
                "region": None,
                "description": None,
                "release_date": None,
            }
            if not _override_exists_for_title_id(tid):
                if tid.startswith('05'):
                    logger.info(f"Homebrew title not identified: {tid}")
                else:
                    logger.warning(f"Title ID not found in titledb: {tid}")
            return fallback

        # Accept multiple spellings & normalize immediately
        release_raw = (
            title_info.get("release_date")
            or title_info.get("releaseDate")
            or None
        )
        release_date = normalize_release_date(release_raw)

        description = (
            title_info.get("description")
            or title_info.get("longDescription")
            or title_info.get("desc")
            or title_info.get("overview")      # ← include what you previously pulled from raw
            or None
        )

        return {
            "name":       (title_info.get("name") or "").strip() or None,
            "bannerUrl":  title_info.get("bannerUrl"),
            "iconUrl":    title_info.get("iconUrl"),
            "id":         title_info.get("id") or tid,
            "category":   title_info.get("category", ""),
            "region":     title_info.get("region"),
            "description": description,
            "release_date": release_date,
        }

    except Exception:
        logger.error(f"Exception retrieving Title ID from titledb: {title_id}")
        return {
            "name": None,
            "id": title_id,
            "category": "",
            "region": None,
            "description": None,
            "release_date": None,
        }

def get_update_number(version):
    return int(version)//65536

def get_all_existing_versions(titleid):
    global _versions_db
    if _versions_db is None:
        logger.error("versions_db is not loaded. Call load_titledb first.")
        return []

    titleid = titleid.lower()
    if titleid not in _versions_db:
        # print(f'Title ID not in versions.json: {titleid.upper()}')
        return []

    versions_from_db = _versions_db[titleid].keys()
    return [
        {
            'version': int(version_from_db),
            'update_number': get_update_number(version_from_db),
            'release_date': normalize_release_date(_versions_db[titleid][str(version_from_db)]),
        }
        for version_from_db in versions_from_db
    ]

def get_all_app_existing_versions(app_id):
    global _cnmts_db
    if _cnmts_db is None:
        logger.error("cnmts_db is not loaded. Call load_titledb first.")
        return None

    app_id = app_id.lower()
    if app_id in _cnmts_db:
        versions_from_cnmts_db = _cnmts_db[app_id].keys()
        if len(versions_from_cnmts_db):
            return sorted(versions_from_cnmts_db)
        else:
            logger.warning(f'No keys in cnmts.json for app ID: {app_id.upper()}')
            return None
    else:
        # print(f'DLC app ID not in cnmts.json: {app_id.upper()}')
        return None

def get_all_existing_dlc(title_id):
    global _cnmts_db
    if _cnmts_db is None:
        logger.error("cnmts_db is not loaded. Call load_titledb first.")
        return []

    title_id = title_id.lower()
    dlcs = []
    for app_id in _cnmts_db.keys():
        for version, version_description in _cnmts_db[app_id].items():
            if version_description.get('titleType') == 130 and version_description.get('otherApplicationId') == title_id:
                if app_id.upper() not in dlcs:
                    dlcs.append(app_id.upper())
    return dlcs

def get_titledb_commit_hash() -> str:
    """
    Return the current TitleDB commit hash (from .latest file) as a string.
    Returns an empty string if unavailable or unreadable.
    """
    try:
        from constants import TITLEDB_DIR
    except Exception:
        return ""

    commit_path = os.path.join(TITLEDB_DIR, ".latest")
    try:
        if os.path.isfile(commit_path):
            with open(commit_path, "r", encoding="utf-8") as f:
                return (f.read() or "").strip()
    except Exception:
        pass

    return ""
