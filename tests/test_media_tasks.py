"""Tests for the tasks that fill the local artwork store.

Downloads are stubbed at `media.fetch`, so what is asserted is which URLs the task decided
to fetch and what it recorded - not how an image was transferred.
"""
import io
import json
import os
import time
import types

import pytest
from PIL import Image

import db as db_mod
import library as library_mod
import media
import settings as settings_mod
import tasks as tasks_mod
import titledb
from app import create_app
from db import Media, Titles, db, init_db
from titledb.schema import SOURCE_EXTRACT

TITLE_ID = "0100000000010000"
DLC_ID = "0100000000011000"
ICON_URL = "https://img/icon.jpg"
BANNER_URL = "https://img/banner.jpg"
SHOT_URLS = ["https://img/shot0.jpg", "https://img/shot1.jpg"]

RECORD = {
    "id": TITLE_ID,
    "name": "Some Game",
    "iconUrl": ICON_URL,
    "bannerUrl": BANNER_URL,
    "screenshots": SHOT_URLS,
}


def jpeg(url=""):
    """An image shaped like the artwork behind the URL: icons are square, the rest 720p."""
    size = (1024, 1024) if "icon" in url else (1280, 720)
    buffer = io.BytesIO()
    Image.new("RGB", size, "red").save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture
def install(tmp_path, monkeypatch):
    """An app with an empty library database, an imported titledb and an empty media store."""
    config = tmp_path / "config"
    config.mkdir()
    titledb_dir = tmp_path / "titledb"
    titledb_dir.mkdir()
    monkeypatch.setattr(db_mod, "DB_FILE", str(config / "ownfoil.db"))
    monkeypatch.setattr(db_mod, "TITLES_DB_FILE", str(config / "titles.db"))
    monkeypatch.setattr(titledb.store, "TITLES_DB_FILE", str(config / "titles.db"))
    monkeypatch.setattr(titledb.store, "DB_FILE", str(config / "ownfoil.db"))
    monkeypatch.setattr(media, "MEDIA_DIR", str(tmp_path / "media"))
    # Per-test settings, so switching the store off in one test can't reach the next.
    monkeypatch.setattr(settings_mod, "CONFIG_FILE", str(config / "settings.yaml"))
    monkeypatch.setattr(settings_mod, "KEYS_FILE", str(config / "keys.txt"))
    monkeypatch.setattr(settings_mod, "_cached_settings", None)

    app = create_app(f"sqlite:///{config / 'ownfoil.db'}")
    with app.app_context():
        init_db(app)

    fetched = []

    def fetch(url):
        fetched.append(url)
        return jpeg(url)

    monkeypatch.setattr(media, "fetch", fetch)
    return types.SimpleNamespace(app=app, titledb_dir=titledb_dir, fetched=fetched,
                                 media_dir=tmp_path / "media")


def _import(install, record=RECORD, dlc=None, extra=None):
    """Build titles.db from one title, its cnmts-linked DLC, and any unlinked `extra`."""
    records = {record["id"]: record}
    records.update({r["id"]: r for r in extra or []})
    cnmts = {}
    for dlc_record in dlc or []:
        records[dlc_record["id"]] = dlc_record
        # titledb writes both sides of this join in lowercase; only titles.id is uppercase.
        cnmts[dlc_record["id"].lower()] = {"0": {"titleType": 130,
                                                "otherApplicationId": record["id"].lower()}}
    region_file = install.titledb_dir / "titles.US.en.json"
    region_file.write_text(json.dumps(records))
    (install.titledb_dir / "cnmts.json").write_text(json.dumps(cnmts))
    (install.titledb_dir / "versions.json").write_text("{}")
    with install.app.app_context():
        titledb.store.import_from_json(str(region_file), "US.en")


def _own(install, *title_ids):
    with install.app.app_context():
        for title_id in title_ids:
            db.session.add(Titles(title_id=title_id))
        db.session.commit()


def _slots(install):
    with install.app.app_context():
        return {(m.title_id, m.kind, m.position): m for m in Media.query.all()}


