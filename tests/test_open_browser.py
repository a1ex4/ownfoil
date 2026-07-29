"""Tests for opening the Web UI in a browser on startup, which only the Windows entrypoint does."""

import inspect

import pytest

import local
from ownfoil.cli import _should_open_browser


def test_opens_by_default():
    assert _should_open_browser(False, {}) is True


def test_flag_disables_it():
    assert _should_open_browser(True, {}) is False


def test_flag_wins_over_the_environment():
    assert _should_open_browser(True, {'OWNFOIL_NO_BROWSER': '0'}) is False


@pytest.mark.parametrize('value', ['1', 'true', 'TRUE', 'Yes', ' yes '])
def test_truthy_env_var_disables_it(value):
    assert _should_open_browser(False, {'OWNFOIL_NO_BROWSER': value}) is False


@pytest.mark.parametrize('value', ['', '0', 'false', 'no'])
def test_other_env_values_leave_it_on(value):
    assert _should_open_browser(False, {'OWNFOIL_NO_BROWSER': value}) is True


def test_main_does_not_open_a_browser_unless_asked():
    """Running app/local.py directly, as in development, must stay silent."""
    assert inspect.signature(local.main).parameters['open_browser'].default is False


def test_open_ui_uses_the_loopback_address(monkeypatch):
    """localhost can resolve to ::1 first on Windows, but the server binds IPv4 only."""
    opened = []
    monkeypatch.setattr(local.webbrowser, 'open', lambda url: opened.append(url) or True)

    local._open_ui(local.LOCAL_URL)

    assert opened == ['http://127.0.0.1:8465']


def test_open_ui_survives_a_raising_browser(monkeypatch, caplog):
    def boom(url):
        raise RuntimeError('no display')

    monkeypatch.setattr(local.webbrowser, 'open', boom)

    local._open_ui('http://127.0.0.1:8465')

    assert 'http://127.0.0.1:8465' in caplog.text


def test_open_ui_warns_when_no_browser_is_found(monkeypatch, caplog):
    """webbrowser.open returns False rather than raising when it finds nothing to launch."""
    monkeypatch.setattr(local.webbrowser, 'open', lambda url: False)

    local._open_ui('http://127.0.0.1:8465')

    assert 'http://127.0.0.1:8465' in caplog.text
