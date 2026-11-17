"""Tests for the LIKE pattern behind `delete_files_under_dir`. Stored filepaths carry the
host's separator, so the pattern has to as well — a hardcoded '/' matches nothing on Windows,
silently turning a folder removal into a no-op."""

import os

import pytest

from db import _dir_prefix, _dir_prefix_like, delete_files_under_dir
from app import create_app
import db as db_mod
from db import Files, Libraries, db, init_db


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


def test_dir_prefix_uses_host_separator_and_no_doubling():
    lib = os.path.join("lib", "Zelda")
    assert _dir_prefix(lib) == lib + os.sep
    assert _dir_prefix(lib + os.sep) == lib + os.sep
    assert _dir_prefix(lib + "/") == lib + os.sep


@pytest.mark.parametrize("wildcard", ["100%_Orange", "under_score"])
def test_like_wildcards_are_escaped(wildcard):
    """% and _ in a folder name are LIKE wildcards and must not widen the match."""
    pattern = _dir_prefix_like(os.path.join("lib", wildcard))
    assert "\\%" in pattern or "\\_" in pattern


@pytest.fixture
def app_ctx(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setattr(db_mod, "DB_FILE", str(config / "ownfoil.db"))
    monkeypatch.setattr(db_mod, "TITLES_DB_FILE", str(config / "titles.db"))

    app = create_app(f"sqlite:///{config / 'ownfoil.db'}")
    init_db(app)
    with app.app_context():
        yield app


def test_delete_files_under_dir_case_sensitive(app_ctx):
    """Folder deletion must not prune files under a sibling folder that differs only by case.

    In SQLite, LIKE is case-insensitive for ASCII, so deleting 'Some Game' would otherwise
    match and delete files in 'SOME GAME'."""
    lib = Libraries(path="/games")
    db.session.add(lib)
    db.session.flush()

    sep = os.sep
    old_game = f"{sep}games{sep}Some Game{sep}game.nsp"
    new_game = f"{sep}games{sep}SOME GAME{sep}game.nsp"
    other_game = f"{sep}games{sep}Other Game{sep}game.nsp"

    db.session.add(Files(library_id=lib.id, filepath=old_game, filename="game.nsp"))
    db.session.add(Files(library_id=lib.id, filepath=new_game, filename="game.nsp"))
    db.session.add(Files(library_id=lib.id, filepath=other_game, filename="game.nsp"))
    db.session.commit()

    # Deleting old folder 'Some Game' must remove old_game but keep new_game ('SOME GAME')
    deleted_count = delete_files_under_dir(f"{sep}games{sep}Some Game")
    assert deleted_count == 1

    remaining_paths = {f.filepath for f in Files.query.all()}
    assert old_game not in remaining_paths
    assert new_game in remaining_paths
    assert other_game in remaining_paths


def test_delete_files_under_dir_wildcard_folder(app_ctx):
    """Folder deletion with % or _ in the name must only match the exact folder."""
    lib = Libraries(path="/games")
    db.session.add(lib)
    db.session.flush()

    sep = os.sep
    target_file = f"{sep}games{sep}100%_Orange{sep}game.nsp"
    similar_file = f"{sep}games{sep}1000_Orange{sep}game.nsp"
    case_diff_file = f"{sep}games{sep}100%_ORANGE{sep}game.nsp"

    db.session.add(Files(library_id=lib.id, filepath=target_file, filename="game.nsp"))
    db.session.add(Files(library_id=lib.id, filepath=similar_file, filename="game.nsp"))
    db.session.add(Files(library_id=lib.id, filepath=case_diff_file, filename="game.nsp"))
    db.session.commit()

    deleted_count = delete_files_under_dir(f"{sep}games{sep}100%_Orange")
    assert deleted_count == 1

    remaining_paths = {f.filepath for f in Files.query.all()}
    assert target_file not in remaining_paths
    assert similar_file in remaining_paths
    assert case_diff_file in remaining_paths
