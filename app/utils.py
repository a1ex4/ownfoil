import logging
import re
import threading
from functools import wraps
import json
import os
import tempfile

from constants import *

logger = logging.getLogger("main")

# Global lock for all JSON writes in this process
_json_write_lock = threading.Lock()

# Custom logging formatter to support colors
class ColoredFormatter(logging.Formatter):
    # Define color codes
    COLORS = {
        'DEBUG': '\033[94m',   # Blue
        'INFO': '\033[92m',    # Green
        'WARNING': '\033[93m', # Yellow
        'ERROR': '\033[91m',   # Red
        'CRITICAL': '\033[95m' # Magenta
    }
    RESET = '\033[0m'  # Reset color

    def format(self, record):
        # Add color to the log level name
        levelname = record.levelname
        if levelname in self.COLORS:
            record.levelname = f"{self.COLORS[levelname]}{levelname}{self.RESET}"
        
        return super().format(record)
    
# Filter to remove date from http access logs
class FilterRemoveDateFromWerkzeugLogs(logging.Filter):
    # '192.168.0.102 - - [30/Jun/2024 01:14:03] "%s" %s %s' -> '192.168.0.102 - "%s" %s %s'
    pattern: re.Pattern = re.compile(r' - - \[.+?] "')

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.pattern.sub(' - "', record.msg)
        return True


def debounce(wait):
    """Decorator that postpones a function's execution until after `wait` seconds
    have elapsed since the last time it was invoked."""
    def decorator(fn):
        @wraps(fn)
        def debounced(*args, **kwargs):
            def call_it():
                fn(*args, **kwargs)
            if hasattr(debounced, '_timer'):
                debounced._timer.cancel()
            debounced._timer = threading.Timer(wait, call_it)
            debounced._timer.start()
        return debounced
    return decorator

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ['keys', 'txt']

def safe_write_json(path, data, **dump_kwargs):
    with _json_write_lock:
        dirpath = os.path.dirname(path) or "."
        # Create temporary file in same directory
        with tempfile.NamedTemporaryFile("w", dir=dirpath, delete=False, encoding="utf-8") as tmp:
            tmp_path = tmp.name
            json.dump(data, tmp, ensure_ascii=False, indent=2, **dump_kwargs)
            tmp.flush()
            os.fsync(tmp.fileno())  # flush to disk
        # Atomically replace target file
        os.replace(tmp_path, path)

def save_json(data, path, **dump_kwargs):
    """
    Save JSON atomically using safe_write_json, ensuring the parent
    directory exists. Accepts extra json.dump kwargs.
    """
    dirpath = os.path.dirname(path) or "."
    os.makedirs(dirpath, exist_ok=True)
    safe_write_json(path, data, **dump_kwargs)

def load_json(path, default=None):
    """
    Load JSON from disk. Returns `default` if the file is missing.
    Raises on decode or IO errors so callers can handle/report.
    """
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def merge_dicts_recursive(source, destination):
    """
    Recursively merges source dictionary into destination dictionary.
    Adds missing keys from source to destination.
    Returns True if any changes were made, False otherwise.
    """
    changed = False
    for key, value in source.items():
        if key not in destination:
            destination[key] = value
            changed = True
            logging.getLogger('main').debug(f'Added missing default setting: {key}')
        elif isinstance(value, dict) and isinstance(destination[key], dict):
            if merge_dicts_recursive(value, destination[key]):
                changed = True
        # If key exists but types are different, or if it's not a dict and not equal,
        # we don't overwrite existing settings unless explicitly told to.
        # For this task, we only add missing keys.
    return changed

def delete_empty_folders(path):
    """
    Recursively deletes empty folders starting from the given path.
    Considers folders containing only hidden files as empty.
    """
    if not os.path.isdir(path):
        return

    # Loop until no more empty directories are found and deleted in a pass
    while True:
        deleted_any_in_pass = False
        # Traverse from bottom up to ensure child empty folders are deleted first
        for dirpath, dirnames, filenames in os.walk(path, topdown=False):
            # Check if the directory is truly empty (no subdirectories and no files)
            if not dirnames and not filenames:
                try:
                    os.rmdir(dirpath)
                    logging.getLogger('main').debug(f"Deleted empty directory: {dirpath}")
                    deleted_any_in_pass = True
                except OSError as e:
                    logging.getLogger('main').error(f"Error deleting directory {dirpath}: {e}")
        
        # After a full pass, check if the root path itself is now empty and can be deleted
        # This handles cases where the initial 'path' becomes empty after its children are removed
        if not os.listdir(path) and os.path.isdir(path):
            try:
                os.rmdir(path)
                logging.getLogger('main').debug(f"Deleted empty root directory: {path}")
                deleted_any_in_pass = True
            except OSError as e:
                logging.getLogger('main').error(f"Error deleting root directory {path}: {e}")

        if not deleted_any_in_pass:
            break # No more empty directories found in this pass, so we are done