def test_a_titles_artwork_is_stored_slot_by_slot(install):
    _import(install)

    with install.app.app_context():
        tasks_mod.download_title_media_task(TITLE_ID)

    assert sorted(install.fetched) == sorted([ICON_URL, BANNER_URL] + SHOT_URLS)
    slots = _slots(install)
    assert set(slots) == {
        (TITLE_ID, media.ICON, 0), (TITLE_ID, media.BANNER, 0),
        (TITLE_ID, media.SCREENSHOT, 0), (TITLE_ID, media.SCREENSHOT, 1),
    }
    icon = slots[(TITLE_ID, media.ICON, 0)]
    assert icon.filename == "icon.jpg"
    assert icon.source_url == ICON_URL
    assert (icon.width, icon.height) == (1024, 1024)
    assert (icon.client_width, icon.client_height) == (256, 256)
    assert media.have(media.ICON, "icon.jpg")

    banner = slots[(TITLE_ID, media.BANNER, 0)]
    assert (banner.client_width, banner.client_height) == (640, 360)


def test_artwork_already_on_disk_is_not_downloaded_again(install):
    _import(install)
    with install.app.app_context():
        tasks_mod.download_title_media_task(TITLE_ID)
        install.fetched.clear()
        tasks_mod.download_title_media_task(TITLE_ID)

    assert install.fetched == []


def test_a_missing_file_is_downloaded_again_even_though_the_row_stands(install):
    _import(install)
    with install.app.app_context():
        tasks_mod.download_title_media_task(TITLE_ID)
    (install.media_dir / media.ICON / media.CLIENT / "icon.jpg").unlink()

    with install.app.app_context():
        install.fetched.clear()
        tasks_mod.download_title_media_task(TITLE_ID)

    assert install.fetched == [ICON_URL]


def test_one_unreachable_image_does_not_lose_the_others(install, monkeypatch):
    _import(install)

    def fetch(url):
        if url == BANNER_URL:
            raise OSError("502")
        install.fetched.append(url)
        return jpeg(url)

    monkeypatch.setattr(media, "fetch", fetch)
    with install.app.app_context():
        tasks_mod.download_title_media_task(TITLE_ID)

    assert (TITLE_ID, media.BANNER, 0) not in _slots(install)
    assert (TITLE_ID, media.ICON, 0) in _slots(install)


def test_a_failed_slot_is_retried_later_rather_than_left_hotlinked(install, monkeypatch):
    """Otherwise a blip leaves the title hotlinked until the next titledb refresh."""
    _import(install)
    monkeypatch.setattr(media, "fetch", lambda url: (_ for _ in ()).throw(OSError("no dns")))
    scheduled = []
    monkeypatch.setattr(tasks_mod, "enqueue_task",
                        lambda name, data, run_after=None: scheduled.append((name, data)))

    with install.app.app_context():
        tasks_mod.download_title_media_task(TITLE_ID)
        tasks_mod.download_title_media_task(TITLE_ID, attempt=tasks_mod.MEDIA_RETRY_LIMIT)

    # One retry for the whole title, not one per failed image, and it gives up in the end.
    assert scheduled == [("download_title_media", {"title_id": TITLE_ID, "attempt": 1})]


def test_a_title_that_downloaded_cleanly_schedules_no_retry(install, monkeypatch):
    _import(install)
    scheduled = []
    monkeypatch.setattr(tasks_mod, "enqueue_task",
                        lambda name, data, run_after=None: scheduled.append((name, data)))

    with install.app.app_context():
        tasks_mod.download_title_media_task(TITLE_ID)

    assert scheduled == []


def test_a_record_that_cannot_be_read_leaves_the_store_alone(install, monkeypatch):
    """An unreadable record means "cannot say" - titles.db is rebuilt on a schema change."""
    _import(install)
    with install.app.app_context():
        tasks_mod.download_title_media_task(TITLE_ID)
    before = set(_slots(install))
    monkeypatch.setattr(titledb.store, "get_title_record", lambda title_id: None)

    with install.app.app_context():
        tasks_mod.download_title_media_task(TITLE_ID)

    assert set(_slots(install)) == before


def test_a_lost_row_is_rebuilt_from_the_stored_bytes(install):
    _import(install)
    with install.app.app_context():
        tasks_mod.download_title_media_task(TITLE_ID)
        Media.query.delete()
        db.session.commit()
        install.fetched.clear()
        tasks_mod.download_title_media_task(TITLE_ID)

    assert install.fetched == []
    icon = _slots(install)[(TITLE_ID, media.ICON, 0)]
    assert icon.filename == "icon.jpg"
    assert (icon.width, icon.height) == (1024, 1024)
    assert (icon.client_width, icon.client_height) == (256, 256)


