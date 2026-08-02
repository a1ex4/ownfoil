"""Tests for the state of the DB on a virgin install, before the first titledb import.

titles.db is built by the `update_titledb` task, which is only enqueued once the server is
already accepting requests — while the Web UI queries GraphQL on its very first page load.
Those resolvers join `titledb.titles` through the ATTACHed schema, so initialization has to
leave that schema queryable-but-empty rather than absent; otherwise every title query fails
with "no such table: titledb.titles" until the download completes.

Assertions are on the GraphQL responses (what the UI receives), not on how the schema is
made available, and the queries mirror those the frontend issues on load.
"""

import json
import types

import pytest

import db as db_mod
import titledb_store
from app import create_app
from db import db, init_db, Titles
from gql import graphql_dispatch


TITLE_ID = "0100000000010000"

# The three title-facing queries the UI issues; all of them reach titledb.
QUERIES = {
    "owned": """query { titles(owned: true, page: 1, pageSize: 10)
                 { total items { titleId name } } }""",
    "not_owned": """query { titles(owned: false, page: 1, pageSize: 10)
                     { total items { titleId name } } }""",
    "single": """query { title(titleId: "%s") { titleId name } }""" % TITLE_ID,
}


def _titledb_json(tmp_dir, name):
    """Minimal titledb JSON files, enough for a real import to build titles.db."""
    (tmp_dir / "titles.US.en.json").write_text(json.dumps({
        TITLE_ID: {"id": TITLE_ID, "name": name, "category": ["Action"]},
    }))
    (tmp_dir / "cnmts.json").write_text("{}")
    (tmp_dir / "versions.json").write_text("{}")


@pytest.fixture
def first_run(tmp_path, monkeypatch):
    """A never-started install: empty config dir, no titles.db, one owned title."""
    config = tmp_path / "config"
    config.mkdir()
    titledb_dir = tmp_path / "titledb"
    titledb_dir.mkdir()
    monkeypatch.setattr(db_mod, "DB_FILE", str(config / "ownfoil.db"))
    monkeypatch.setattr(db_mod, "TITLES_DB_FILE", str(config / "titles.db"))
    monkeypatch.setattr(titledb_store, "TITLES_DB_FILE", str(config / "titles.db"))
    monkeypatch.setattr(titledb_store, "TITLEDB_DIR", str(titledb_dir))
    monkeypatch.setattr(titledb_store, "CUSTOM_TITLES_FILE", str(config / "custom_titles.json"))

    app = create_app(f"sqlite:///{config / 'ownfoil.db'}")
    app.add_url_rule("/api/graphql", view_func=graphql_dispatch, methods=["GET", "POST"])

    init_db(app)  # what both entrypoints run before the server accepts requests

    # Seeded in its own context so requests get theirs, as they do under the real server.
    with app.app_context():
        db.session.add(Titles(title_id=TITLE_ID, have_base=True))
        db.session.commit()
    yield types.SimpleNamespace(app=app, client=app.test_client(), titledb_dir=titledb_dir)


def _query(client, query):
    resp = client.get("/api/graphql", query_string={"query": query})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert "errors" not in body, body["errors"]
    return body["data"]


@pytest.mark.parametrize("name", sorted(QUERIES))
def test_title_queries_answer_before_the_first_import(first_run, name):
    """No titledb data yet is not an error: queries answer, owned titles still surface."""
    data = _query(first_run.client, QUERIES[name])

    if name == "owned":
        assert data["titles"]["total"] == 1
        assert data["titles"]["items"][0]["titleId"] == TITLE_ID
        assert data["titles"]["items"][0]["name"] is None  # unrecognized until imported
    elif name == "not_owned":
        assert data["titles"]["total"] == 0
    else:
        assert data["title"]["titleId"] == TITLE_ID


def test_first_import_supersedes_the_empty_db(first_run):
    """The empty DB must not pin the ATTACH: the first real import has to take over.

    Querying first is what makes this bite — it leaves titles.db ATTACHed on the pooled
    connection that the import then replaces underneath.
    """
    _query(first_run.client, QUERIES["owned"])

    _titledb_json(first_run.titledb_dir, "Some Game")
    titledb_store.import_from_json({"titles": {"region": "US", "language": "en"}})

    data = _query(first_run.client, QUERIES["owned"])
    assert data["titles"]["items"][0]["name"] == "Some Game"
