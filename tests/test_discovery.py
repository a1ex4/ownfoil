"""LAN discovery: the broadcast a console sends looking for shops, and what we answer with.

The request and the reply are the client's contract, so these tests pin the wire format rather
than the implementation: what a datagram must contain to be answered, and what comes back.
"""
import json
import socket

import pytest
import yaml

import discovery
import settings as settings_mod
from constants import APP_VERSION, DISCOVERY_MAGIC, DISCOVERY_REQUEST, HTTP_PORT
from settings import get_server_uid, set_services_settings, set_shop_settings

# label, datagram sent to the responder, whether it deserves a reply
REQUESTS = [
    ("the request itself", DISCOVERY_REQUEST, True),
    ("terminated request", DISCOVERY_REQUEST + b"\n", True),
    ("truncated request", DISCOVERY_REQUEST[:-3], False),
    ("wrong case", DISCOVERY_REQUEST.lower(), False),
    ("empty datagram", b"", False),
    ("another protocol", b"M-SEARCH * HTTP/1.1", False),
]


@pytest.fixture
def settings_dir(tmp_path, monkeypatch):
    """Settings of this test's own, so one test's shop cannot reach the next."""
    monkeypatch.setattr(settings_mod, "CONFIG_FILE", str(tmp_path / "settings.yaml"))
    monkeypatch.setattr(settings_mod, "KEYS_FILE", str(tmp_path / "keys.txt"))
    monkeypatch.setattr(settings_mod, "_cached_settings", None)
    return tmp_path


@pytest.fixture
def responder(settings_dir):
    """A responder on an ephemeral port: the real one is taken by the running Ownfoil."""
    responder = discovery.DiscoveryResponder(port=0)
    assert responder.bind()
    responder.start()
    yield responder
    responder.stop()


def ask(responder, request=DISCOVERY_REQUEST, timeout=2):
    """Send one datagram to the responder, returning its reply or None if it stayed silent."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(request, ("127.0.0.1", responder.port))
        try:
            reply, _ = sock.recvfrom(4096)
        except socket.timeout:
            return None
    return json.loads(reply)


@pytest.mark.parametrize("request_bytes, answered",
                         [case[1:] for case in REQUESTS], ids=[case[0] for case in REQUESTS])
def test_only_the_discovery_request_is_answered(responder, request_bytes, answered):
    """Nothing else on the network gets a reply - the port hears every broadcast there is."""
    reply = ask(responder, request_bytes, timeout=2 if answered else 0.3)

    assert (reply is not None) == answered


def test_reply_describes_the_shop(responder, monkeypatch):
    monkeypatch.setattr(settings_mod, "get_lan_ip", lambda: "192.168.1.42")
    set_shop_settings({"name": "My Shop", "host": "shop.example.com", "public": True})

    assert ask(responder) == {
        "magic": DISCOVERY_MAGIC,
        "uid": get_server_uid(),
        "name": "My Shop",
        "version": APP_VERSION,
        "local": f"192.168.1.42:{HTTP_PORT}",
        "remote": "shop.example.com",
        "public": True,
    }


def test_local_address_is_empty_when_the_lan_ip_is_unknown(responder, monkeypatch):
    """A host with no routable address still answers: the console can fall back on the remote one."""
    monkeypatch.setattr(settings_mod, "get_lan_ip", lambda: None)

    assert ask(responder)["local"] == ""


def test_uid_identifies_the_server_across_requests(responder):
    """Clients key saved servers on the uid, so two answers from one shop must carry the same one."""
    first, second = ask(responder)["uid"], ask(responder)["uid"]

    assert first and first == second


def test_the_uid_is_minted_with_the_settings_file(settings_dir):
    """A first run writes a real identity: the empty placeholder never reaches a client."""
    uid = get_server_uid()
    written = yaml.safe_load((settings_dir / "settings.yaml").read_text())['server']['uid']
    # Drop the cache so the next read comes off disk, as another process would see it.
    settings_mod._cached_settings = None

    assert uid and written == uid == get_server_uid()


def test_the_service_can_be_turned_off(settings_dir, monkeypatch):
    """Disabled, nothing listens at all - the port is left to whoever else wants it."""
    monkeypatch.setattr(discovery, "DISCOVERY_PORT", 0)
    set_services_settings({"discovery": {"enabled": False}})
    discovery.reconcile()
    assert discovery._responder is None

    set_services_settings({"discovery": {"enabled": True}})
    discovery.reconcile()
    try:
        assert ask(discovery._responder)["magic"] == DISCOVERY_MAGIC
    finally:
        discovery.stop()

    assert discovery._responder is None