def test_a_shrunken_screenshot_list_drops_the_slots_it_no_longer_fills(install):
    _import(install)
    with install.app.app_context():
        tasks_mod.download_title_media_task(TITLE_ID)
    assert (TITLE_ID, media.SCREENSHOT, 1) in _slots(install)

    _import(install, dict(RECORD, screenshots=SHOT_URLS[:1]))
    with install.app.app_context():
        tasks_mod.download_title_media_task(TITLE_ID)

    slots = _slots(install)
    assert (TITLE_ID, media.SCREENSHOT, 0) in slots
    assert (TITLE_ID, media.SCREENSHOT, 1) not in slots


def test_artwork_already_in_the_store_is_left_alone(install):
    """An extracted icon files itself at extraction time, so its URL is already local."""
    local = media.url_for(TITLE_ID, media.ICON, 0, media.ORIGINAL, "abc.jpg")
    _import(install, dict(RECORD, iconUrl=local))

    with install.app.app_context():
        tasks_mod.download_title_media_task(TITLE_ID)

    assert ICON_URL not in install.fetched
    assert local not in install.fetched


def test_a_title_no_source_describes_is_not_an_error(install):
    _import(install)

    with install.app.app_context():
        tasks_mod.download_title_media_task("0100000000099000")

    assert install.fetched == []
    assert _slots(install) == {}


def test_the_parent_fans_out_one_child_per_title_and_dlc(install, monkeypatch):
    """DLC have their own banners and screenshots, and `Title.availableDlc` serves them."""
    _import(install, dlc=[dict(RECORD, id=DLC_ID, name="Some DLC")])
    _own(install, TITLE_ID, "0100000000020000")
    children = []
    monkeypatch.setattr(tasks_mod, "enqueue_or_child",
                        lambda name, data: children.append((name, data)))
    waited = []
    monkeypatch.setattr(tasks_mod, "set_waiting_for_children", lambda: waited.append(True))

    with install.app.app_context():
        tasks_mod.download_media_task()

    # 0100000000020000 is owned but titledb describes no such title, so there is nothing to
    # fetch for it and no child is spent finding that out.
    assert children == [("download_title_media", {"title_id": TITLE_ID}),
                        ("download_title_media", {"title_id": DLC_ID})]
    assert waited == [True]


def test_an_owned_dlc_is_covered_even_when_cnmts_does_not_link_it(install):
    """It still reaches the client as `App.titledb`, so the apps rows are a source too."""
    orphan, update = "0100000000012000", "0100000000010800"
    # cnmts links DLC_ID to its base title; `orphan` is only ever known as an app of it. The
    # update has a titles row of its own, as cnmts gives every update, but no artwork on it.
    _import(install, dlc=[dict(RECORD, id=DLC_ID)],
            extra=[dict(RECORD, id=orphan), {"id": update, "name": "Some Game Update"}])
    with install.app.app_context():
        title = Titles(title_id=TITLE_ID)
        title.apps.append(db_mod.Apps(app_id=orphan, app_type="DLC"))
        title.apps.append(db_mod.Apps(app_id=update, app_type="UPDATE"))
        db.session.add(title)
        db.session.commit()

        # Existing is not the question - the update is dropped for having nothing to fetch.
        assert tasks_mod.media_title_ids(TITLE_ID) == [TITLE_ID, orphan, DLC_ID]


def test_a_dlcs_own_artwork_is_stored_under_its_own_id(install):
    _import(install, dlc=[dict(RECORD, id=DLC_ID, name="Some DLC")])

    with install.app.app_context():
        tasks_mod.download_title_media_task(DLC_ID)

    assert (DLC_ID, media.BANNER, 0) in _slots(install)


def test_a_newly_identified_title_fetches_its_own_artwork(monkeypatch):
    """The per-title chain is the only one a new title goes down, so artwork hangs off it.

    Without this a game added between titledb refreshes stays hotlinked until the next one,
    which is up to the whole update interval - and adding games is the common case.
    """
    children = []
    monkeypatch.setattr(tasks_mod, "add_missing_apps_for_title", lambda title_id: None)
    monkeypatch.setattr(tasks_mod, "enqueue_or_child",
                        lambda name, data: children.append((name, data)))
    monkeypatch.setattr(tasks_mod, "set_waiting_for_children", lambda: None)
    monkeypatch.setattr(tasks_mod, "media_title_ids", lambda title_id: [TITLE_ID, DLC_ID])

    tasks_mod.add_missing_apps_for_title_task(TITLE_ID)

    assert children == [("update_titles_for_title", {"title_id": TITLE_ID}),
                        ("download_title_media", {"title_id": TITLE_ID}),
                        ("download_title_media", {"title_id": DLC_ID})]


