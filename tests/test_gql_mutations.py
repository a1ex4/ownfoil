"""Tests for the mutation root.

Writes differ from reads in three ways that matter to a client, and each is pinned
here: a refused write errors rather than quietly doing nothing, a write is never
served from cache and never accepted over GET, and every guard the REST endpoints
apply still applies.
"""

import json
import types

import pytest

import db as db_mod
import titledb
from app import create_app
from constants import APP_TYPE_BASE
from db import Apps, Files, Libraries, Titles, db, init_db
from gql import graphql_dispatch
from gql.schema import MAX_QUERY_DEPTH


ALPHA = "0100000000AAAAA0"[:16]
TITLEDB_JSON = {ALPHA: {"id": ALPHA, "name": "Alpha Game", "publisher": "Nintendo"}}


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
    (titledb_dir / "cnmts.json").write_text("{}")
    (titledb_dir / "versions.json").write_text("{}")
    with app.app_context():
        titledb.store.import_from_json(str(region_file), "US.en")
        title = Titles(title_id=ALPHA, have_base=True)
        library_row = Libraries(path=str(tmp_path / "games"))
        db.session.add_all([title, library_row])
        db.session.flush()
        db.session.add(Apps(title_id=title.id, app_id=ALPHA, app_version="0",
                            app_type=APP_TYPE_BASE, owned=True))
        db.session.add_all([
            Files(library_id=library_row.id, filename="Plain.nsp", extension="nsp",
                  filepath=str(tmp_path / "games" / "Plain.nsp"), size=10,
                  compressed=False),
            Files(library_id=library_row.id, filename="Small.nsz", extension="nsz",
                  filepath=str(tmp_path / "games" / "Small.nsz"), size=5,
                  compressed=True),
            Files(library_id=library_row.id, filename="Notes.txt", extension="txt",
                  filepath=str(tmp_path / "games" / "Notes.txt"), size=1,
                  compressed=False),
        ])
        db.session.commit()

    return types.SimpleNamespace(app=app, client=app.test_client())


def mutate(library, document, expect_error=False):
    resp = library.client.post("/api/graphql", json={"query": document})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    if expect_error:
        assert body.get("errors"), f"expected an error, got {body}"
        return body["errors"][0]["message"]
    assert "errors" not in body, body["errors"]
    return body["data"]


def file_id(library, filename):
    resp = library.client.get("/api/graphql", query_string={"query": """
        query { files(filter: {filename: {eq: "%s"}}) { items { id } } }""" % filename})
    return resp.get_json()["data"]["files"]["items"][0]["id"]


def test_a_write_enqueues_real_work(library):
    data = mutate(library, """
        mutation { scanLibrary { id taskName status } }""")

    assert data["scanLibrary"]["taskName"] == "scan_library"
    assert data["scanLibrary"]["status"] == "pending"


def test_the_enqueued_task_is_readable_through_the_query_side(library):
    """A mutation returns exactly what `task(id:)` would - one shape for a task."""
    created = mutate(library, "mutation { scanLibrary { id } }")["scanLibrary"]

    resp = library.client.get("/api/graphql", query_string={"query": """
        query { task(id: "%s") { taskName } }""" % created["id"]})

    assert resp.get_json()["data"]["task"]["taskName"] == "scan_library"


def test_enqueuing_a_duplicate_returns_the_existing_task(library):
    first = mutate(library, "mutation { scanLibrary { id } }")["scanLibrary"]
    second = mutate(library, "mutation { scanLibrary { id } }")["scanLibrary"]

    assert first["id"] == second["id"]


def test_an_unknown_task_name_is_refused(library):
    message = mutate(library, """
        mutation { enqueueTask(name: "not_a_task") { id } }""", expect_error=True)

    assert "not_a_task" in message


def test_a_task_can_be_cancelled(library):
    created = mutate(library, "mutation { scanLibrary { id } }")["scanLibrary"]

    assert mutate(library, """
        mutation { cancelTask(id: "%s") }""" % created["id"])["cancelTask"] is True
    # Already gone: cancelling again is False, not an error.
    assert mutate(library, """
        mutation { cancelTask(id: "%s") }""" % created["id"])["cancelTask"] is False


# (case, mutation template, filename, the reason it must be refused)
REFUSED_COMPRESSION = [
    ("already compressed",   "compressFile",   "Small.nsz", "already compressed"),
    ("not compressed",       "decompressFile", "Plain.nsp", "not compressed"),
    ("uncompressible type",  "compressFile",   "Notes.txt", "cannot be compressed"),
]


