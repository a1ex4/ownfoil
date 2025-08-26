import logging
import re
import threading
from functools import wraps
import json
import os
import tempfile

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