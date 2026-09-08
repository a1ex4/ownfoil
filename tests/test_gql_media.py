"""Tests for how artwork surfaces through GraphQL.

A local copy substitutes for the catalogue's URL; without one the field still answers, with
the upstream URL and `local: false`. Both halves matter - a title whose download has not run
yet has to render.
"""

import json
import types

import pytest

import db as db_mod
import media
import titledb
from app import create_app
from constants import APP_TYPE_BASE
from db import Apps, Titles, db, init_db, upsert_media
from gql import graphql_dispatch

LOCAL = "0100000000AAAA00"
REMOTE = "0100000000BBBB00"

ICON_URL = "https://img-eshop.cdn.nintendo.net/i/icon.jpg"
BANNER_URL = "https://img-eshop.cdn.nintendo.net/i/banner.jpg"
SHOT_URLS = ["https://img-eshop.cdn.nintendo.net/i/shot0.jpg",
             "https://img-eshop.cdn.nintendo.net/i/shot1.jpg"]

TITLEDB_JSON = {
    LOCAL: {"id": LOCAL, "name": "Stored Game", "iconUrl": ICON_URL,
            "bannerUrl": BANNER_URL, "screenshots": SHOT_URLS},
    REMOTE: {"id": REMOTE, "name": "Unfetched Game", "iconUrl": ICON_URL,
             "bannerUrl": BANNER_URL},
}


@pytest.fixture
def catalogue(tmp_path, monkeypatch):
    """Two owned titles: one whose artwork has been downloaded, one whose has not."""
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
        for title_id in (LOCAL, REMOTE):
            title = Titles(title_id=title_id, have_base=True)
            db.session.add(title)
            db.session.flush()
            db.session.add(Apps(title_id=title.id, app_id=title_id, app_version="0",
                                app_type=APP_TYPE_BASE, owned=True))
        db.session.commit()

        upsert_media(LOCAL, media.ICON, 0, source="titledb", source_url=ICON_URL,
                     filename="icon.jpg", size=(1024, 1024), client_size=(256, 256))
        upsert_media(LOCAL, media.BANNER, 0, source="titledb", source_url=BANNER_URL,
                     filename="banner.jpg", size=(1280, 720), client_size=(640, 360))
        for position, url in enumerate(SHOT_URLS):
            upsert_media(LOCAL, media.SCREENSHOT, position, source="titledb", source_url=url,
                         filename=f"shot{position}.jpg", size=(1280, 720),
                         client_size=(640, 360))

    return types.SimpleNamespace(app=app, client=app.test_client())


def query(catalogue, text_query):
    resp = catalogue.client.get("/api/graphql", query_string={"query": text_query})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert "errors" not in body, body["errors"]
    return body["data"]


def _title(catalogue, title_id, selection):
    return query(catalogue, 'query { title(titleId: "%s") { %s } }'
                 % (title_id, selection))["title"]


# (requested size, the URL segment and dimensions it resolves to)
SIZE_CASES = [
    ("CLIENT", "client", 256, 256),
    ("ORIGINAL", "original", 1024, 1024),
]


@pytest.mark.parametrize("size,segment,width,height", SIZE_CASES)
def test_the_requested_size_picks_the_rendition(catalogue, size, segment, width, height):
    icon = _title(catalogue, LOCAL,
                  "icon(size: %s) { url size local width height }" % size)["icon"]

    assert icon == {
        "url": f"/api/media/{LOCAL}/icon/0/{segment}/icon.jpg",
        "size": size, "local": True, "width": width, "height": height,
    }


def test_client_is_the_default_size(catalogue):
    assert (_title(catalogue, LOCAL, "icon { url }")["icon"]
            == _title(catalogue, LOCAL, "icon(size: CLIENT) { url }")["icon"])


def test_a_title_with_no_local_copy_still_answers_with_the_catalogue_url(catalogue):
    icon = _title(catalogue, REMOTE, "icon { url size local width height }")["icon"]

    assert icon == {"url": ICON_URL, "size": "ORIGINAL", "local": False,
                    "width": None, "height": None}


def test_screenshots_keep_their_order_and_carry_their_position(catalogue):
    shots = _title(catalogue, LOCAL, "screenshots { url }")["screenshots"]

    assert [s["url"] for s in shots] == [
        f"/api/media/{LOCAL}/screenshot/0/client/shot0.jpg",
        f"/api/media/{LOCAL}/screenshot/1/client/shot1.jpg",
    ]


def test_a_slot_no_source_fills_is_null(catalogue):
    assert _title(catalogue, LOCAL, "frontBoxArt { url }")["frontBoxArt"] is None
    assert _title(catalogue, REMOTE, "screenshots { url }")["screenshots"] is None


def test_the_listing_and_the_app_path_substitute_too(catalogue):
    """index.html reads artwork through `apps { titledb }`, not through `titles`."""
    listing = query(catalogue, """
        query { titles(orderBy: {field: NAME}) { items { titleId icon { url local } } } }
        """)["titles"]["items"]
    by_id = {t["titleId"]: t["icon"] for t in listing}
    assert by_id[LOCAL]["local"] is True
    assert by_id[REMOTE]["local"] is False

    apps = query(catalogue, """
        query { apps(groupByAppId: true) { items { appId titledb { icon { url local } } } } }
        """)["apps"]["items"]
    icons = {a["appId"]: a["titledb"]["icon"] for a in apps}
    assert icons[LOCAL]["url"] == f"/api/media/{LOCAL}/icon/0/client/icon.jpg"
    assert icons[REMOTE]["url"] == ICON_URL


def test_not_asking_for_artwork_runs_no_media_query(catalogue, monkeypatch):
    import gql.resolvers as resolvers

    calls = []
    original = resolvers._hydrate_title_media
    monkeypatch.setattr(resolvers, "_hydrate_title_media",
                        lambda titles, sel: calls.append(sel) or original(titles, sel))

    query(catalogue, "query { titles { items { titleId name } } }")

    assert all(not any(s.has(f) for f in resolvers._MEDIA_FIELDS) for s in calls)
