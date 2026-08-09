"""Tests for reading titledb for titles the library has never seen.

`apps { versions }` answers "what do I have and what am I missing" - but only for
titles that already have a row in main.titles, because the apps table is only
populated for those. A catalogue or wishlist page asks the same question about a
title nobody owns, and that answer can only come from titledb.versions / titledb.cnmts.

These also pin the case handling: versions.title_id is normalised to uppercase at
import, while cnmts keeps the JSON's lowercase. Getting that wrong returns an empty
list rather than an error, so it needs a test that would notice.
"""

import json
import types

import pytest

import db as db_mod
import titledb
from app import create_app
from constants import APP_TYPE_BASE
from db import Apps, Titles, db, init_db
from gql import graphql_dispatch


OWNED = "0100000000AAAAA0"[:16]
UNOWNED = "0100000000BBBB00"[:16]
OWNED_DLC = OWNED[:-4] + "1001"
UNOWNED_DLC_1 = UNOWNED[:-4] + "1001"
UNOWNED_DLC_2 = UNOWNED[:-4] + "1002"

TITLEDB_JSON = {
    OWNED:         {"id": OWNED, "name": "Alpha Game", "releaseDate": 20170303},
    OWNED_DLC:     {"id": OWNED_DLC, "name": "Alpha Extra Levels"},
    UNOWNED:       {"id": UNOWNED, "name": "Beta Game", "releaseDate": 20180921},
    UNOWNED_DLC_1: {"id": UNOWNED_DLC_1, "name": "Beta Season Pass"},
    UNOWNED_DLC_2: {"id": UNOWNED_DLC_2, "name": "Beta Costume Pack"},
}

# versions.json is keyed by title id -> {version: release date}.
VERSIONS_JSON = {
    OWNED:   {"65536": "2017-05-01", "131072": "2017-09-01"},
    UNOWNED: {"65536": "2018-11-02"},
}

# cnmts.json is keyed by app id -> {cnmt version: record}. The ids arrive lowercase
# and are stored as-is, which is what makes the join asymmetric.
CNMTS_JSON = {
    OWNED_DLC.lower():     {"0": {"titleId": OWNED_DLC.lower(), "titleType": 130,
                                  "version": 0,
                                  "otherApplicationId": OWNED.lower()}},
    UNOWNED_DLC_1.lower(): {"0": {"titleId": UNOWNED_DLC_1.lower(), "titleType": 130,
                                  "version": 0,
                                  "otherApplicationId": UNOWNED.lower()}},
    UNOWNED_DLC_2.lower(): {"0": {"titleId": UNOWNED_DLC_2.lower(), "titleType": 130,
                                  "version": 65536,
                                  "otherApplicationId": UNOWNED.lower()}},
}


@pytest.fixture
def catalogue(tmp_path, monkeypatch):
    """Alpha is in the library; Beta exists only in titledb."""
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
    # import_from_json finds these two beside the region file.
    (titledb_dir / "cnmts.json").write_text(json.dumps(CNMTS_JSON))
    (titledb_dir / "versions.json").write_text(json.dumps(VERSIONS_JSON))

    with app.app_context():
        titledb.store.import_from_json(str(region_file), "US.en")
        title = Titles(title_id=OWNED, have_base=True, up_to_date=True, complete=True)
        db.session.add(title)
        db.session.flush()
        db.session.add(Apps(title_id=title.id, app_id=OWNED, app_version="0",
                            app_type=APP_TYPE_BASE, owned=True))
        db.session.commit()

    return types.SimpleNamespace(app=app, client=app.test_client())


def query(catalogue, text_query):
    resp = catalogue.client.get("/api/graphql", query_string={"query": text_query})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert "errors" not in body, body["errors"]
    return body["data"]


def test_an_unowned_title_still_reports_its_versions(catalogue):
    """The library has no apps row for Beta, so this can only come from titledb."""
    data = query(catalogue, """
        query { title(titleId: "%s") { name availableVersions { version releaseDate } } }
        """ % UNOWNED)

    title = data["title"]
    assert title["name"] == "Beta Game"
    # The launch build is implicit in the title's release date, not a versions row.
    assert title["availableVersions"] == [
        {"version": 0, "releaseDate": "2018-09-21"},
        {"version": 65536, "releaseDate": "2018-11-02"},
    ]


def test_an_unowned_title_still_reports_its_dlc(catalogue):
    data = query(catalogue, """
        query { title(titleId: "%s") { availableDlc { appId version titledb { name } } } }
        """ % UNOWNED)

    dlc = data["title"]["availableDlc"]
    assert {d["appId"] for d in dlc} == {UNOWNED_DLC_1, UNOWNED_DLC_2}
    assert {d["titledb"]["name"] for d in dlc} == {"Beta Season Pass", "Beta Costume Pack"}
    assert next(d for d in dlc if d["appId"] == UNOWNED_DLC_2)["version"] == 65536


def test_the_owned_title_reports_the_same_catalogue_data(catalogue):
    """The field means the same thing whether or not the title is in the library."""
    data = query(catalogue, """
        query { title(titleId: "%s") {
            availableVersions { version } availableDlc { appId } } }""" % OWNED)

    assert [v["version"] for v in data["title"]["availableVersions"]] == [0, 65536, 131072]
    assert [d["appId"] for d in data["title"]["availableDlc"]] == [OWNED_DLC]


def test_the_catalogue_listing_carries_it_too(catalogue):
    """One batched query for the whole page, not one per title."""
    data = query(catalogue, """
        query { titles(owned: false, page: 1, pageSize: 50) {
            items { titleId availableVersions { version } } } }""")

    beta = next(t for t in data["titles"]["items"] if t["titleId"] == UNOWNED)
    assert [v["version"] for v in beta["availableVersions"]] == [0, 65536]


def test_a_title_with_no_dlc_reports_an_empty_list_not_null(catalogue):
    """Null means "not loaded"; a title genuinely without DLC has to say so."""
    data = query(catalogue, """
        query { title(titleId: "%s") { availableDlc { appId } } }""" % UNOWNED_DLC_1)

    assert data["title"]["availableDlc"] == []


def test_not_asking_costs_nothing(catalogue):
    data = query(catalogue, 'query { title(titleId: "%s") { name } }' % UNOWNED)

    assert data["title"] == {"name": "Beta Game"}
