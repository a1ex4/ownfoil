"""Tests for `load_secret_key`: each instance signs sessions with its own persisted random key."""

import os
import stat

import pytest

from app import app, load_secret_key

LEGACY_KEY = '8accb915665f11dfa15c2db1a4e8026905f57716'


def test_generated_on_first_start(tmp_path):
    path = tmp_path / 'secret_key'
    key = load_secret_key(str(path))
    assert len(key) == 64
    assert path.read_text() == key
    if os.name == 'posix':
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_reused_across_restarts(tmp_path):
    path = str(tmp_path / 'secret_key')
    assert load_secret_key(path) == load_secret_key(path)


@pytest.mark.parametrize('content', ['', 'abc123', 'deadbeef' * 4])
def test_invalid_file_regenerated(tmp_path, content):
    path = tmp_path / 'secret_key'
    path.write_text(content)
    key = load_secret_key(str(path))
    assert len(key) == 64
    assert path.read_text() == key


def test_app_does_not_use_legacy_key():
    assert app.config['SECRET_KEY'] != LEGACY_KEY
    assert len(app.config['SECRET_KEY']) == 64