def normalize_release_date(value):
    """
    Normalize a release date into 'YYYY-MM-DD' format.

    Accepts:
      - int like 20230915
      - str '20230915' or '2023-09-15'
      - str containing extra characters (e.g. '2023/09/15', '20230915 (US)')
      - 'Unknown' / None / empty string

    Returns:
      - 'YYYY-MM-DD' if recognized
      - None if unknown or invalid
    """
    if value is None:
        return None

    try:
        s = str(value).strip()
        if not s or s.lower() == "unknown":
            return None

        # Integer-like (either int or numeric string)
        if s.isdigit() and len(s) == 8:
            return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"

        # Already ISO-like
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            return s

        # Fallback: extract digits from mixed strings (e.g. '2023/09/15 (JP)')
        digits = "".join(ch for ch in s if ch.isdigit())
        if len(digits) == 8:
            return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
    except Exception:
        pass

    return None

def normalize_id(raw: str | None, kind: str = "title") -> str | None:
    """
    Normalize and validate a Nintendo ID (Title ID or App ID).

    Args:
        raw:  The raw ID string (may include '0x' prefix, lowercase, or spacing)
        kind: Either 'title' (default, 16 hex) or 'app' (16 or 32 hex)

    Returns:
        - Normalized uppercase ID string if valid
        - None if invalid or empty

    Example:
        normalize_id('0x0100abcd1234ef00')     -> '0100ABCD1234EF00'
        normalize_id('0100abcd1234ef00', 'app') -> '0100ABCD1234EF00'
        normalize_id('0100abcd1234ef001122334455667788', 'app')
        -> '0100ABCD1234EF001122334455667788'
    """
    if not raw:
        return None

    s = str(raw).strip().upper()
    if s.startswith("0X"):
        s = s[2:]

    if kind == "title":
        return s if TITLE_ID_RE.fullmatch(s) else None
    elif kind == "app":
        return s if APP_ID_RE.fullmatch(s) else None
    else:
        raise ValueError(f"Unknown ID kind: {kind!r} (expected 'title' or 'app')")

def invalidate_cache(path: str) -> bool:
    """
    Delete a cache file if it exists.
    Returns True if removed, False if it wasn't there.
    """
    try:
        os.remove(path)
        logger.info(f"Invalidated: {path}")
        return True
    except FileNotFoundError:
        return False
    except Exception as e:
        logger.warning(f"Failed to invalidate {path}: {e}")
        return False

def generate_snapshot(path: str):
    """
    Regenerate a known cache snapshot given its file path.
    Dispatches to the correct builder so the cache is warm for next request.
    """
    
    try:
        if path == LIBRARY_CACHE_FILE:
            from library import load_or_generate_library
            load_or_generate_library()
            logger.info(f"Regenerated library snapshot: {path}")
        elif path == OVERRIDES_CACHE_FILE:
            from overrides import load_or_generate_overrides_snapshot
            load_or_generate_overrides_snapshot()
            logger.info(f"Regenerated overrides snapshot: {path}")
        elif path == SHOP_CACHE_FILE:
            from shop import load_or_generate_shop_snapshot
            load_or_generate_shop_snapshot()
            logger.info(f"Regenerated shop snapshot: {path}")
        else:
            logger.warning(f"Unknown snapshot path: {path}")
    except Exception as e:
        logger.error(f"Failed to regenerate {path}: {e}")

def invalidate_and_regenerate_cache(path: str):
    """
    Invalidate and regenerate a known cache snapshot given its file path.
    """
    invalidate_cache(path)
    generate_snapshot(path)

def regenerate_all_caches():
    """
    Invalidate and regenerate all known cache snapshots.
    """
    for path in (LIBRARY_CACHE_FILE, OVERRIDES_CACHE_FILE, SHOP_CACHE_FILE):
        logger.info(f"[cache] refreshing {os.path.basename(path)}")
        invalidate_and_regenerate_cache(path)
