"""Tests that the graph is navigable from every entry point, not just from `apps`.

A GraphQL schema promises that a field means the same thing wherever you reach it.
These tests hold the schema to that: `versions` and `title` are hydrated by the top
level `apps` query, and a title-detail page (title -> apps -> versions) or a file
list (files -> apps -> title) has to get the same data rather than silent nulls.

Assertions are on what a client can read, not on which hydrator produced it.
"""

import json
import types

import pytest

import db as db_mod
import titledb
from app import create_app
from constants import APP_TYPE_BASE, APP_TYPE_DLC, APP_TYPE_UPD
from db import Apps, Files, Libraries, Titles, db, init_db
from gql import graphql_dispatch


ALPHA = "0100000000AAAAA0"[:16]
ALPHA_UPD = ALPHA[:-3] + "800"
ALPHA_DLC = ALPHA[:-4] + "1001"

# The update app deliberately has no titledb row of its own - that is the normal case
# for updates, and it is what makes `App.title` the only way to name the game.
TITLEDB_JSON = {
    ALPHA:     {"id": ALPHA, "name": "Alpha Game", "publisher": "Nintendo"},
    ALPHA_DLC: {"id": ALPHA_DLC, "name": "Alpha Extra Levels"},
}

# (app_id, type, version, owned, filename or None)
APPS = [
    (ALPHA,     APP_TYPE_BASE, "0",      True,  "Alpha.nsp"),
    (ALPHA_UPD, APP_TYPE_UPD,  "65536",  True,  "Alpha[v65536].nsp"),
    (ALPHA_UPD, APP_TYPE_UPD,  "131072", False, None),
    (ALPHA_DLC, APP_TYPE_DLC,  "0",      True,  "AlphaDLC.nsp"),
    (ALPHA_DLC, APP_TYPE_DLC,  "65536",  False, None),
]


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

        title = Titles(title_id=ALPHA, have_base=True, up_to_date=False, complete=True)
        db.session.add(title)
        library_row = Libraries(path=str(tmp_path / "games"))
        db.session.add_all([title, library_row])
        db.session.flush()

        for app_id, app_type, version, owned, filename in APPS:
            app_row = Apps(title_id=title.id, app_id=app_id, app_version=version,
                           app_type=app_type, owned=owned)
            db.session.add(app_row)
            if filename:
                file_row = Files(library_id=library_row.id,
                                 filepath=str(tmp_path / "games" / filename),
                                 filename=filename, extension="nsp", size=1024,
                                 identified=True)
                db.session.add(file_row)
                app_row.files.append(file_row)
        db.session.commit()

    return types.SimpleNamespace(app=app, client=app.test_client())


def query(library, text_query, **variables):
    params = {"query": text_query}
    if variables:
        params["variables"] = json.dumps(variables)
    resp = library.client.get("/api/graphql", query_string=params)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert "errors" not in body, body["errors"]
    return body["data"]


# The version history behind a BASE app is its title's UPDATE apps, so this is the
# same answer whichever query reaches the app.
EXPECTED_BASE_VERSIONS = [(65536, True), (131072, False)]


def test_a_titles_apps_carry_their_versions(library):
    """The title-detail page's query. Previously always null on this path."""
    data = query(library, """
        query { title(titleId: "%s") { name apps(appType: ["BASE"]) {
            appId versions { version owned } } } }""" % ALPHA)

    base = data["title"]["apps"][0]
    assert [(v["version"], v["owned"]) for v in base["versions"]] == EXPECTED_BASE_VERSIONS


def test_the_titles_list_carries_versions_too(library):
    data = query(library, """
        query { titles(owned: true, page: 1, pageSize: 10) { items {
            titleId apps(appType: ["BASE"]) { versions { version owned } } } } }""")

    base = data["titles"]["items"][0]["apps"][0]
    assert [(v["version"], v["owned"]) for v in base["versions"]] == EXPECTED_BASE_VERSIONS


def test_reaching_an_app_by_title_or_by_apps_gives_the_same_versions(library):
    """The point of the fix: a field cannot mean two different things by path."""
    via_apps = query(library, """
        query { apps(groupByAppId: true, appType: ["BASE"], page: 1, pageSize: 10) {
            items { appId versions { version owned } } } }""")["apps"]["items"][0]
    via_title = query(library, """
        query { title(titleId: "%s") { apps(appType: ["BASE"]) {
            appId versions { version owned } } } }""" % ALPHA)["title"]["apps"][0]

    assert via_apps["versions"] == via_title["versions"]


def test_a_file_names_the_game_behind_an_update(library):
    """An UPDATE app has no titledb row of its own, so without `title` a file list
    cannot say which game the file belongs to."""
    data = query(library, """
        query { files(filter: {filename: {contains: "v65536"}}, page: 1, pageSize: 10) {
            items { filename apps { appId appType titledb { name } title { titleId name } } } } }""")

    app = data["files"]["items"][0]["apps"][0]
    assert app["appType"] == "UPDATE"
    assert app["titledb"] is None          # no metadata of its own, as expected
    assert app["title"]["name"] == "Alpha Game"
    assert app["title"]["titleId"] == ALPHA


def test_a_files_apps_carry_their_versions(library):
    data = query(library, """
        query { files(filter: {filename: {contains: "AlphaDLC"}}, page: 1, pageSize: 10) {
            items { apps { appId versions { version owned } } } } }""")

    dlc = data["files"]["items"][0]["apps"][0]
    assert [(v["version"], v["owned"]) for v in dlc["versions"]] == [(0, True), (65536, False)]


def test_unrequested_nested_fields_stay_null(library):
    """The hydrators are selection-gated: not asking must still cost nothing, and the
    back-link path must not start recursing."""
    data = query(library, """
        query { apps(appType: ["BASE"], page: 1, pageSize: 10) { items {
            files { filename apps { appId files { filename } } } } } }""")

    backlinked = data["apps"]["items"][0]["files"][0]["apps"][0]
    assert backlinked["files"] is None
