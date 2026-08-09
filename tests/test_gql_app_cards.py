"""Tests for the apps query the main view's grid is built from.

The grid renders one card per app id and asks the server for exactly one page of them,
so three things have to hold: an app id's versions collapse into a single item (a DLC
with three versions is one card, not three), a page of N is N cards whatever the
filters, and the title-level notions the badges show - ownership, "up to date",
"complete" - come back with the card.

Assertions are on what a page contains, not on how the SQL gets there.
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


# Two titles: Alpha is behind on updates but has every DLC, Beta is current but
# incomplete. Alpha's first DLC is missing its newest version, the second is current.
ALPHA, BETA = "0100000000ALPHA0".replace("ALPHA", "AAAAA")[:16], "0100000000BBBB000"[:16]
ALPHA_DLC_1, ALPHA_DLC_2 = ALPHA[:-4] + "1001", ALPHA[:-4] + "1002"
ALPHA_UPD, BETA_UPD = ALPHA[:-3] + "800", BETA[:-3] + "800"

TITLEDB_JSON = {
    ALPHA:       {"id": ALPHA, "name": "Alpha Game", "publisher": "Nintendo"},
    ALPHA_DLC_1: {"id": ALPHA_DLC_1, "name": "Alpha Extra Levels"},
    ALPHA_DLC_2: {"id": ALPHA_DLC_2, "name": "Alpha Costume Pack"},
    BETA:        {"id": BETA, "name": "Beta Game", "publisher": "Sega"},
}

# (title_id, name, have_base, up_to_date, complete, [(app_id, type, version, owned), ...])
LIBRARY = [
    (ALPHA, True, False, True, [
        (ALPHA,       APP_TYPE_BASE, "0",      True),
        (ALPHA_UPD,   APP_TYPE_UPD,  "65536",  True),
        (ALPHA_UPD,   APP_TYPE_UPD,  "131072", False),   # behind: newest update missing
        (ALPHA_DLC_1, APP_TYPE_DLC,  "0",      True),
        (ALPHA_DLC_1, APP_TYPE_DLC,  "65536",  False),   # behind: newest version missing
        (ALPHA_DLC_2, APP_TYPE_DLC,  "0",      True),    # current
    ]),
    (BETA, True, True, False, [
        (BETA,      APP_TYPE_BASE, "0",     True),
        (BETA_UPD,  APP_TYPE_UPD,  "65536", True),
    ]),
]

# 2 BASE + 2 distinct DLC app ids, however many version rows they have
ALL_CARDS = 4


@pytest.fixture
def library(tmp_path, monkeypatch):
    """A small library, served through the real GraphQL view."""
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

        for title_id, have_base, up_to_date, complete, apps in LIBRARY:
            title = Titles(title_id=title_id, have_base=have_base,
                           up_to_date=up_to_date, complete=complete)
            db.session.add(title)
            db.session.flush()
            for app_id, app_type, version, owned in apps:
                db.session.add(Apps(title_id=title.id, app_id=app_id, app_version=version,
                                    app_type=app_type, owned=owned))
        db.session.commit()

    return types.SimpleNamespace(app=app, client=app.test_client())


CARDS = """
query Cards($page: Int!, $pageSize: Int!, $appType: [String!], $search: String,
            $owned: Boolean, $upToDate: Boolean, $complete: Boolean) {
    apps(groupByAppId: true, orderBy: {field: NAME}, page: $page, pageSize: $pageSize,
         appType: $appType, search: $search, owned: $owned,
         upToDate: $upToDate, complete: $complete) {
        total
        items {
            appId appType owned
            titledb { name }
            title { titleId name ownership { haveBase upToDate complete } }
            versions { version owned }
        }
    }
}"""


def cards(library, **variables):
    variables.setdefault("page", 1)
    variables.setdefault("pageSize", 50)
    variables.setdefault("appType", [APP_TYPE_BASE, APP_TYPE_DLC])
    resp = library.client.get("/api/graphql", query_string={
        "query": CARDS, "variables": json.dumps(variables)})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert "errors" not in body, body["errors"]
    return body["data"]["apps"]


def test_an_app_id_is_one_card_whatever_its_versions(library):
    """The DLC with two version rows has to be one card, or the page size lies."""
    page = cards(library)

    assert page["total"] == ALL_CARDS
    assert len(page["items"]) == ALL_CARDS
    assert [i["appId"] for i in page["items"]].count(ALPHA_DLC_1) == 1


def test_a_page_holds_exactly_page_size_cards(library):
    """Pages must tile the result: no overlap, nothing dropped."""
    seen = []
    for page_number in (1, 2):
        page = cards(library, page=page_number, pageSize=2)
        assert len(page["items"]) == 2
        assert page["total"] == ALL_CARDS
        seen += [i["appId"] for i in page["items"]]

    assert len(set(seen)) == ALL_CARDS


def test_cards_are_ordered_by_title_name_across_pages(library):
    names = [i["title"]["name"] for i in cards(library)["items"]]

    assert names == sorted(names, key=str.lower)


# (case, query variables, the app ids the page must contain)
FILTER_CASES = [
    ("type BASE",     {"appType": [APP_TYPE_BASE]},  {ALPHA, BETA}),
    ("type DLC",      {"appType": [APP_TYPE_DLC]},   {ALPHA_DLC_1, ALPHA_DLC_2}),
    # Alpha is behind (title flag) and so is its first DLC (newest version missing);
    # Beta and the second DLC are current.
    ("up to date",    {"upToDate": True},            {BETA, ALPHA_DLC_2}),
    ("outdated",      {"upToDate": False},           {ALPHA, ALPHA_DLC_1}),
    # Completion is a title-level notion, so it only ever matches BASE cards.
    ("complete",      {"complete": True},            {ALPHA}),
    ("missing dlc",   {"complete": False},           {BETA}),
    # A DLC matches its parent's name too, the way the old client-side search did.
    ("search name",   {"search": "alpha"},           {ALPHA, ALPHA_DLC_1, ALPHA_DLC_2}),
    ("search own name", {"search": "costume"},       {ALPHA_DLC_2}),
    ("search app id", {"search": ALPHA_DLC_1},       {ALPHA_DLC_1}),
    ("search misses", {"search": "nothing here"},    set()),
]


@pytest.mark.parametrize("case,variables,expected",
                         FILTER_CASES, ids=[c[0] for c in FILTER_CASES])
def test_filters_select_the_right_cards(library, case, variables, expected):
    page = cards(library, **variables)

    assert {i["appId"] for i in page["items"]} == expected
    assert page["total"] == len(expected)  # the count has to agree with the page


def test_ownership_is_the_app_ids_as_a_whole(library):
    """Grouped, an app id is owned when any of its versions is."""
    owned = cards(library, owned=True)
    missing = cards(library, owned=False)

    # Every app id here has at least one owned version, including the part-owned DLC.
    assert {i["appId"] for i in owned["items"]} == {ALPHA, BETA, ALPHA_DLC_1, ALPHA_DLC_2}
    assert missing["items"] == []


def test_base_cards_carry_the_titles_update_history(library):
    """A Switch update ships under its own app id, so a BASE card's versions are the
    title's UPDATE apps - that is what the version popover lists."""
    base = next(i for i in cards(library)["items"] if i["appId"] == ALPHA)

    assert [(v["version"], v["owned"]) for v in base["versions"]] == [(65536, True), (131072, False)]
    assert base["title"]["ownership"] == {"haveBase": True, "upToDate": False, "complete": True}


def test_dlc_cards_carry_their_own_versions_and_their_parent(library):
    dlc = next(i for i in cards(library)["items"] if i["appId"] == ALPHA_DLC_1)

    assert [(v["version"], v["owned"]) for v in dlc["versions"]] == [(0, True), (65536, False)]
    assert dlc["titledb"]["name"] == "Alpha Extra Levels"   # its own metadata
    assert dlc["title"]["name"] == "Alpha Game"             # the parent, for the card title


def test_ungrouped_apps_still_page_by_version_row(library):
    """groupByAppId is opt-in: without it the query still answers per (app id, version)."""
    query = """query { apps(page: 1, pageSize: 50, appType: ["DLC"]) { total items { appId } } }"""
    resp = library.client.get("/api/graphql", query_string={"query": query})
    body = resp.get_json()

    assert "errors" not in body, body["errors"]
    assert body["data"]["apps"]["total"] == 3  # two versions of DLC 1, one of DLC 2
