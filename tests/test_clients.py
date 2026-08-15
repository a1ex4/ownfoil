"""The shop clients, replayed from traffic recorded off the real hardware.

Every request here was sent by a client on a Switch against the capture harness' fixture
shop (`tests/capture/`), headers and all, and is replayed byte for byte through the app.
That is the point of the captures: each client is identified by the exact set of headers it
sends - and by the ones it does not send - so a hand-written request would only prove the
test agrees with itself.

What a client cannot vary from hardware is varied here on top of the captured headers: the
content filters, host verification over HTTPS, refusals on the download route.
"""
import base64
import json
import os
import re
from collections import namedtuple

import pytest

import fixture
import scenarios
from clients.cyberfoil import CyberFoilClient
from clients.sphaira import SphairaClient
from clients.tinfoil import TinfoilClient
from constants import APP_TYPE_BASE, APP_TYPE_DLC, APP_TYPE_UPD
from db import Files
from settings import get_settings, set_shop_settings

CAPTURES = os.path.join(os.path.dirname(__file__), "captures")

# How each client's shop differs from the others:
#   readable   the capture whose shop is not encrypted - what content assertions replay
#   root       the url its shop lives at
#   hauth      whether it verifies the host over HTTPS
#   file_route whether it downloads through /api/get_game/<id> or from its own handler
#   directories whether its shop is a directory tree rather than a flat list of files
Client = namedtuple("Client", "cls readable root hauth file_route directories")

CLIENTS = {
    "tinfoil": Client(TinfoilClient, "tinfoil-plaintext", "/", True, True, False),
    "cyberfoil": Client(CyberFoilClient, "authenticated-browse", "/", True, True, False),
    "sphaira": Client(SphairaClient, "authenticated-browse", "/", False, False, True),
}

RECORDED = [name for name in CLIENTS if os.path.isdir(os.path.join(CAPTURES, name))]
HAUTH = [name for name in RECORDED if CLIENTS[name].hauth]
FILE_ROUTE = [name for name in RECORDED if CLIENTS[name].file_route]

pytestmark = pytest.mark.skipif(
    not RECORDED,
    reason="no captures: record them with tests/capture/run_capture.py --client <client>")


def load(client, name):
    path = os.path.join(CAPTURES, client, f"{name}.json")
    if not os.path.exists(path):
        pytest.skip(f"{name} was not captured for {client}")
    with open(path) as f:
        return json.load(f)


