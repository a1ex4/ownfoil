"""Tests for `get_lan_ip`, which reports the address the Setup page tells a Switch to connect
to. It borrows werkzeug's trick: connect a UDP socket to an arbitrary address in a private
range and read back the local address the kernel routed it through. The socket is stubbed out
here so the tests give the same result on any host, including CI containers."""

import socket

import pytest

import utils


class FakeSocket:
    """Stands in for socket.socket: records the connect target, returns a canned sockname."""

    def __init__(self, sockname, connect_error=None):
        self.sockname = sockname
        self.connect_error = connect_error
        self.connected_to = None

    def __call__(self, family, type):
        self.family = family
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def connect(self, address):
        if self.connect_error:
            raise self.connect_error
        self.connected_to = address

    def getsockname(self):
        return self.sockname


def _fake(monkeypatch, sockname, connect_error=None):
    fake = FakeSocket(sockname, connect_error)
    monkeypatch.setattr(utils.socket, "socket", fake)
    return fake


def test_returns_the_routed_local_address(monkeypatch):
    _fake(monkeypatch, ("192.168.1.42", 58162))
    assert utils.get_lan_ip() == "192.168.1.42"


def test_probes_a_private_ipv4_target_over_udp(monkeypatch):
    """Nothing is sent, but the target must be routable-looking and the socket connectionless."""
    fake = _fake(monkeypatch, ("192.168.1.42", 58162))

    utils.get_lan_ip()

    assert fake.family == socket.AF_INET
    assert fake.connected_to == ("10.253.155.219", 58162)


def test_ipv6_probes_the_ipv6_target(monkeypatch):
    fake = _fake(monkeypatch, ("2001:db8::5", 58162, 0, 0))

    assert utils.get_lan_ip(socket.AF_INET6) == "2001:db8::5"
    assert fake.family == socket.AF_INET6
    assert fake.connected_to == ("fd31:f903:5ab5:1::1", 58162)


def test_ipv6_zone_id_is_kept(monkeypatch):
    """A link-local address carries a zone id that must survive validation."""
    _fake(monkeypatch, ("fe80::1%eth0", 58162, 0, 2))
    assert utils.get_lan_ip(socket.AF_INET6) == "fe80::1%eth0"


@pytest.mark.parametrize("error", [
    OSError("network is unreachable"),
    socket.gaierror("no address"),      # subclass of OSError
])
def test_unreachable_network_gives_none(monkeypatch, error):
    """None lets the caller fall back to the request host instead of showing a dead address."""
    _fake(monkeypatch, ("192.168.1.42", 58162), connect_error=error)
    assert utils.get_lan_ip() is None


@pytest.mark.parametrize("ip", [
    "127.0.0.1",
    "::1",
    "0.0.0.0",
    "::",
])
def test_useless_addresses_give_none(monkeypatch, ip):
    """Loopback and unspecified results are no use to a Switch elsewhere on the LAN."""
    _fake(monkeypatch, (ip, 58162))
    assert utils.get_lan_ip() is None


def test_unparseable_address_gives_none(monkeypatch):
    _fake(monkeypatch, ("not-an-ip", 58162))
    assert utils.get_lan_ip() is None