@pytest.mark.parametrize("case,field,filename,reason", REFUSED_COMPRESSION,
                         ids=[c[0] for c in REFUSED_COMPRESSION])
def test_compression_guards_match_the_rest_endpoints(library, case, field, filename, reason):
    message = mutate(library, """
        mutation { %s(fileId: "%s") { id } }""" % (field, file_id(library, filename)),
        expect_error=True)

    assert reason in message


def test_a_compressible_file_is_accepted(library):
    data = mutate(library, """
        mutation { compressFile(fileId: "%s") { taskName } }""" % file_id(library, "Plain.nsp"))

    assert data["compressFile"]["taskName"] == "compress_file"


def test_a_missing_file_is_refused(library):
    message = mutate(library, """
        mutation { compressFile(fileId: "99999") { id } }""", expect_error=True)

    assert "not found" in message.lower()


def test_a_title_override_changes_what_the_query_side_reads(library):
    """The write and the read have to agree immediately - the override is projected
    into titles.db, not just stored."""
    data = mutate(library, """
        mutation { setTitleOverride(titleId: "%s", record: "{\\"name\\": \\"Renamed\\"}")
            { titleId name source } }""" % ALPHA)

    assert data["setTitleOverride"]["name"] == "Renamed"
    assert data["setTitleOverride"]["source"] == "custom"

    resp = library.client.get("/api/graphql", query_string={"query": """
        query { title(titleId: "%s") { name } }""" % ALPHA})
    assert resp.get_json()["data"]["title"]["name"] == "Renamed"


def test_deleting_an_override_restores_the_downloaded_value(library):
    mutate(library, """
        mutation { setTitleOverride(titleId: "%s", record: "{\\"name\\": \\"Renamed\\"}")
            { name } }""" % ALPHA)

    assert mutate(library, """
        mutation { deleteTitleOverride(titleId: "%s") }""" % ALPHA)["deleteTitleOverride"]

    resp = library.client.get("/api/graphql", query_string={"query": """
        query { title(titleId: "%s") { name } }""" % ALPHA})
    assert resp.get_json()["data"]["title"]["name"] == "Alpha Game"


def test_malformed_json_is_refused_not_stored(library):
    message = mutate(library, """
        mutation { setTitleOverride(titleId: "%s", record: "not json") { name } }
        """ % ALPHA, expect_error=True)

    assert "valid JSON" in message


# ---- the cache and transport contract ----

def test_a_mutation_is_never_cached(library):
    resp = library.client.post("/api/graphql",
                               json={"query": "mutation { scanLibrary { id } }"})

    assert "ETag" not in resp.headers
    assert resp.headers["Cache-Control"] == "no-store"


def test_a_mutation_over_get_is_rejected(library):
    """A GET is cacheable, prefetchable and link-followable; it must not write."""
    resp = library.client.get("/api/graphql",
                              query_string={"query": "mutation { scanLibrary { id } }"})

    assert resp.status_code == 405


def test_a_query_named_like_a_mutation_still_caches(library):
    """The dispatcher parses the document rather than matching on the string."""
    resp = library.client.get("/api/graphql", query_string={
        "query": "query mutationStatus { titles { total } }"})

    assert resp.status_code == 200
    assert resp.headers["ETag"]


def test_a_write_invalidates_a_held_etag(library):
    """The read side must notice what the write side did."""
    grid = {"query": "query { tasks { id } }"}
    etag = library.client.get("/api/graphql", query_string=grid).headers["ETag"]

    mutate(library, "mutation { scanLibrary { id } }")

    assert library.client.get("/api/graphql", query_string=grid,
                              headers={"If-None-Match": etag}).status_code == 200


def _nested_query(levels):
    """`files { apps { files { apps ... } } }`, `levels` deep. The hydration chain
    stops recursing after the first hop, but the parser still has to walk it all."""
    inner = "id"
    for i in range(levels):
        inner = "%s { %s }" % ("files" if i % 2 else "apps", inner)
    return "query { apps { items { %s } } }" % inner


def test_a_query_within_the_depth_limit_is_served(library):
    """The cap must leave the real UI queries comfortably alone."""
    resp = library.client.get("/api/graphql", query_string={
        "query": _nested_query(MAX_QUERY_DEPTH - 5)})

    assert resp.get_json().get("errors") is None


def test_a_pathological_nesting_is_refused(library):
    """The endpoint is reachable by any shop user, so depth is capped."""
    resp = library.client.get("/api/graphql", query_string={
        "query": _nested_query(MAX_QUERY_DEPTH + 5)})

    assert resp.get_json().get("errors")