def authorization(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def headers_of(client, name, index=0):
    """The captured headers, with the scenario's credentials put back.

    Credentials that aren't fixture accounts are redacted on the way to disk, so the unknown
    user and the wrong password only survive as placeholders. The scenario knows what was
    typed; restoring it from there keeps those refusals exact.
    """
    credentials = scenarios.BY_NAME[name].credentials
    return [[key, authorization(*credentials) if key.lower() == "authorization" else value]
            for key, value in load(client, name)["exchanges"][index]["request"]["headers"]]


def sent(client, name, header, index=0):
    """A header value as the client sent it in a capture.

    The host and the device-derived values are pseudonyms - stable within a set of captures,
    but not across a re-record, so the tests read them back rather than spelling them out.
    """
    return dict(load(client, name)["exchanges"][index]["request"]["headers"])[header]


@pytest.fixture
def shop(shop_app):
    """The fixture shop, on an empty database."""
    # Each client is identified partly by its User-Agent - Tinfoil by having none at all -
    # and the test client supplies its own unless the whole environ base is replaced, which
    # play() does per replay with the address the request was recorded from.
    return shop_app


def play(shop, client, name, index=0, headers=None, path=None, settings=None):
    """Apply a capture's shop settings and replay one of its exchanges."""
    capture = load(client, name)
    set_shop_settings({**capture["shop_settings"], **(settings or {})})
    exchange = capture["exchanges"][index]
    request = exchange["request"]
    shop.client.environ_base = {"REMOTE_ADDR": request["remote_addr"]}
    response = shop.client.open(path or request["path"], method=request["method"],
                                query_string=request["query"],
                                headers=headers or headers_of(client, name, index))
    return exchange, response


def listed(response):
    """What a shop response lists, in whichever form the client's shop takes.

    A url shop carries the filename in the fragment of each url; Sphaira's shop is a page of
    links, one per entry of the directory being browsed.
    """
    if response.mimetype == "text/html":
        return sorted(re.findall(r'<a href="([^"]*)">', response.get_data(as_text=True)))
    return sorted(entry["url"].split("#", 1)[1] for entry in response.get_json()["files"])


def error_of(response):
    """The refusal a shop response carries, in the client's own format."""
    if response.mimetype == "text/html":
        # Sphaira has nowhere to put an error but the listing, so it numbers the lines:
        # "00 - ERROR", "01 - Shop requires authentication.", "02 - Unknown user ghost."
        return "\n".join(item.split(" - ", 1)[1] for item in listed(response)[1:])
    return response.get_json()["error"]


def is_error(response):
    """Whether a shop response is a refusal - never encrypted, whatever the shop is."""
    if response.mimetype == "text/html":
        return listed(response)[:1] == ["00 - ERROR"]
    return response.mimetype == "application/json" and "error" in response.get_json()


def body_matches(exchange, response):
    """Whether a response is the one that was recorded, json shop or html listing."""
    body = exchange["response"]["body"]
    if body["kind"] == "json":
        return response.get_json() == body["json"]
    return response.get_data(as_text=True) == body["text"]


def count(shop, filename):
    with shop.app.app_context():
        return Files.query.filter_by(filename=filename).first().download_count


def in_library(client, match=lambda entry: True):
    """What the client's shop shows for the fixture library entries the predicate accepts.

    A url shop lists every file at once; Sphaira browses directories, so the root of its
    shop shows the first path segment of each file instead.
    """
    entries = [entry for entry in fixture.LIBRARY if match(entry)]
    if CLIENTS[client].directories:
        return sorted({entry["relpath"].split("/")[0] + "/" for entry in entries})
    return sorted(os.path.basename(entry["relpath"]) for entry in entries)


def unidentified(entry):
    return not entry.get("identified", True)


def exchanges(client, name, match):
    """Indexes of the exchanges in a capture the predicate accepts."""
    return [index for index, exchange in enumerate(load(client, name)["exchanges"])
            if match(exchange)]


def browsing(client, name):
    """The shop requests: everything the server did not answer with a file."""
    return exchanges(client, name, lambda e: "Content-Disposition" not in
                     dict(e["response"]["headers"]))


def transfers(client, name):
    """The exchanges that actually transferred a file, in the order they happened."""
    return exchanges(client, name, lambda e: "Content-Disposition" in
                     dict(e["response"]["headers"]))


def served_filename(exchange):
    """The filename the server said it was serving, out of Content-Disposition."""
    disposition = dict(exchange["response"]["headers"])["Content-Disposition"]
    return disposition.split('filename="', 1)[1].rstrip('"')


def one_transfer(client, name="download", method=None):
    """The first transfer in a capture, optionally of one method.

    Sphaira HEADs a file before it GETs it, and a HEAD carries no body - so a test that
    reads what came back has to ask for the GET.
    """
    indexes = [index for index in transfers(client, name)
               if method in (None, load(client, name)["exchanges"][index]["request"]["method"])]
    if not indexes:
        pytest.skip(f"{client} transferred no file in the {name} capture")
    return indexes[0]


# ==================== Identification ====================

def every_recorded_exchange(match):
    """(client, capture, index) for every recorded exchange the predicate accepts."""
    return [(client, name, index)
            for client in RECORDED
            for name in sorted(f[:-5] for f in os.listdir(os.path.join(CAPTURES, client))
                               if f.endswith(".json"))
            for index, exchange in enumerate(load(client, name)["exchanges"])
            if match(exchange)]


SHOP_REQUESTS = every_recorded_exchange(
    lambda e: "/api/get_game/" not in e["request"]["path"])
TRANSFER_REQUESTS = every_recorded_exchange(
    lambda e: "/api/get_game/" in e["request"]["path"])


def test_every_recorded_client_is_replayed():
    """A capture directory nobody replays is a test suite that silently isn't running."""
    assert set(os.listdir(CAPTURES)) <= set(CLIENTS)


@pytest.mark.parametrize("client,name,index", SHOP_REQUESTS)
def test_a_captured_shop_request_identifies_its_client(shop, client, name, index):
    exchange = load(client, name)["exchanges"][index]
    with shop.app.test_request_context(exchange["request"]["path"],
                                       headers=exchange["request"]["headers"]):
        from flask import request

        assert CLIENTS[client].cls.identify_client(request)


@pytest.mark.parametrize("client,name,index", TRANSFER_REQUESTS)
def test_a_captured_download_is_served_by_the_file_route(shop, client, name, index):
    """A transfer never reaches a client handler, and identification is not what stops it.

    Tinfoil downloads through a stack that sends no shop headers at all, but CyberFoil sends
    the lot and is identified all the same. What keeps both out of the shop handlers is the
    route: /api/get_game/<id> is concrete, so it wins over the shop's catch-all.
    """
    exchange = load(client, name)["exchanges"][index]
    with shop.app.test_request_context(exchange["request"]["path"],
                                       headers=exchange["request"]["headers"]):
        from flask import request

        assert request.endpoint == "serve_game"


# ==================== Browsing the shop ====================

@pytest.mark.parametrize("client", RECORDED)
def test_a_public_shop_serves_the_listing_without_credentials(shop, client, ):
    for index in browsing(client, "public-browse"):
        exchange, response = play(shop, client, "public-browse", index=index)
        assert response.status_code == 200
        assert len(response.data) == exchange["response"]["body"].get(
            "length", len(response.data))


@pytest.mark.parametrize("client", RECORDED)
@pytest.mark.parametrize("name", ["authenticated-browse", "admin-browse"])
def test_an_authorized_account_gets_the_listing(shop, client, name):
    """Shop access is all it takes: an admin sees no more of the shop than a shopper does."""
    for index in browsing(client, name):
        _, response = play(shop, client, name, index=index)
        assert response.status_code == 200
        assert not is_error(response)


REFUSALS = {
    "private-no-credentials": "Shop requires authentication.\nNo authentication provided.",
    "unknown-user": "Shop requires authentication.\nUnknown user ghost.",
    "wrong-password": "Shop requires authentication.\nIncorrect password for user shopper.",
    "no-shop-access": "User noshop does not have access to the shop.",
    "client-disabled": "Shop access from {client} is disabled.",
}


@pytest.mark.parametrize("client", RECORDED)
@pytest.mark.parametrize("name,message", sorted(REFUSALS.items()))
def test_a_refusal_says_why(shop, client, name, message):
    """The clients show the error body to the user, so the wording is the whole feature.

    It arrives as an unencrypted 200: a non-200 reads as the client's own network error.
    """
    for index in browsing(client, name):
        _, response = play(shop, client, name, index=index)
        assert response.status_code == 200
        assert error_of(response) == message.format(client=CLIENTS[client].cls.CLIENT_NAME)


@pytest.mark.parametrize("client", RECORDED)
def test_the_disabled_check_is_not_about_credentials(shop, client):
    """The same refused request, credentials and all, is served once the client is enabled."""
    _, response = play(shop, client, "client-disabled",
                       settings={"clients": {client: {"enabled": True}}})
    assert response.status_code == 200
    assert not is_error(response)


# ==================== Shop contents ====================

READABLE = every_recorded_exchange(lambda e: e["response"]["body"]["kind"] in ("json", "text"))


@pytest.mark.parametrize("client,name,index", READABLE)
def test_a_recorded_shop_response_replays_exactly(shop, client, name, index):
    """Every shop the clients could read - json, or Sphaira's page - comes back as recorded.

    This is the whole of what each client was shown: root listings, the directories Sphaira
    navigated into, the endpoints CyberFoil probes, and the refusals.
    """
    exchange, response = play(shop, client, name, index=index)
    assert response.status_code == exchange["response"]["status"]
    assert body_matches(exchange, response)


FILTERS = {
    "": lambda entry: True,
    "base/": lambda entry: entry.get("app_type") == APP_TYPE_BASE,
    "update/": lambda entry: entry.get("app_type") == APP_TYPE_UPD,
    "dlc/": lambda entry: entry.get("app_type") == APP_TYPE_DLC,
    "multi/": lambda entry: entry.get("multicontent"),
}


def browse(shop, client, filter=""):
    """Replay the client's readable shop request against one of the content filters."""
    name = CLIENTS[client].readable
    return play(shop, client, name, path=CLIENTS[client].root + filter)[1]


@pytest.mark.parametrize("client", RECORDED)
@pytest.mark.parametrize("filter", sorted(FILTERS))
def test_a_content_filter_lists_only_that_content(shop, client, filter):
    """Only /base was worth an operator's time; the rest is the same request, other path."""
    assert listed(browse(shop, client, filter)) == in_library(client, FILTERS[filter])


@pytest.mark.parametrize("client", RECORDED)
def test_an_unidentified_file_is_listed_only_without_a_filter(shop, client):
    hidden = set(in_library(client, unidentified))
    assert hidden <= set(listed(browse(shop, client)))
    assert not hidden & set(listed(browse(shop, client, "base/")))


def test_the_encrypted_listing_is_the_size_it_was(shop):
    """Tinfoil's container is nondeterministic - a random AES key - but its length is not."""
    for name in ("public-browse", "content-filter"):
        exchange, response = play(shop, "tinfoil", name)
        assert response.data[:8] == b"TINFOIL\xfd"
        assert len(response.data) == exchange["response"]["body"]["length"]


# ==================== Downloading ====================

@pytest.mark.parametrize("client", RECORDED)
def test_a_download_is_served_as_captured(shop, client):
    index = one_transfer(client)
    exchange, response = play(shop, client, "download", index=index)
    response.close()
    captured = dict(exchange["response"]["headers"])
    assert response.status_code == exchange["response"]["status"]
    assert response.headers["Content-Disposition"] == captured["Content-Disposition"]
    assert response.headers.get("Content-Range") == captured.get("Content-Range")
    assert count(shop, served_filename(exchange)) == 1


@pytest.mark.parametrize("client", RECORDED)
def test_a_range_past_the_end_of_the_file_is_not_a_download(shop, client):
    """CyberFoil probes a file at a fixed offset before taking it, which on a small file is
    past the end. The server refuses the range; nothing was transferred, so nothing counts.
    """
    index = one_transfer(client)
    exchange = load(client, "download")["exchanges"][index]
    headers = [[key, value] for key, value in headers_of(client, "download", index)
               if key.lower() != "range"] + [["Range", "bytes=61440-61443"]]
    _, response = play(shop, client, "download", index=index, headers=headers)
    response.close()
    assert response.status_code == 416
    assert count(shop, served_filename(exchange)) == 0


@pytest.mark.parametrize("client", RECORDED)
def test_taking_the_same_file_again_is_not_counted_twice(shop, client):
    """Two captures a minute apart: the second transfer is the same download, not a new one."""
    for name in ("download", "download-repeat"):
        _, response = play(shop, client, name, index=one_transfer(client, name))
        response.close()

    repeated = load(client, "download-repeat")["exchanges"][one_transfer(client,
                                                                        "download-repeat")]
    assert repeated["downloads"] == {}    # nor was it counted when it was recorded
    assert count(shop, served_filename(repeated)) == 1


@pytest.mark.parametrize("client", RECORDED)
def test_free_browsing_never_produces_an_error(shop, client):
    """The catch-all capture: whatever the client did unprompted, none of it broke."""
    capture = load(client, "free-browsing")
    if not capture["exchanges"]:
        pytest.skip(f"{client} sent nothing unprompted while it was recorded")
    for index in range(len(capture["exchanges"])):
        _, response = play(shop, client, "free-browsing", index=index)
        response.close()
        assert response.status_code < 400


@pytest.mark.parametrize("client", RECORDED)
def test_every_file_a_client_takes_is_counted_once(shop, client):
    """The throttle window is per file, so a client working through the shop counts them all.

    Tinfoil HEADs a file before taking it; the HEAD counts and the GET behind it does not.
    """
    capture = load(client, "free-browsing")
    indexes = transfers(client, "free-browsing")
    if not indexes:
        pytest.skip(f"{client} transferred no file while browsing freely")
    for index in indexes:
        _, response = play(shop, client, "free-browsing", index=index)
        response.close()

    for filename in {served_filename(capture["exchanges"][i]) for i in indexes}:
        assert count(shop, filename) == 1


def unauthenticated(client, name="download", method=None):
    """A captured transfer with its credentials stripped."""
    index = one_transfer(client, name, method)
    exchange = load(client, name)["exchanges"][index]
    return index, [[key, value] for key, value in exchange["request"]["headers"]
                   if key.lower() != "authorization"]


@pytest.mark.parametrize("client", FILE_ROUTE)
def test_a_download_needs_shop_access_too(shop, client):
    """The download route has no client to speak to, so it refuses in HTTP terms instead."""
    index, headers = unauthenticated(client)
    _, response = play(shop, client, "download", index=index, headers=headers)
    response.close()
    assert response.status_code == 401
    assert error_of(response) == "No authentication provided."

    headers.append(["Authorization", authorization("noshop", "noshoppass1")])
    _, response = play(shop, client, "download", index=index, headers=headers)
    response.close()
    assert response.status_code == 403
    assert error_of(response) == 'User "noshop" does not have access to the shop.'


def test_sphaira_serves_a_file_by_name_whatever_the_directory(shop):
    """Sphaira's directories are virtual, rebuilt from the library paths, and the file lookup
    behind them is by filename alone - so the directory in front of it is decoration.
    """
    index = one_transfer("sphaira", method="GET")
    exchange = load("sphaira", "download")["exchanges"][index]
    filename = served_filename(exchange)
    _, response = play(shop, "sphaira", "download", index=index,
                       path=f"/Nowhere In The Library/{filename}")
    response.close()
    assert response.headers["Content-Disposition"] == \
        dict(exchange["response"]["headers"])["Content-Disposition"]


def test_sphaira_says_when_a_file_is_not_found(shop):
    """The one refusal Sphaira answers a file request with, rather than the shop's."""
    index = one_transfer("sphaira", method="GET")
    _, response = play(shop, "sphaira", "download", index=index,
                       path="/Test Game/Not A Real File.nsp")
    response.close()
    assert error_of(response) == "File not found"


def test_sphaira_refuses_a_file_the_way_it_refuses_the_shop(shop):
    """Sphaira serves files from its own handler, so a refusal is a listing, not a status.

    It has no way to show an HTTP error, and the file it asked for must not be served.
    """
    index, headers = unauthenticated("sphaira", method="GET")
    _, response = play(shop, "sphaira", "download", index=index, headers=headers)
    response.close()
    assert error_of(response) == "Shop requires authentication.\nNo authentication provided."
    assert count(shop, served_filename(load("sphaira", "download")["exchanges"][index])) == 0


@pytest.mark.parametrize("client", RECORDED)
def test_a_public_shop_serves_downloads_to_anyone(shop, client):
    index, headers = unauthenticated(client)
    exchange, response = play(shop, client, "download", index=index, headers=headers,
                              settings={"public": True})
    response.close()
    assert response.status_code == exchange["response"]["status"]


@pytest.mark.parametrize("client", FILE_ROUTE)
def test_an_unknown_file_id_is_not_found(shop, client):
    """A stale url from an old listing is a 404, not a crash."""
    _, response = play(shop, client, "download", index=one_transfer(client),
                       path="/api/get_game/9999", settings={"public": True})
    response.close()
    assert response.status_code == 404
    assert error_of(response) == "No file with id 9999."


@pytest.mark.parametrize("client", FILE_ROUTE)
def test_a_download_under_a_content_filter_serves_no_file(shop, client):
    """`/base/api/get_game/<id>` is not a route, so nothing is served and nothing is counted.

    CyberFoil resolves the listing's `/api/get_game/<id>` against the shop url it was given,
    which is how a client configured with a filtered url asks for this and downloads nothing.
    Tinfoil resolves the same url against the host, and is unaffected.
    """
    index = one_transfer(client)
    exchange = load(client, "download")["exchanges"][index]
    _, response = play(shop, client, "download", index=index,
                       path=f"/base{exchange['request']['path']}")
    response.close()
    assert "Content-Disposition" not in response.headers
    assert count(shop, served_filename(exchange)) == 0


# ==================== Host verification over HTTPS ====================
#
# Hauth only matters over HTTPS, which the captures are not: the clients were pointed at a
# plain-HTTP shop on the LAN. The reverse proxy header is what the app actually keys off, so
# the secure paths are synthesized here from the same captured headers.

SECURE = {"X-Forwarded-Proto": "https"}


def secure_play(shop, client, name=None, host=None, hauth=None):
    """Replay a captured request as if it had arrived through an HTTPS reverse proxy."""
    name = name or CLIENTS[client].readable
    headers = headers_of(client, name) + list(SECURE.items())
    settings = {"host": host or "", "clients": {client: {}}}
    if CLIENTS[client].cls is TinfoilClient:
        settings["clients"][client]["encrypt"] = False
    if hauth is not None:
        settings["clients"][client]["hauth"] = hauth
    return play(shop, client, name, headers=headers, settings=settings)[1]


@pytest.mark.parametrize("client", RECORDED)
def test_https_without_a_configured_shop_host_is_served_unverified(shop, client):
    """Nothing to verify against: serve the shop, but don't pin the client to a referrer."""
    response = secure_play(shop, client)
    assert response.status_code == 200
    assert not is_error(response)
    assert "referrer" not in response.get_data(as_text=True)


@pytest.mark.parametrize("client", RECORDED)
def test_https_from_the_wrong_host_is_refused(shop, client):
    response = secure_play(shop, client, host="shop.example.net")
    host = sent(client, CLIENTS[client].readable, "Host")
    assert error_of(response) == f"Incorrect URL referrer detected: {host}."


@pytest.mark.parametrize("client", HAUTH)
def test_a_matching_hauth_pins_the_client_to_the_shop_host(shop, client):
    name = CLIENTS[client].readable
    host = sent(client, name, "Host")
    response = secure_play(shop, client, host=host, hauth={host: sent(client, name, "Hauth")})
    assert response.get_json()["referrer"] == f"https://{host}"


@pytest.mark.parametrize("client", HAUTH)
def test_a_wrong_hauth_is_refused(shop, client):
    host = sent(client, CLIENTS[client].readable, "Host")
    response = secure_play(shop, client, host=host, hauth={host: "0" * 32})
    assert error_of(response) == f"Incorrect Hauth for URL `{host}`."


@pytest.mark.parametrize("client", HAUTH)
def test_an_admin_registers_the_hauth_of_an_unknown_host(shop, client):
    """How a shop learns its Hauth: the first admin to connect over HTTPS records it."""
    host = sent(client, "admin-browse", "Host")
    response = secure_play(shop, client, "admin-browse", host=host, hauth={})
    assert response.status_code == 200
    assert get_settings()["shop"]["clients"][client]["hauth"] == {
        host: sent(client, "admin-browse", "Hauth")}


@pytest.mark.parametrize("client", HAUTH)
def test_a_non_admin_does_not_register_the_hauth(shop, client):
    """Until an admin has been through, verification stays off rather than locking users out."""
    response = secure_play(shop, client, host=sent(client, CLIENTS[client].readable, "Host"), hauth={})
    assert response.status_code == 200
    assert "referrer" not in response.get_json()
    assert get_settings()["shop"]["clients"][client]["hauth"] == {}
