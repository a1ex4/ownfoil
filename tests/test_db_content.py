"""
Verifies the DB content produced by a full scan + identify cycle matches a committed baseline.

Uses minimal fixture titledb (tests/fixtures/titledb/) and game files
(tests/fixtures/games/) so the result is deterministic and independent of the
real titledb download.  The pipeline is driven directly — no scheduler, no
file watcher, no 10-second debounce — which makes the test fast and
synchronous.

Cross-branch workflow
---------------------
1. On develop (or after intentional DB-content changes):
       pytest --update-content
   Commit the updated tests/fixtures/db_content.json.

2. On any feature branch:
       pytest tests/test_db_content.py
   Fails if the post-pipeline DB content diverges from the baseline.
"""

import json
import logging
import os
import pytest
from pathlib import Path
from sqlalchemy import create_engine, text

CONTENT_FIXTURE = Path(__file__).parent / 'fixtures' / 'db_content.json'
TITLEDB_FIXTURE = Path(__file__).parent / 'fixtures' / 'titledb'
GAMES_FIXTURE = Path(__file__).parent / 'fixtures' / 'games'

# Suppress noisy startup logs during test runs
logging.disable(logging.WARNING)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def extract_db_content(db_uri: str) -> dict:
    """
    Return a normalized, sorted snapshot of the post-pipeline DB content.

    Excluded (non-deterministic or environment-dependent):
      - files.id, files.library_id, files.filepath, files.folder, files.last_attempt
      - libraries.id, libraries.last_scan
      - apps.id, apps.title_id (FK integer)
      - titles.id
    """
    engine = create_engine(db_uri)
    with engine.connect() as conn:
        files = [
            {
                'filename': r['filename'],
                'extension': r['extension'],
                'size': r['size'],
                'identified': bool(r['identified']),
                'identification_type': r['identification_type'],
                'identification_error': r['identification_error'],
                'identification_attempts': r['identification_attempts'],
                'compressed': bool(r['compressed']) if r['compressed'] is not None else False,
                'multicontent': bool(r['multicontent']) if r['multicontent'] is not None else False,
                'nb_content': r['nb_content'],
            }
            for r in conn.execute(text('SELECT * FROM files')).mappings()
        ]

        titles = [
            {
                'title_id': r['title_id'],
                'have_base': bool(r['have_base']),
                'up_to_date': bool(r['up_to_date']),
                'complete': bool(r['complete']),
            }
            for r in conn.execute(text('SELECT * FROM titles')).mappings()
        ]

        apps = [
            {
                'app_id': r['app_id'],
                'app_version': r['app_version'],
                'app_type': r['app_type'],
                'owned': bool(r['owned']) if r['owned'] is not None else False,
            }
            for r in conn.execute(text('SELECT * FROM apps')).mappings()
        ]

        # Resolve the app_files join-table to human-readable (filename, app_id, app_version)
        # so the fixture isn't tied to auto-increment IDs.
        app_files_rows = conn.execute(text('''
            SELECT f.filename, a.app_id, a.app_version
            FROM app_files af
            JOIN files f ON f.id = af.file_id
            JOIN apps a  ON a.id = af.app_id
        ''')).mappings()
        app_files = [
            {'filename': r['filename'], 'app_id': r['app_id'], 'app_version': r['app_version']}
            for r in app_files_rows
        ]

    return {
        'files':     sorted(files,     key=lambda x: x['filename']),
        'titles':    sorted(titles,    key=lambda x: x['title_id']),
        'apps':      sorted(apps,      key=lambda x: (x['app_id'], x['app_version'])),
        'app_files': sorted(app_files, key=lambda x: (x['filename'], x['app_id'])),
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_titles_globals():
    """Reset titles module globals between tests to prevent cross-test pollution."""
    import titles as t
    t._titles_db_loaded = False
    t._cnmts_db = None
    t._titles_db = None
    t._versions_db = None
    t._versions_txt_db = None
    t.identification_in_progress_count = 0
    yield
    t._titles_db_loaded = False
    t._cnmts_db = None
    t._titles_db = None
    t._versions_db = None
    t._versions_txt_db = None
    t.identification_in_progress_count = 0


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

def test_db_content_after_scan_and_identify(tmp_path, monkeypatch, request):
    """
    Full pipeline against fixture game files must produce the committed DB content.

    Pipeline steps (driven directly, no scheduler or debounce):
      init_db → add_library → scan_library_path →
      load_titledb → process_library_identification →
      add_missing_apps_to_db → remove_missing_files_from_db → update_titles
    """
    import db as db_module
    import titles as titles_lib
    import settings as settings_module
    from app import create_app, init_db
    from library import scan_library_path, add_missing_apps_to_db, process_library_identification, remove_missing_files_from_db, update_titles
    from db import add_library

    # ------------------------------------------------------------------
    # Patch module-level constants before any db/settings I/O occurs
    # ------------------------------------------------------------------
    config_dir = tmp_path / 'config'
    config_dir.mkdir()

    db_path = str(config_dir / 'ownfoil.db')
    db_uri = f'sqlite:///{db_path}'

    # init_db checks os.path.exists(DB_FILE) — redirect to temp path
    monkeypatch.setattr(db_module, 'DB_FILE', db_path)

    # load_settings() reads/writes CONFIG_FILE — redirect to temp dir so
    # the real app/config/settings.yaml is never touched
    monkeypatch.setattr(settings_module, 'CONFIG_FILE', str(config_dir / 'settings.yaml'))

    # load_keys() has `key_file=KEYS_FILE` as a default parameter captured at
    # import time, so patching the module attribute after import has no effect.
    # Stub the whole function so the real dev keys are never loaded, keeping
    # Keys.keys_loaded falsy and forcing filename-based identification.
    monkeypatch.setattr(settings_module, 'load_keys', lambda key_file=None: (None, [], []))

    # Ensure Keys.keys_loaded is falsy at test start in case a previous test
    # or the module-level import already loaded real keys.
    from nsz.nut import Keys as NSZKeys
    monkeypatch.setattr(NSZKeys, 'keys_loaded', None)

    # load_titledb() reads from TITLEDB_DIR — point to minimal fixture data
    monkeypatch.setattr(titles_lib, 'TITLEDB_DIR', str(TITLEDB_FIXTURE))

    # ------------------------------------------------------------------
    # Bootstrap the Flask app and empty DB
    # ------------------------------------------------------------------
    flask_app = create_app(db_uri=db_uri)
    init_db(flask_app)

    # ------------------------------------------------------------------
    # Scan: add library entry and discover game files
    # ------------------------------------------------------------------
    games_path = str(GAMES_FIXTURE)
    with flask_app.app_context():
        add_library(games_path)
        scan_library_path(games_path)

    # ------------------------------------------------------------------
    # Identify: load titledb then run the full identification pipeline
    # ------------------------------------------------------------------
    titles_lib.load_titledb()

    # process_library_identification manages its own app context internally
    process_library_identification(flask_app)

    with flask_app.app_context():
        add_missing_apps_to_db()
        remove_missing_files_from_db()
        update_titles()

    # ------------------------------------------------------------------
    # Extract stable DB content and compare/update fixture
    # ------------------------------------------------------------------
    content = extract_db_content(db_uri)

    update = request.config.getoption('--update-content')

    if update or not CONTENT_FIXTURE.exists():
        CONTENT_FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        CONTENT_FIXTURE.write_text(json.dumps(content, indent=2) + '\n')
        action = 'updated' if update else 'created'
        pytest.skip(f'Content fixture {action} — commit {CONTENT_FIXTURE.relative_to(Path.cwd())}')

    expected = json.loads(CONTENT_FIXTURE.read_text())
    assert content == expected, (
        'DB content after scan+identify diverged from the develop baseline.\n'
        'If this is intentional, run `pytest --update-content` from develop and commit the fixture.'
    )
