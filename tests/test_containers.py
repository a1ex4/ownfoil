"""Behavioural tests for the shared nsz container opener.

No sample game files exist in the repo, so nsz's open() is stubbed: what matters here is
the dispatch (which class a suffix maps to) and the lifecycle (always closed), not parsing.
"""
import pytest

import containers
from nsz.Fs import Nsp, Xci


@pytest.fixture
def opened(monkeypatch):
    """Record open/flush/close on whichever container the factory returns."""
    calls = []
    for cls in (Nsp.Nsp, Xci.Xci):
        monkeypatch.setattr(cls, "open", lambda self, *a, **k: calls.append(("open", a, k)))
        monkeypatch.setattr(cls, "flush", lambda self: calls.append(("flush",)))
        monkeypatch.setattr(cls, "close", lambda self: calls.append(("close",)))
    return calls


@pytest.mark.parametrize("name, expected", [
    ("Game.nsp", Nsp.Nsp), ("Game.nsz", Nsp.Nsp),
    ("Game.xci", Xci.Xci), ("Game.xcz", Xci.Xci),
    # nsz's factory matches the suffix case-sensitively and falls through to a generic
    # File; the scanner never admits these today, so this is a guard, not a live fix.
    ("Game.NSP", Nsp.Nsp), ("Game.XcZ", Xci.Xci),
])
def test_extension_dispatch_is_case_insensitive(opened, tmp_path, name, expected):
    with containers.open_container(str(tmp_path / name)) as container:
        assert type(container) is expected


def test_unsupported_extension_is_refused(opened, tmp_path):
    with pytest.raises(ValueError, match="Unsupported container extension"):
        with containers.open_container(str(tmp_path / "notes.txt")):
            pass
    assert opened == []          # nothing was opened, so nothing needs closing


def test_meta_only_is_passed_through(opened, tmp_path):
    with containers.open_container(str(tmp_path / "Game.nsp"), meta_only=True):
        pass
    assert opened[0][2]["meta_only"] is True


def test_container_is_closed_when_the_body_raises(opened, tmp_path):
    with pytest.raises(RuntimeError):
        with containers.open_container(str(tmp_path / "Game.nsp")):
            raise RuntimeError("boom")
    # sliced: nsz's File.__del__ closes again on collection, which is not what is under test
    assert [c[0] for c in opened][:3] == ["open", "flush", "close"]


def test_container_is_closed_when_open_raises(monkeypatch, tmp_path):
    """The one real gap the context manager closes: a container that fails partway through
    open() has a file handle to release."""
    calls = []
    monkeypatch.setattr(Nsp.Nsp, "open", lambda self, *a, **k: (_ for _ in ()).throw(OSError("bad header")))
    monkeypatch.setattr(Nsp.Nsp, "flush", lambda self: calls.append("flush"))
    monkeypatch.setattr(Nsp.Nsp, "close", lambda self: calls.append("close"))
    with pytest.raises(OSError):
        with containers.open_container(str(tmp_path / "Game.nsp")):
            pass
    assert calls[:2] == ["flush", "close"]
