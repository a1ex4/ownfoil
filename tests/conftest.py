import os
import sys

# The app uses flat imports (`from constants import *`), so its package dir must be importable.
APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app"))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)
