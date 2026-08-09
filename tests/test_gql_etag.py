"""Tests for the ETag revalidation the web UI's free-reload UX rests on.

A GraphQL response carries an ETag, and a repeat request with If-None-Match gets a
304. That is only safe while the ETag tracks *every* change the schema can surface -
including the ones that flip a flag on a row that already exists, which is exactly
what finishing a scan does. A stale 304 there means the grid keeps showing "missing
update" badges for content the user now owns.

Assertions are on the observable contract - 304 vs 200 - not on how the hash is built.
"""

import json
import types

import pytest

import db as db_mod
import titledb
from app import create_app
from constants import APP_TYPE_BASE, APP_TYPE_UPD
from db import Apps, Files, Libraries, Task, Titles, db, init_db
from gql import graphql_dispatch


ALPHA = "0100000000AAAAA0"[:16]
ALPHA_UPD = ALPHA[:-3] + "800"

TITLEDB_JSON = {ALPHA: {"id": ALPHA, "name": "Alpha Game", "publisher": "Nintendo"}}


@pytest.fixture
def library(tmp_path, monkeypatch):
    """One title, one file, one task - enough for every column the hash covers."""
    config = tmp_path / "config"
    config.mkdir()
    titledb_dir = tmp_path / "titledb"
    titledb_dir.mkdir()
    monkeypatch.setattr(db_mod, "DB_FILE", str(config / "ownfoil.db"))
    monkeypatch.setattr(db_mod, "TITLES_DB_FILE", str(config / "titles.db"))
    monkeypatch.setattr(titledb.store, "TITLES_DB_FILE", str(config / "titles.db"))
    monkeypatch.setattr(titledb.store, "DB_FILE", str(config / "ownfoil.db"))

    app = create_app(f"sqlite:///{config / 'ownfoil.db'}")
    app.add_url_rule("/api/graphql", view_func=graphql_dispatch, methods=["GET", "POST"])
    init_db(app)

    region_file = titledb_dir / "titles.US.en.json"
    region_file.write_text(json.dumps(TITLEDB_JSON))
    (titledb_dir / "cnmts.json").write_text("{}")
    (titledb_dir / "versions.json").write_text("{}")
    with app.app_context():
        titledb.store.import_from_json(str(region_file), "US.en")

        title = Titles(title_id=ALPHA, have_base=True, up_to_date=False, complete=False)
        db.session.add(title)
        db.session.flush()
        db.session.add(Apps(title_id=title.id, app_id=ALPHA, app_version="0",
                            app_type=APP_TYPE_BASE, owned=True))
        db.session.add(Apps(title_id=title.id, app_id=ALPHA_UPD, app_version="65536",
                            app_type=APP_TYPE_UPD, owned=False))
        library_row = Libraries(path=str(tmp_path / "games"))
        db.session.add(library_row)
        db.session.flush()
        db.session.add(Files(library_id=library_row.id,
                             filepath=str(tmp_path / "games" / "Alpha.nsp"),
                             filename="Alpha.nsp", extension="nsp", size=1024,
                             identified=True, organized=False, download_count=0))
        db.session.commit()

    return types.SimpleNamespace(app=app, client=app.test_client())


QUERY = """
query Grid {
    apps(groupByAppId: true, page: 1, pageSize: 50) {
        total
        items { appId owned title { ownership { haveBase upToDate complete } } }
    }
}"""


def fetch(library, etag=None):
    headers = {"If-None-Match": etag} if etag else {}
    return library.client.get("/api/graphql", query_string={"query": QUERY},
                              headers=headers)


def test_an_unchanged_library_revalidates_to_304(library):
    """The whole point: a reload that changed nothing costs no work."""
    first = fetch(library)
    assert first.status_code == 200
    assert first.headers["ETag"]

    second = fetch(library, first.headers["ETag"])

    assert second.status_code == 304


# (case, a callable applying the change, the column it stands for)
# Every one of these leaves the row counts untouched - that is what makes them the
# interesting cases, and what the previous count-only hash missed.
def _flip_title_flags(session):
    session.query(Titles).filter_by(title_id=ALPHA).update(
        {"up_to_date": True, "complete": True})


def _own_the_update(session):
    session.query(Apps).filter_by(app_id=ALPHA_UPD).update({"owned": True})


def _mark_file_organized(session):
    session.query(Files).update({"organized": True})


def _count_a_download(session):
    session.query(Files).update({"download_count": 1})


def _reidentify_file(session):
    session.query(Files).update({"identified": False, "identification_attempts": 3})


def _start_a_task(session):
    session.add(Task(task_name="scan_library", status="running", completion_pct=40,
                     input_hash="x"))


IN_PLACE_CHANGES = [
    ("title flags flipped by a scan", _flip_title_flags),
    ("an update becomes owned",       _own_the_update),
    ("a file is organized",           _mark_file_organized),
    ("a file is downloaded",          _count_a_download),
    ("a file is re-identified",       _reidentify_file),
    ("a task reports progress",       _start_a_task),
]


@pytest.mark.parametrize("case,apply_change",
                         IN_PLACE_CHANGES, ids=[c[0] for c in IN_PLACE_CHANGES])
def test_in_place_changes_invalidate_the_cache(library, case, apply_change):
    first = fetch(library)
    etag = first.headers["ETag"]

    with library.app.app_context():
        apply_change(db.session)
        db.session.commit()

    assert fetch(library, etag).status_code == 200


def test_the_new_etag_is_stable_once_the_change_has_landed(library):
    """Invalidation must settle: one change, one new ETag, then 304s again."""
    etag = fetch(library).headers["ETag"]
    with library.app.app_context():
        _flip_title_flags(db.session)
        db.session.commit()

    changed = fetch(library, etag)
    assert changed.status_code == 200

    assert fetch(library, changed.headers["ETag"]).status_code == 304


def test_a_304_still_carries_the_etag(library):
    """Without it the client has nothing to send on the next revalidation."""
    etag = fetch(library).headers["ETag"]

    not_modified = fetch(library, etag)

    assert not_modified.status_code == 304
    assert not_modified.headers["ETag"] == etag
