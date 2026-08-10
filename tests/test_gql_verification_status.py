"""The derived verification status: one label for the two verdict columns.

Nothing stores the status, so there are three separate spellings of the same rule -
STATUS_RULES in `containers.verification`, the SQL the filter builds from it, and the
in-memory matcher nested file lists use. These tests are what keeps them from drifting:
the table below is the specification, and every path is asserted against it rather than
against each other.
"""

import json
import types

import pytest

import db as db_mod
import titledb
from app import create_app
from constants import APP_TYPE_BASE
from containers.verification import STATUS_RULES, status_of
from db import Apps, Files, Libraries, Titles, db, init_db
from gql import graphql_dispatch
from gql.filters import VerificationStatus

ALPHA = "0100000000AAAAA0"
TITLEDB_JSON = {ALPHA: {"id": ALPHA, "name": "Alpha Game"}}

# (filename, signature_valid, hash_valid, status). Every combination of the two columns
# the verify task can write: the signature is always recorded, and the hash only when
# the depth asked for it, so a null signature with a non-null hash cannot occur.
CASES = [
    ("unverified.nsp",  None,  None,  "UNVERIFIED"),
    ("shallow-pass.nsp", True,  None,  "SIGNATURE_OK"),
    ("shallow-fail.nsp", False, None,  "SIGNATURE_FAILED"),
    ("pristine.nsp",    True,  True,  "VALID"),
    ("resigned.nsp",    False, True,  "REPACK"),
    ("rotted.nsp",      True,  False, "CORRUPT"),
    ("broken.nsp",      False, False, "CORRUPT"),
]

STATUSES = sorted({status for *_, status in CASES})


def expected_files(status):
    return sorted(name for name, _, _, s in CASES if s == status)


@pytest.fixture
def library(tmp_path, monkeypatch):
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
    with app.app_context():
        titledb.store.import_from_json(str(region_file), "US.en")

        title = Titles(title_id=ALPHA, have_base=True)
        library_row = Libraries(path=str(tmp_path / "games"))
        db.session.add_all([title, library_row])
        db.session.flush()

        app_row = Apps(title_id=title.id, app_id=ALPHA, app_version="0",
                       app_type=APP_TYPE_BASE, owned=True)
        db.session.add(app_row)
        for filename, signature_valid, hash_valid, _ in CASES:
            file_row = Files(library_id=library_row.id,
                             filepath=str(tmp_path / "games" / filename),
                             filename=filename, extension="nsp", size=1, identified=True,
                             signature_valid=signature_valid, hash_valid=hash_valid)
            db.session.add(file_row)
            app_row.files.append(file_row)
        db.session.commit()

    return types.SimpleNamespace(app=app, client=app.test_client())


def query(library, text_query):
    resp = library.client.get("/api/graphql", query_string={"query": text_query})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert "errors" not in body, body["errors"]
    return body["data"]


@pytest.mark.parametrize("signature_valid,hash_valid,expected",
                         [(s, h, status) for _, s, h, status in CASES],
                         ids=[name for name, *_ in CASES])
def test_the_table_labels_every_verdict_pair(signature_valid, hash_valid, expected):
    assert status_of(signature_valid, hash_valid) == expected


@pytest.mark.parametrize("signature_valid,hash_valid,expected",
                         [(s, h, status) for _, s, h, status in CASES],
                         ids=[name for name, *_ in CASES])
def test_raw_sql_integers_land_on_the_same_label(signature_valid, hash_valid, expected):
    """A row read through the ORM carries booleans, one read through raw SQL carries
    0/1 - and the files query reads through raw SQL."""
    as_int = [None if v is None else int(v) for v in (signature_valid, hash_valid)]
    assert status_of(*as_int) == expected


def test_every_status_the_schema_offers_is_covered():
    """Guards the guard: a new enum member with no case would make the tests below
    vacuous for it, and an unreachable one would be a lie in the docs pane."""
    assert {s.value for s in VerificationStatus} == set(STATUSES)
    assert {row[0] for row in STATUS_RULES} == set(STATUSES)


def test_the_field_reports_the_label(library):
    data = query(library, """
        query { files(page: 1, pageSize: 50) {
            items { filename verificationStatus } } }""")
    labelled = {f["filename"]: f["verificationStatus"] for f in data["files"]["items"]}
    assert labelled == {name: status for name, _, _, status in CASES}


@pytest.mark.parametrize("status", STATUSES)
def test_the_filter_selects_exactly_what_the_field_labels(library, status):
    """The SQL path against the table. UNVERIFIED is the case worth holding onto: it
    tests two NULLs, which a `col = 0/1` clause would silently drop."""
    data = query(library, """
        query { files(page: 1, pageSize: 50, filter: {verificationStatus: %s}) {
            items { filename } } }""" % status)
    assert sorted(f["filename"] for f in data["files"]["items"]) == expected_files(status)


@pytest.mark.parametrize("status", STATUSES)
def test_a_nested_file_list_filters_the_same_way(library, status):
    """`App.files` filters an already-hydrated list in Python rather than in SQL, so it
    is a third implementation of the rule and has to answer identically."""
    data = query(library, """
        query { apps(page: 1, pageSize: 10) {
            items { files(filter: {verificationStatus: %s}) { filename } } } }""" % status)
    names = [f["filename"] for a in data["apps"]["items"] for f in a["files"]]
    assert sorted(names) == expected_files(status)


def test_the_status_and_the_bare_booleans_agree(library):
    """CORRUPT spans both signature polarities, so it is not a rename of `hashValid`."""
    data = query(library, """
        query {
          corrupt: files(page: 1, pageSize: 50, filter: {verificationStatus: CORRUPT}) {
            items { filename } }
          hashFailed: files(page: 1, pageSize: 50, filter: {hashValid: false}) {
            items { filename } }
        }""")
    assert ([f["filename"] for f in data["corrupt"]["items"]]
            == [f["filename"] for f in data["hashFailed"]["items"]])
    assert len(data["corrupt"]["items"]) == 2
