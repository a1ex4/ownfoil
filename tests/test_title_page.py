"""Tests for the title details page.

The page is a shell plus a single `title(titleId:)` call, so two things have to hold: the
route renders and is gated like the rest of the shop, and the query answers with what the
page draws - every app the catalogue knows for the title, owned or missing, each DLC under
its own name, and the files behind them for an admin and nobody else.

Assertions are on what a caller can see, not on how the SQL gets there.
"""

import json
import types

import pytest

import db as db_mod
import titledb
from app import create_app
from constants import APP_TYPE_BASE, APP_TYPE_DLC, APP_TYPE_UPD
from db import Apps, Titles, db, init_db
from gql import graphql_dispatch


GAME = "0100000000020000"
GAME_UPD = GAME[:-3] + "800"
DLC_OWNED, DLC_MISSING = GAME[:-4] + "1001", GAME[:-4] + "1002"

TITLEDB_JSON = {
    GAME:        {"id": GAME, "name": "Test Game", "publisher": "Nintendo",
                  "releaseDate": 20231020, "nsuId": 70010000068689},
    DLC_OWNED:   {"id": DLC_OWNED, "name": "Test Game Extra Levels"},
    DLC_MISSING: {"id": DLC_MISSING, "name": "Test Game Costume Pack"},
}

# (app_id, type, version, owned) - the base is owned, the update line is one version
# behind, one DLC is owned and the other is only in the catalogue. That mix is what the
# page has to render, and every unowned row here is a real `apps` row: the library
# expands the catalogue into them so "what am I missing" is answerable.
APPS = [
    (GAME,        APP_TYPE_BASE, "0",      True),
    (GAME_UPD,    APP_TYPE_UPD,  "65536",  True),
    (GAME_UPD,    APP_TYPE_UPD,  "131072", False),
    (DLC_OWNED,   APP_TYPE_DLC,  "0",      True),
    (DLC_MISSING, APP_TYPE_DLC,  "0",      False),
]

DETAILS = """
query TitleDetails($titleId: ID!) {
    title(titleId: $titleId) {
        titleId name publisher releaseDate nsuId
        apps {
            appId appType appVersion owned
            titledb { name }
            files { id filename size verificationStatus }
        }
    }
}"""


@pytest.fixture
def catalogue(tmp_path, monkeypatch):
    """One title with an owned base, a missing update and a missing DLC."""
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

        title = Titles(title_id=GAME, have_base=True, up_to_date=False, complete=False)
        db.session.add(title)
        db.session.flush()
        for app_id, app_type, version, owned in APPS:
            db.session.add(Apps(title_id=title.id, app_id=app_id, app_version=version,
                                app_type=app_type, owned=owned))
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


def test_every_known_app_comes_back_owned_or_missing(catalogue):
    """The content table is a "what am I missing" view, so unowned rows must survive."""
    apps = details(catalogue.client)["apps"]

    assert {(a["appId"], a["appVersion"]): a["owned"] for a in apps} == {
        (GAME, 0): True,
        (GAME_UPD, 65536): True,
        (GAME_UPD, 131072): False,
        (DLC_OWNED, 0): True,
        (DLC_MISSING, 0): False,
    }


def test_a_dlc_is_named_by_its_own_catalogue_entry(catalogue):
    """A DLC row reads "Extra Levels", not its 16 hex digits - including an unowned one."""
    names = {a["appId"]: (a["titledb"] or {}).get("name")
             for a in details(catalogue.client)["apps"]}

    assert names[DLC_OWNED] == "Test Game Extra Levels"
    assert names[DLC_MISSING] == "Test Game Costume Pack"
    # An update ships under its own app id, which titledb does not describe.
    assert names[GAME_UPD] is None


def test_an_unknown_title_is_null_rather_than_an_error(catalogue):
    """The page renders a "not found" state off this, so it must not be a GraphQL error."""
    assert details(catalogue.client, "FFFFFFFFFFFFFFFF") is None


def login(client, user, password):
    resp = client.post("/login", data={"user": user, "password": password})
    assert resp.status_code == 302, resp.get_data(as_text=True)


def test_files_reach_an_admin_and_nobody_else(shop_app):
    """File rows are filesystem layout: the expanders exist for an admin only."""
    login(shop_app.client, "admin", "adminpass1")
    owned = [a for a in details(shop_app.client, "0100000000010000")["apps"] if a["owned"]]
    assert owned and all(a["files"] for a in owned)

    shop_app.client.get("/logout")
    login(shop_app.client, "shopper", "shoppass1")
    apps = details(shop_app.client, "0100000000010000")["apps"]
    assert apps and all(a["files"] is None for a in apps)


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
