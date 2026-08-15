import atexit
import os
import shutil
import sys
import tempfile

# The app uses flat imports (`from constants import *`), so its package dir must be importable.
APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app"))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

# CI installs requirements.txt rather than the project, so the ownfoil package isn't importable
# from site-packages; add the repo root to reach ownfoil/cli.py.
REPO_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

# The capture harness defines the fixture shop the client tests replay against.
CAPTURE_DIR = os.path.join(os.path.dirname(__file__), "capture")
if CAPTURE_DIR not in sys.path:
    sys.path.insert(0, CAPTURE_DIR)

# constants.py resolves CONFIG_DIR at import, and app.py's module-level `app` - the one
# carrying the shop routes - takes its database URI from it right there. Redirect it before
# anything imports the app, or those tests run against the developer's own config dir.
_CONFIG_DIR = tempfile.mkdtemp(prefix="ownfoil-tests-")
os.environ["OWNFOIL_CONFIG_DIR"] = _CONFIG_DIR
atexit.register(shutil.rmtree, _CONFIG_DIR, True)

# nsz's Print module parses sys.argv at import time (fine under the app, whose argv is just
# the entrypoint). Hide pytest's argv from it so importing settings/titles/tasks doesn't abort.
sys.argv = sys.argv[:1]

import pytest


@pytest.fixture
def shop_app(tmp_path, monkeypatch):
    """The real app object, on an empty database and the capture harness' fixture library.

    The shop routes live on app.py's module-level `app` rather than on a create_app()
    instance, so these tests use that one: its database is the session config dir redirected
    above, rebuilt here for every test.
    """
    import types

    import fixture
    import settings as settings_mod
    import utils
    from app import app
    from db import db, init_db

    # Per-test settings, so one test's shop configuration can't reach the next.
    monkeypatch.setattr(settings_mod, "CONFIG_FILE", str(tmp_path / "settings.yaml"))
    monkeypatch.setattr(settings_mod, "KEYS_FILE", str(tmp_path / "keys.txt"))
    monkeypatch.setattr(settings_mod, "_cached_settings", None)

    # Process-global and keyed by (file, client), so download counts leak between tests.
    utils._throttle_registry.clear()

    init_db(app)
    library_root = fixture.build_library(str(tmp_path / "library"))
    with app.app_context():
        db.drop_all()
        db.create_all()
        fixture.seed_users()
        fixture.seed_library(library_root)

    return types.SimpleNamespace(app=app, client=app.test_client(), library_root=library_root)
