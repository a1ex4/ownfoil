"""Tests for the titles.db lifecycle: created at init, fingerprinted, rebuilt on a change.

titles.db is the one database the app replaces on disk while it is ATTACHed to live
connections, and the one it is allowed to throw away: it is fully derivable from the
downloaded JSON. So it is versioned by a schema fingerprint rather than a migration chain,
it must never be left in WAL mode (the read path opens it `mode=ro`, which WAL forbids
without an -shm), and it must self-heal rather than fail startup.
"""

import contextlib
import json
import os
import sqlite3
import types

import pytest
import sqlalchemy as sa

import db as db_mod
import titledb
import titles as titles_lib
from app import create_app
from constants import APP_TYPE_BASE, APP_TYPE_DLC, APP_TYPE_UPD
from db import init_db


TABLES = ['titles', 'cnmts', 'versions', 'meta']
TITLE_ID = "0100000000010000"


@pytest.fixture
def install(tmp_path, monkeypatch):
    """An install whose config dir is empty: no ownfoil.db, no titles.db."""
    config = tmp_path / "config"
    config.mkdir()
    titledb_dir = tmp_path / "titledb"
    titledb_dir.mkdir()
    monkeypatch.setattr(db_mod, "DB_FILE", str(config / "ownfoil.db"))
    monkeypatch.setattr(db_mod, "TITLES_DB_FILE", str(config / "titles.db"))
    monkeypatch.setattr(titledb.store, "TITLES_DB_FILE", str(config / "titles.db"))
    monkeypatch.setattr(titledb.store, "CUSTOM_TITLES_FILE", str(config / "custom_titles.json"))
    return types.SimpleNamespace(
        app=create_app(f"sqlite:///{config / 'ownfoil.db'}"),
        titledb_dir=titledb_dir,
        titles_db=str(config / "titles.db"),
    )


def _meta(path, key):
    with contextlib.closing(sqlite3.connect(path)) as conn:
        row = conn.execute('SELECT value FROM meta WHERE key = ?', (key,)).fetchone()
        return row[0] if row else None


def _schema_of(path):
    """{table: (columns, indexes)} as SQLite itself reports them."""
    out = {}
    with contextlib.closing(sqlite3.connect(path)) as conn:
        for table in TABLES:
            cols = [tuple(r[1:]) for r in conn.execute(f'PRAGMA table_info({table})')]
            indexes = {}
            for _seq, name, *_rest in conn.execute(f'PRAGMA index_list({table})'):
                if name.startswith('sqlite_autoindex'):
                    continue
                indexes[name] = [r[2] for r in conn.execute(f'PRAGMA index_info({name})')]
            out[table] = (cols, indexes)
    return out


def test_created_at_init_and_fingerprinted(install):
    init_db(install.app)

    assert os.path.isfile(install.titles_db)
    assert _meta(install.titles_db, 'schema_version') == titledb.schema.fingerprint()


def test_stale_fingerprint_recreates_the_db(install):
    """A schema change is a rebuild: there is no chain to migrate, the data is derivable."""
    titledb.store.create_titledb(install.titles_db)
    with contextlib.closing(sqlite3.connect(install.titles_db)) as conn:
        conn.execute('INSERT INTO titles ("id", source) VALUES (?, ?)', ('0100', 'upstream'))
        conn.execute("UPDATE meta SET value = 'stale' WHERE key = 'schema_version'")
        conn.commit()

    init_db(install.app)

    assert _meta(install.titles_db, 'schema_version') == titledb.schema.fingerprint()
    with contextlib.closing(sqlite3.connect(install.titles_db)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM titles').fetchone()[0] == 0
        # Locale gone, so the next update_titledb rebuilds from the JSON files.
        assert conn.execute(
            "SELECT COUNT(*) FROM meta WHERE key = 'imported_locale'").fetchone()[0] == 0


def test_unusable_db_is_recreated(install):
    """A file SQLite can't even open must not take the whole app down with it."""
    with open(install.titles_db, 'wb') as f:
        f.write(b'this is not a database')

    init_db(install.app)

    assert _meta(install.titles_db, 'schema_version') == titledb.schema.fingerprint()
    assert titledb.store._connect_ro() is not None


def test_titles_db_is_never_left_in_wal_mode(install):
    """WAL is unreadable through the `mode=ro` connections the whole read path uses."""
    init_db(install.app)

    with contextlib.closing(sqlite3.connect(install.titles_db)) as conn:
        assert conn.execute('PRAGMA journal_mode').fetchone()[0] != 'wal'
    assert not os.path.exists(install.titles_db + '-wal')
    assert not os.path.exists(install.titles_db + '-shm')
    assert titledb.store._connect_ro() is not None


def test_main_db_keeps_wal(install):
    """The pragma gate must not cost ownfoil.db its WAL mode."""
    init_db(install.app)

    with install.app.app_context():
        mode = db_mod.db.session.execute(sa.text('PRAGMA main.journal_mode')).scalar()
    assert mode == 'wal'


def _import(install, cnmts=None):
    """Run a real import of minimal titledb JSON, as the update_titledb task would."""
    (install.titledb_dir / "titles.US.en.json").write_text(json.dumps({
        TITLE_ID: {"id": TITLE_ID, "name": "Some Game"},
    }))
    (install.titledb_dir / "cnmts.json").write_text(json.dumps(cnmts or {}))
    (install.titledb_dir / "versions.json").write_text("{}")
    with install.app.app_context():
        titledb.store.import_from_json(str(install.titledb_dir / "titles.US.en.json"), "US.en")


def test_rebuilt_db_keeps_the_fingerprint(install):
    """import_from_json replaces the file wholesale; the replacement must be versioned."""
    init_db(install.app)

    _import(install)

    assert _meta(install.titles_db, 'schema_version') == titledb.schema.fingerprint()
    assert _meta(install.titles_db, 'imported_locale') == "US.en"
    assert not os.path.exists(install.titles_db + '-wal')


# (titleType, the app id carrying it, what identify_appId must return for it)
CNMT_CASES = [
    (128, TITLE_ID,             (TITLE_ID, APP_TYPE_BASE)),
    (129, "0100000000010800",   (TITLE_ID, APP_TYPE_UPD)),
    (130, "0100000000011001",   (TITLE_ID, APP_TYPE_DLC)),
]


@pytest.mark.parametrize("title_type,app_id,expected", CNMT_CASES)
def test_numeric_json_fields_survive_the_round_trip(install, title_type, app_id, expected):
    """titleType has to come back as the number identify_appId compares against.

    A column typed TEXT stores the JSON's 129 as '129', which equals none of the
    APP_TYPE constants, and every app silently falls back to filename identification.
    """
    init_db(install.app)
    _import(install, cnmts={app_id.lower(): {"0": {
        "titleId": TITLE_ID, "titleType": title_type, "otherApplicationId": TITLE_ID,
    }}})

    assert titledb.store.get_cnmt_latest(app_id)["titleType"] == title_type
    assert titles_lib.identify_appId(app_id) == expected
