"""Download counting: what counts as one download, and who counts as one client.

Both download routes go through the same 60s throttle, keyed per (file, client). The window
is what makes a Sphaira download - a HEAD for the size, then the GET - count once instead of
twice, and it is also what makes the client's identity matter: get that wrong and one
person's download suppresses everybody else's.
"""
import types

import pytest

from db import Files
from settings import set_shop_settings

FILENAME = "Test Game [0100000000010000][v0].nsp"
SPHAIRA_PATH = f"/Test Game/{FILENAME}"
# What identifies Sphaira: without it the shop route serves the Web UI instead.
SPHAIRA_HEADERS = {"User-Agent": "Sphaira/1.0.6"}

# Two people behind the same reverse proxy: identical remote_addr, different real address.
PROXIED = [{"X-Forwarded-For": "203.0.113.7"}, {"X-Forwarded-For": "203.0.113.8"}]


@pytest.fixture
def shop(shop_app):
    """A public shop, so these tests are about counting and not about authentication."""
    set_shop_settings({"public": True})
    return shop_app


@pytest.fixture
def clock(monkeypatch):
    """A hand-wound monotonic clock, so the throttle window can be crossed without waiting."""
    import utils

    now = [10_000.0]
    monkeypatch.setattr(utils, "time", types.SimpleNamespace(monotonic=lambda: now[0]))
    return now


def count(shop):
    with shop.app.app_context():
        return Files.query.filter_by(filename=FILENAME).first().download_count


def file_id(shop):
    with shop.app.app_context():
        return Files.query.filter_by(filename=FILENAME).first().id


def sphaira_download(shop, headers=None):
    return shop.client.get(SPHAIRA_PATH, headers={**SPHAIRA_HEADERS, **(headers or {})})


def get_game_download(shop, headers=None):
    return shop.client.get(f"/api/get_game/{file_id(shop)}", headers=headers or {})


# Both routes serve files, so both have to count them the same way.
ROUTES = {"sphaira": sphaira_download, "get_game": get_game_download}


@pytest.fixture(params=sorted(ROUTES))
def download(request, shop):
    route = ROUTES[request.param]
    return lambda headers=None: route(shop, headers)


def test_a_download_is_counted(shop, download):
    assert download().status_code == 200
    assert count(shop) == 1


def test_repeat_within_the_window_counts_once(shop, download):
    download()
    download()
    assert count(shop) == 1


def test_the_window_expires(shop, download, clock):
    download()
    clock[0] += 61
    download()
    assert count(shop) == 2


def test_clients_behind_a_proxy_count_separately(shop, download):
    """remote_addr is the proxy for all of them; only X-Forwarded-For tells them apart."""
    download(PROXIED[0])
    download(PROXIED[1])
    assert count(shop) == 2


def test_sphaira_head_then_get_counts_one_download(shop):
    """Sphaira asks for the headers before it downloads - that is one download, not two."""
    assert shop.client.head(SPHAIRA_PATH, headers=SPHAIRA_HEADERS).status_code == 200
    assert shop.client.get(SPHAIRA_PATH, headers=SPHAIRA_HEADERS).status_code == 200
    assert count(shop) == 1


def test_an_unserved_file_is_never_counted(shop):
    shop.client.get("/Test Game/Not A Real File.nsp", headers=SPHAIRA_HEADERS)
    assert count(shop) == 0


def test_a_range_past_the_end_of_the_file_is_never_counted(shop, download):
    """Clients probe a file with a Range before taking it; a refused probe is no download."""
    assert download({"Range": "bytes=61440-61443"}).status_code == 416
    assert count(shop) == 0
