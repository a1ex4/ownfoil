import os
import sys

# The app uses flat imports (`from constants import *`), so its package dir must be importable.
APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app"))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

# CI installs requirements.txt rather than the project, so the ownfoil package isn't importable
# from site-packages; add the repo root to reach ownfoil/cli.py.
REPO_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

# nsz's Print module parses sys.argv at import time (fine under the app, whose argv is just
# the entrypoint). Hide pytest's argv from it so importing settings/titles/tasks doesn't abort.
sys.argv = sys.argv[:1]
