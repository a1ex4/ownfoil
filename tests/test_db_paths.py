"""Tests for the LIKE pattern behind `delete_files_under_dir`. Stored filepaths carry the
host's separator, so the pattern has to as well — a hardcoded '/' matches nothing on Windows,
silently turning a folder removal into a no-op."""

import os

import pytest

from db import _dir_prefix_like


def test_prefix_uses_the_host_separator():
    lib = os.path.join("lib", "Zelda")
    pattern = _dir_prefix_like(lib)

    assert pattern.endswith(os.sep.replace("\\", "\\\\") + "%")
    # A file under the directory must match the pattern once LIKE escaping is undone.
    assert pattern.replace("\\\\", "\\")[:-1] == lib + os.sep


def test_trailing_separator_is_not_doubled():
    lib = os.path.join("lib", "Zelda")
    assert _dir_prefix_like(lib + os.sep) == _dir_prefix_like(lib)
    assert _dir_prefix_like(lib + "/") == _dir_prefix_like(lib)


@pytest.mark.parametrize("wildcard", ["100%_Orange", "under_score"])
def test_like_wildcards_are_escaped(wildcard):
    """% and _ in a folder name are LIKE wildcards and must not widen the match."""
    pattern = _dir_prefix_like(os.path.join("lib", wildcard))
    assert "\\%" in pattern or "\\_" in pattern
