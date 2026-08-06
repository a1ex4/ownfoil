"""Tests for the titles.db lifecycle and for how it merges its metadata sources.

titles.db is the one database the app replaces on disk while it is ATTACHed to live
connections, and the one it is allowed to throw away: it is fully derivable from the
downloaded JSON plus the durable overrides in ownfoil.db. So it is versioned by a schema
fingerprint rather than a migration chain, it must never be left in WAL mode (the read path
opens it `mode=ro`, which WAL forbids without an -shm), and it must self-heal rather than
fail startup.

The merge tests assert what a caller reads back, not how the rows are stored: a source only
sets the fields it knows about, and the rest has to fall through to the next source down.
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
from titledb.schema import SOURCE_CUSTOM, SOURCE_EXTRACT, SOURCE_TITLEDB


TABLES = ['titles', 'title_overrides', 'cnmts', 'versions', 'meta']
TITLE_ID = "0100000000010000"

# What the downloaded JSON provides: the rich eShop record.
TITLEDB_RECORD = {
    "id": TITLE_ID,
    "name": "Some Game",
    "publisher": "Nintendo",
    "bannerUrl": "https://img/banner.jpg",
    "category": ["Action"],
}


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
    monkeypatch.setattr(titledb.store, "DB_FILE", str(config / "ownfoil.db"))
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


def _import(install, titles=None, cnmts=None):
    """Run a real import of minimal titledb JSON, as the update_titledb task would."""
    region_file = install.titledb_dir / "titles.US.en.json"
    region_file.write_text(json.dumps(titles or {TITLE_ID: TITLEDB_RECORD}))
    (install.titledb_dir / "cnmts.json").write_text(json.dumps(cnmts or {}))
    (install.titledb_dir / "versions.json").write_text("{}")
    with install.app.app_context():
        titledb.store.import_from_json(str(region_file), "US.en")


def _sources_of(install, title_id):
    with contextlib.closing(sqlite3.connect(install.titles_db)) as conn:
        row = conn.execute('SELECT sources FROM titles WHERE id = ?', (title_id,)).fetchone()
        return row[0] if row else None


def _set_overrides(install, overrides, title_id=TITLE_ID):
    with install.app.app_context():
        for source, record in overrides.items():
            ok, err = titledb.store.set_override(title_id, dict(record, id=title_id), source)
            assert ok, err


# ------------- lifecycle -------------

def test_created_at_init_and_fingerprinted(install):
    init_db(install.app)

    assert os.path.isfile(install.titles_db)
    assert _meta(install.titles_db, 'schema_version') == titledb.schema.fingerprint()


def test_stale_fingerprint_recreates_the_db(install):
    """A schema change is a rebuild: there is no chain to migrate, the data is derivable."""
    titledb.store.create_titledb(install.titles_db)
    with contextlib.closing(sqlite3.connect(install.titles_db)) as conn:
        conn.execute('INSERT INTO titles ("id", source) VALUES (?, ?)', ('0100', SOURCE_TITLEDB))
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


def test_rebuilt_db_keeps_the_schema_and_fingerprint(install):
    """import_from_json replaces the file wholesale; the replacement must match."""
    init_db(install.app)
    before = _schema_of(install.titles_db)

    _import(install)

    assert _schema_of(install.titles_db) == before
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


# ------------- source merging -------------

# (case, overrides to set, expected fields of the merged record, expected `sources`)
MERGE_CASES = [
    (
        "custom sets one field only",
        {SOURCE_CUSTOM: {"name": "My Name"}},
        {"name": "My Name", "publisher": "Nintendo", "bannerUrl": "https://img/banner.jpg"},
        "custom,titledb",
    ),
    (
        "extract wins over titledb",
        {SOURCE_EXTRACT: {"name": "Extracted", "publisher": "Extracted Inc"}},
        {"name": "Extracted", "publisher": "Extracted Inc", "bannerUrl": "https://img/banner.jpg"},
        "extract,titledb",
    ),
    (
        "custom wins over extract, field by field",
        {SOURCE_EXTRACT: {"name": "Extracted", "publisher": "Extracted Inc"},
         SOURCE_CUSTOM: {"name": "My Name"}},
        {"name": "My Name", "publisher": "Extracted Inc", "bannerUrl": "https://img/banner.jpg"},
        "custom,extract,titledb",
    ),
    (
        "titledb alone",
        {},
        {"name": "Some Game", "publisher": "Nintendo", "bannerUrl": "https://img/banner.jpg"},
        "titledb",
    ),
]
MERGE_IDS = [c[0] for c in MERGE_CASES]


@pytest.mark.parametrize("case,overrides,expected,sources", MERGE_CASES, ids=MERGE_IDS)
def test_sources_merge_field_by_field(install, case, overrides, expected, sources):
    """A source that sets some fields must not blank out the ones only a lower one has."""
    init_db(install.app)
    _import(install)

    _set_overrides(install, overrides)

    record = titledb.store.get_title_record(TITLE_ID)
    assert {k: record[k] for k in expected} == expected
    assert record["category"] == ["Action"]  # json columns survive the merge
    assert _sources_of(install, TITLE_ID) == sources


@pytest.mark.parametrize("case,overrides,expected,sources", MERGE_CASES, ids=MERGE_IDS)
def test_overrides_survive_a_rebuild(install, case, overrides, expected, sources):
    """The overrides are durable in ownfoil.db; the rebuild has to project them back in."""
    init_db(install.app)
    _import(install)
    _set_overrides(install, overrides)

    _import(install)

    record = titledb.store.get_title_record(TITLE_ID)
    assert {k: record[k] for k in expected} == expected
    assert _sources_of(install, TITLE_ID) == sources


def test_override_only_title_is_queryable(install):
    """A custom entry for an id titledb has never heard of still has to surface."""
    init_db(install.app)
    _import(install)
    unknown = "0100000000099999"

    _set_overrides(install, {SOURCE_CUSTOM: {"name": "Homebrew"}}, title_id=unknown)

    record = titledb.store.get_title_record(unknown)
    assert record["name"] == "Homebrew"
    assert record["publisher"] is None
    assert _sources_of(install, unknown) == "custom"


def test_deleting_an_override_restores_the_titledb_values(install):
    """Removing an override must not leave its values behind, nor drop the title."""
    init_db(install.app)
    _import(install)
    _set_overrides(install, {SOURCE_CUSTOM: {"name": "My Name"}})

    with install.app.app_context():
        ok, err = titledb.store.delete_override(TITLE_ID)
    assert ok, err

    record = titledb.store.get_title_record(TITLE_ID)
    assert record["name"] == "Some Game"
    assert record["publisher"] == "Nintendo"
    assert _sources_of(install, TITLE_ID) == "titledb"


def test_editing_an_override_keeps_the_titledb_baseline(install):
    """The snapshot is taken once: re-snapshotting a merged row would enshrine it."""
    init_db(install.app)
    _import(install)
    _set_overrides(install, {SOURCE_CUSTOM: {"name": "First"}})

    _set_overrides(install, {SOURCE_CUSTOM: {"name": "Second"}})
    with install.app.app_context():
        titledb.store.delete_override(TITLE_ID)

    assert titledb.store.get_title_record(TITLE_ID)["name"] == "Some Game"


def test_deleting_the_override_of_an_unknown_title_drops_it(install):
    """Nothing to fall back on: the row goes away instead of lingering half-empty."""
    init_db(install.app)
    _import(install)
    unknown = "0100000000099999"
    _set_overrides(install, {SOURCE_CUSTOM: {"name": "Homebrew"}}, title_id=unknown)

    with install.app.app_context():
        titledb.store.delete_override(unknown)

    assert titledb.store.get_title_record(unknown) is None


def test_deleting_one_source_leaves_the_others(install):
    init_db(install.app)
    _import(install)
    _set_overrides(install, {SOURCE_EXTRACT: {"name": "Extracted", "publisher": "Extracted Inc"},
                             SOURCE_CUSTOM: {"name": "My Name"}})

    with install.app.app_context():
        titledb.store.delete_override(TITLE_ID, SOURCE_CUSTOM)

    record = titledb.store.get_title_record(TITLE_ID)
    assert record["name"] == "Extracted"
    assert record["publisher"] == "Extracted Inc"
    assert _sources_of(install, TITLE_ID) == "extract,titledb"