def test_an_empty_library_parks_no_parent(install, monkeypatch):
    waited = []
    monkeypatch.setattr(tasks_mod, "set_waiting_for_children", lambda: waited.append(True))

    with install.app.app_context():
        tasks_mod.download_media_task()

    assert waited == []


# --- the store switched off ---
def test_nothing_is_downloaded_while_the_store_is_off(install, monkeypatch):
    _import(install, dlc=[dict(RECORD, id=DLC_ID)])
    _own(install, TITLE_ID)
    children = []
    monkeypatch.setattr(tasks_mod, "enqueue_or_child",
                        lambda name, data: children.append((name, data)))
    settings_mod.set_local_media_settings({"enabled": False})

    with install.app.app_context():
        tasks_mod.download_media_task()
        # Reached directly too: a retry scheduled before the switch comes back to this task.
        tasks_mod.download_title_media_task(TITLE_ID)

    assert children == []
    assert install.fetched == []
    assert _slots(install) == {}


def test_a_newly_identified_title_spends_no_child_while_the_store_is_off(install, monkeypatch):
    """The per-title chain would otherwise enqueue one child per title and DLC to no effect."""
    children = []
    monkeypatch.setattr(tasks_mod, "add_missing_apps_for_title", lambda title_id: None)
    monkeypatch.setattr(tasks_mod, "enqueue_or_child",
                        lambda name, data: children.append((name, data)))
    monkeypatch.setattr(tasks_mod, "set_waiting_for_children", lambda: None)
    monkeypatch.setattr(tasks_mod, "media_title_ids", lambda title_id: [TITLE_ID, DLC_ID])
    settings_mod.set_local_media_settings({"enabled": False})

    tasks_mod.add_missing_apps_for_title_task(TITLE_ID)

    assert children == [("update_titles_for_title", {"title_id": TITLE_ID})]


def test_switching_the_store_off_keeps_what_it_already_holds(install):
    """Disabling stops downloads; it is not a delete, and the stored copies stay servable."""
    _import(install)
    with install.app.app_context():
        tasks_mod.download_title_media_task(TITLE_ID)
    stored = set(_slots(install))
    settings_mod.set_local_media_settings({"enabled": False})

    with install.app.app_context():
        tasks_mod.download_title_media_task(TITLE_ID)

    assert set(_slots(install)) == stored
    assert media.have(media.ICON, "icon.jpg")


def test_usage_counts_both_renditions_of_every_kind(install):
    _import(install)
    with install.app.app_context():
        tasks_mod.download_title_media_task(TITLE_ID)

    usage = media.usage()
    assert usage[media.ICON][media.ORIGINAL]["files"] == 1
    assert usage[media.SCREENSHOT][media.CLIENT]["files"] == len(SHOT_URLS)
    # A rendition is a smaller re-encode of the same image, and nothing is stored for a kind
    # the title has none of.
    assert 0 < usage[media.BANNER][media.CLIENT]["bytes"] < usage[media.BANNER][media.ORIGINAL]["bytes"]
    assert usage[media.BOXART][media.ORIGINAL] == {"bytes": 0, "files": 0}


# --- collecting what the library no longer covers ---

def _forget(install, title_id):
    """Lose a title the way losing its last owned file does, apps and all."""
    with install.app.app_context():
        db.session.delete(Titles.query.filter_by(title_id=title_id).one())
        db.session.commit()


def _backdate(directory):
    """Age the store past the grace, the way a sweep of anything but a live download sees it."""
    old = time.time() - media.COLLECT_GRACE - 60
    for path in directory.rglob("*"):
        if path.is_file():
            os.utime(path, (old, old))


def test_the_artwork_of_a_title_that_left_the_library_is_dropped(install):
    _import(install)
    _own(install, TITLE_ID)
    with install.app.app_context():
        tasks_mod.download_title_media_task(TITLE_ID)

    _forget(install, TITLE_ID)
    _backdate(install.media_dir)
    with install.app.app_context():
        library_mod.remove_orphan_media()

    assert _slots(install) == {}
    assert not media.have(media.ICON, "icon.jpg")


def test_a_file_two_titles_share_outlives_one_of_them(install):
    """Content addressing puts one file behind every title using that image, so what a title
    leaving may retire is a row - never, on its own, the bytes."""
    other = "0100000000020000"
    _import(install, extra=[dict(RECORD, id=other)])
    _own(install, TITLE_ID, other)
    with install.app.app_context():
        tasks_mod.download_title_media_task(TITLE_ID)
        tasks_mod.download_title_media_task(other)

    _forget(install, TITLE_ID)
    _backdate(install.media_dir)
    with install.app.app_context():
        library_mod.remove_orphan_media()

    assert {title_id for title_id, _kind, _position in _slots(install)} == {other}
    assert media.have(media.ICON, "icon.jpg")


