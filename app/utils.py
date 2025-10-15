import logging
import re
import threading
from functools import wraps
import json
import os
import tempfile
from flask import current_app, request
from flask_login import current_user
from werkzeug.exceptions import Forbidden

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
