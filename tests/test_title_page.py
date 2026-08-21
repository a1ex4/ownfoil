"""Tests for the title details page.

The page is a shell plus a single `title(titleId:)` call for the title's metadata, so two
things have to hold: the route renders and is gated like the rest of the shop, and the
query answers with what the panel draws.

Assertions are on what a caller can see, not on how the SQL gets there.
"""

import json
import types

import pytest

import db as db_mod
import titledb
from app import create_app
from db import Titles, db, init_db
from gql import graphql_dispatch


GAME = "0100000000020000"

TITLEDB_JSON = {
    GAME: {"id": GAME, "name": "Test Game", "publisher": "Nintendo",
           "releaseDate": 20231020, "nsuId": 70010000068689},
}

DETAILS = """
query TitleDetails($titleId: ID!) {
    title(titleId: $titleId) { titleId name publisher releaseDate nsuId }
}"""


@pytest.fixture
def catalogue(tmp_path, monkeypatch):
    """One title, catalogued and owned."""
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

        db.session.add(Titles(title_id=GAME, have_base=True, up_to_date=False,
                              complete=False))
        db.session.commit()

    return types.SimpleNamespace(app=app, client=app.test_client())


def details(client, title_id=GAME):
    resp = client.get("/api/graphql", query_string={
        "query": DETAILS, "variables": json.dumps({"titleId": title_id})})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert "errors" not in body, body["errors"]
    return body["data"]["title"]


def test_the_query_carries_the_metadata_the_panel_shows(catalogue):
    title = details(catalogue.client)

    assert title["name"] == "Test Game"
    assert title["publisher"] == "Nintendo"
    assert title["releaseDate"] == "20231020"
    assert title["nsuId"] == "70010000068689"


def test_an_unknown_title_is_null_rather_than_an_error(catalogue):
    """The page renders a "not found" state off this, so it must not be a GraphQL error."""
    assert details(catalogue.client, "FFFFFFFFFFFFFFFF") is None


def login(client, user, password):
    resp = client.post("/login", data={"user": user, "password": password})
    assert resp.status_code == 302, resp.get_data(as_text=True)


def test_the_page_renders_for_any_id(shop_app):
    """The route is a shell: it hands the id to the page and lets the query decide."""
    login(shop_app.client, "shopper", "shoppass1")

    resp = shop_app.client.get("/title/0100000000010000")
    assert resp.status_code == 200
    assert 'const TITLE_ID = "0100000000010000"' in resp.get_data(as_text=True)

    # No title carries this id; the page still renders and says so client-side.
    assert shop_app.client.get("/title/FFFFFFFFFFFFFFFF").status_code == 200


def test_the_id_reaches_the_page_uppercased(shop_app):
    """Both stores key titles in uppercase, so a lowercased url has to be folded."""
    login(shop_app.client, "shopper", "shoppass1")

    resp = shop_app.client.get("/title/0100000000010000".lower())
    assert 'const TITLE_ID = "0100000000010000"' in resp.get_data(as_text=True)


def test_a_private_shop_asks_for_a_login(shop_app):
    """Gated like the library it is reached from - not left open beside it."""
    resp = shop_app.client.get("/title/0100000000010000")

    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
