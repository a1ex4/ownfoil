"""Which names on disk count as library files - the scanner and the watcher share one answer."""
import pytest

import titles
from utils import is_library_file


@pytest.mark.parametrize("name,expected", [
    ("Game.nsp", True),
    ("Game.NSP", False),            # extensions are matched as written, as before
    ("Game [0100000000010000][v0].xcz", True),
    ("readme.txt", False),
    ("Game.nsp.part", False),
    # macOS AppleDouble twins carry the extension but hold Finder metadata (issue #357).
    ("._Game.nsp", False),
    ("._Game.xci", False),
    (".Game.nsp", False),
    ("/library/Title/._Game.nsp", False),
    ("/library/Title/Game.nsp", True),
])
def test_is_library_file(name, expected):
    assert is_library_file(name) is expected


def test_scan_skips_dotfiles_but_walks_dotted_folders(tmp_path):
    (tmp_path / "Game.nsp").write_bytes(b"x")
    (tmp_path / "._Game.nsp").write_bytes(b"\x00\x05\x16\x07")
    (tmp_path / ".DS_Store").write_bytes(b"x")
    sub = tmp_path / "Title [0100000000010000]"
    sub.mkdir()
    (sub / "Update.nsz").write_bytes(b"x")
    (sub / "._Update.nsz").write_bytes(b"\x00\x05\x16\x07")

    dirs, files = titles.get_dirs_and_files(str(tmp_path))

    assert dirs == [str(sub)]
    assert sorted(files) == [str(tmp_path / "Game.nsp"), str(sub / "Update.nsz")]
