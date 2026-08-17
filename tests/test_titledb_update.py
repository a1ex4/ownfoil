"""Tests for fetching titledb from the GitHub release.

The app reads a `latest` marker asset, compares it to the hash it stored last time, and pulls
the per-file `.zst` assets it needs. The build writes that marker only after every data asset
is in place, so the contract these tests pin down is: the local marker advances if and only if
every file it implies is on disk and imported. A failure anywhere has to leave the previous
revision intact so the next run retries it, rather than skipping it forever.
"""

import json
import os
import types

import pytest
import requests
import zstandard

import titledb
from titledb import update as update_mod


OLD_COMMIT = "1111111111111111111111111111111111111111"
NEW_COMMIT = "2222222222222222222222222222222222222222"

DEFAULTS = ["cnmts.json", "versions.json", "languages.json"]
REGION_FILE = "titles.US.en.json"
SETTINGS = {"titles": {"region": "US", "language": "en"}}


class FakeResponse:
    def __init__(self, body, status=200):
        self._body = body
        self.status = status

    @property
    def text(self):
        return self._body.decode()

    def iter_content(self, size):
        for i in range(0, len(self._body), size):
            yield self._body[i:i + size]

    def raise_for_status(self):
        if self.status != 200:
            raise requests.HTTPError(f"{self.status}")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def remote(tmp_path, monkeypatch):
    """A fake release whose assets are served to the real download code, plus a request log."""
    titledb_dir = tmp_path / "titledb"
    titledb_dir.mkdir()
    monkeypatch.setattr(update_mod, "TITLEDB_DIR", str(titledb_dir))

    # The import is exercised by test_titledb_lifecycle; here it only has to not need a database.
    imported = []
    monkeypatch.setattr(update_mod.store, "get_imported_locale", lambda: "US.en")
    monkeypatch.setattr(update_mod.store, "import_from_json", lambda path, locale: imported.append(path))

    state = types.SimpleNamespace(
        dir=titledb_dir,
        commit=NEW_COMMIT,
        assets={},
        requested=[],
        imported=imported,
        broken=set(),
        truncate=set(),
    )

    def publish(name, payload):
        state.assets[name] = zstandard.ZstdCompressor().compress(json.dumps(payload).encode())

    for name in DEFAULTS + [REGION_FILE, "titles.FR.fr.json"]:
        publish(name, {"file": name})
    state.publish = publish

    def fake_get(url, **kwargs):
        name = url.rsplit("/", 1)[-1]
        state.requested.append(name)
        if name == "latest":
            return FakeResponse(state.commit.encode())
        asset = name[: -len(".zst")]
        if asset in state.broken or asset not in state.assets:
            return FakeResponse(b"", status=404)
        body = state.assets[asset]
        if asset in state.truncate:
            body = body[: len(body) // 2]
        return FakeResponse(body)

    monkeypatch.setattr(update_mod.requests, "get", fake_get)
    return state


def marker(remote):
    path = remote.dir / ".latest"
    return path.read_text() if path.exists() else None


def downloaded(remote):
    return sorted(f for f in os.listdir(remote.dir) if f.endswith(".json"))


# ------------- the happy path -------------

def test_first_run_downloads_and_decompresses_every_needed_file(remote):
    titledb.update_titledb(SETTINGS)

    assert downloaded(remote) == sorted(DEFAULTS + [REGION_FILE])
    for name in DEFAULTS + [REGION_FILE]:
        assert json.loads((remote.dir / name).read_text()) == {"file": name}
    assert marker(remote) == NEW_COMMIT
    assert remote.imported == [str(remote.dir / REGION_FILE)]


def test_no_assets_are_fetched_when_the_marker_matches(remote):
    for name in DEFAULTS + [REGION_FILE]:
        (remote.dir / name).write_text("{}")
    (remote.dir / ".latest").write_text(NEW_COMMIT)

    titledb.update_titledb(SETTINGS)

    assert remote.requested == ["latest"]


def test_a_missing_region_file_is_fetched_even_when_the_marker_matches(remote):
    for name in DEFAULTS:
        (remote.dir / name).write_text("{}")
    (remote.dir / ".latest").write_text(NEW_COMMIT)

    titledb.update_titledb(SETTINGS)

    assert remote.requested == ["latest", f"{REGION_FILE}.zst"]
    assert json.loads((remote.dir / REGION_FILE).read_text()) == {"file": REGION_FILE}


def test_other_locales_already_on_disk_are_refreshed_too(remote):
    (remote.dir / "titles.FR.fr.json").write_text("{}")
    (remote.dir / ".latest").write_text(OLD_COMMIT)

    titledb.update_titledb(SETTINGS)

    assert "titles.FR.fr.json.zst" in remote.requested
    assert json.loads((remote.dir / "titles.FR.fr.json").read_text()) == {"file": "titles.FR.fr.json"}


# ------------- failures must not advance the marker -------------

@pytest.mark.parametrize("failure", ["broken", "truncate"])
def test_a_failed_asset_leaves_the_previous_revision_untouched(remote, failure):
    getattr(remote, failure).add(REGION_FILE)
    (remote.dir / REGION_FILE).write_text('{"kept": true}')
    (remote.dir / ".latest").write_text(OLD_COMMIT)

    with pytest.raises(Exception):
        titledb.update_titledb(SETTINGS)

    assert marker(remote) == OLD_COMMIT, "a failed download must be retried, not skipped forever"
    assert json.loads((remote.dir / REGION_FILE).read_text()) == {"kept": True}


def test_an_unreachable_marker_does_not_touch_the_local_one(remote):
    (remote.dir / ".latest").write_text(OLD_COMMIT)
    remote.commit = None

    def boom(url, **kwargs):
        raise requests.ConnectionError("offline")

    update_mod.requests.get = boom
    with pytest.raises(requests.ConnectionError):
        titledb.update_titledb(SETTINGS)

    assert marker(remote) == OLD_COMMIT


def test_a_leftover_tmp_file_is_not_mistaken_for_a_region_file(remote):
    (remote.dir / f"{REGION_FILE}.tmp").write_text("partial")
    (remote.dir / ".latest").write_text(OLD_COMMIT)

    titledb.update_titledb(SETTINGS)

    assert f"{REGION_FILE}.tmp.zst" not in remote.requested
    assert marker(remote) == NEW_COMMIT