def test_a_slot_that_changed_image_leaves_no_file_behind(install):
    """A locale switch rewrites the URLs, so a file falls out of use with every title in place
    and nothing to hang a per-title cleanup off."""
    _import(install)
    _own(install, TITLE_ID)
    with install.app.app_context():
        tasks_mod.download_title_media_task(TITLE_ID)
    _import(install, record=dict(RECORD, iconUrl="https://img/icon-de.jpg"))
    with install.app.app_context():
        tasks_mod.download_title_media_task(TITLE_ID)
    _backdate(install.media_dir)

    with install.app.app_context():
        library_mod.remove_orphan_media()

    assert media.have(media.ICON, "icon-de.jpg")
    assert not media.have(media.ICON, "icon.jpg")


def test_an_extracted_icon_the_dump_outranks_keeps_its_file(install):
    """titledb wins the slot, so the row names its image and not the extracted one - but the
    override still names it, and serves it again the day the dump stops describing the title."""
    _import(install)
    _own(install, TITLE_ID)
    with install.app.app_context():
        url = media.store_extracted(TITLE_ID, media.ICON, jpeg("icon"))
        titledb.store.set_override(TITLE_ID, {"id": TITLE_ID, "iconUrl": url},
                                   source=SOURCE_EXTRACT)
        tasks_mod.download_title_media_task(TITLE_ID)
    _backdate(install.media_dir)

    with install.app.app_context():
        library_mod.remove_orphan_media()

    assert _slots(install)[(TITLE_ID, media.ICON, 0)].filename == "icon.jpg"
    assert media.have(media.ICON, url.rsplit("/", 1)[1])


def test_a_dlc_is_kept_by_its_base_title_and_dropped_with_it(install):
    """A DLC is a row in `titles` never and in `apps` only when owned, so cnmts is the only
    thing separating its artwork from a leftover."""
    _import(install, dlc=[dict(RECORD, id=DLC_ID)])
    _own(install, TITLE_ID)
    with install.app.app_context():
        tasks_mod.download_title_media_task(DLC_ID)
        library_mod.remove_orphan_media()
    assert {title_id for title_id, _kind, _position in _slots(install)} == {DLC_ID}

    _forget(install, TITLE_ID)
    with install.app.app_context():
        library_mod.remove_orphan_media()
    assert _slots(install) == {}


def test_an_app_of_an_owned_title_keeps_its_own_artwork(install):
    """Apps have artwork under their own id and no `titles` row to be found by."""
    app_id = "0100000000010800"
    _import(install, extra=[dict(RECORD, id=app_id)])
    with install.app.app_context():
        title = Titles(title_id=TITLE_ID)
        title.apps.append(db_mod.Apps(app_id=app_id, app_type="DLC"))
        db.session.add(title)
        db.session.commit()

        tasks_mod.download_title_media_task(app_id)
        library_mod.remove_orphan_media()

    assert {title_id for title_id, _kind, _position in _slots(install)} == {app_id}


def test_nothing_is_collected_while_titledb_cannot_be_read(install, monkeypatch, tmp_path):
    """titles.db is rebuilt from scratch when its schema fingerprint changes, and in that
    window every DLC looks unclaimed - a sweep that believed it would delete the lot."""
    _import(install, dlc=[dict(RECORD, id=DLC_ID)])
    _own(install, TITLE_ID)
    with install.app.app_context():
        tasks_mod.download_title_media_task(DLC_ID)
    monkeypatch.setattr(titledb.store, "TITLES_DB_FILE", str(tmp_path / "gone.db"))

    with install.app.app_context():
        library_mod.remove_orphan_media()

    assert {title_id for title_id, _kind, _position in _slots(install)} == {DLC_ID}


def test_the_extracted_metadata_of_a_departed_title_goes_too(install):
    """An extract override outlives its files otherwise, keeping a titles.db row alive that
    names an icon in the store."""
    orphan = "0100000000099000"
    _import(install)
    with install.app.app_context():
        titledb.store.set_override(orphan, {"id": orphan, "name": "Read from the file"},
                                   source=SOURCE_EXTRACT)
        assert titledb.store.get_title_record(orphan)["name"] == "Read from the file"

        library_mod.remove_orphan_media()

        assert db_mod.list_title_overrides(SOURCE_EXTRACT) == []
        assert titledb.store.get_title_record(orphan) is None
