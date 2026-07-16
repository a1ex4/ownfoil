"""Behavioural tests for the file-compression pipeline.

nsz itself is stubbed: these assert the *orchestration contract* — what ends up on
disk and in the database — independently of how (solid/block) a file is compressed.
The invariant under test is that the source is only ever removed after a verified
replacement exists, and that the same Files row is carried across the extension change.
"""
import os
import types

import pytest

import file_compression as compression
import tasks
from db import (db, Files, Libraries, IgnoredEvent, TempFile,
                add_ignored_event, add_temp_file)
from nsz.NszDecompressor import VerificationException

from app import create_app


# --- Harness -----------------------------------------------------------------------------

DEFAULT_COMPRESSION = {
    "level": 18, "long_distance": False, "solid": "auto",
    "block_size_exponent": 20, "threads": 0,
}


def _settings(compress_files=True, organizer=False, delete_older=False):
    return {"library": {"management": {
        "compress_files": compress_files,
        "compression": dict(DEFAULT_COMPRESSION),
        "delete_older_updates": delete_older,
        "organizer": {"enabled": organizer, "remove_empty_folders": False},
    }}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """App + DB + a library dir on disk, with a helper to seed files."""
    app = create_app(f"sqlite:///{tmp_path/'test.db'}")
    lib_dir = tmp_path / "games"
    lib_dir.mkdir()
    ctx = app.app_context()
    ctx.push()
    db.create_all()
    library = Libraries(path=str(lib_dir))
    db.session.add(library)
    db.session.commit()

    monkeypatch.setattr(tasks, "get_settings", lambda: _settings())

    def seed(name, *, identified=True, compressed=False, content=b"RAWDATA", subdir=None):
        folder = lib_dir / subdir if subdir else lib_dir
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(content)
        f = Files(
            filepath=str(path), library_id=library.id, folder=str(folder),
            filename=name, extension=name.rsplit(".", 1)[-1], size=len(content),
            mtime=os.path.getmtime(path), identified=identified, compressed=compressed,
        )
        db.session.add(f)
        db.session.commit()
        return f

    yield types.SimpleNamespace(app=app, lib_dir=lib_dir, library=library, seed=seed,
                                monkeypatch=monkeypatch)
    ctx.pop()


def _stub_produce(out_bytes=b"COMPRESSEDPAYLOAD"):
    """Return a compress/decompress stub that writes a fake verified output file."""
    def produce(source, out_dir, *args):
        # Name matches nsz's own convention (stem + mapped extension).
        out_name = os.path.basename(str(
            compression.compressed_path(source) if str(source).endswith(("nsp", "xci"))
            else compression.decompressed_path(source)))
        out = os.path.join(str(out_dir), out_name)
        with open(out, "wb") as fh:
            fh.write(out_bytes)
        return out
    return produce


def _ignored(src, dest=None):
    q = IgnoredEvent.query.filter_by(src_path=src)
    if dest is not None:
        q = q.filter_by(dest_path=dest)
    return q.first() is not None


# --- Pure helpers ------------------------------------------------------------------------

@pytest.mark.parametrize("src,expected", [
    ("/games/Foo.nsp", "/games/Foo.nsz"),
    ("/games/Foo.xci", "/games/Foo.xcz"),
    ("/a/b/Game [v1].nsp", "/a/b/Game [v1].nsz"),
])
def test_compressed_path(src, expected):
    assert str(compression.compressed_path(src)) == expected


@pytest.mark.parametrize("src,expected", [
    ("/games/Foo.nsz", "/games/Foo.nsp"),
    ("/games/Foo.xcz", "/games/Foo.xci"),
])
def test_decompressed_path(src, expected):
    assert str(compression.decompressed_path(src)) == expected


# --- verification contract (#1: keep + full bit-identical verify) ------------------------

@pytest.mark.parametrize("name,fn", [("Game.nsp", "solidCompress"), ("Cart.xci", "blockCompress")])
def test_compress_to_keeps_and_verifies_against_source(tmp_path, monkeypatch, name, fn):
    source = tmp_path / name
    source.write_bytes(b"RAW")
    out_dir = tmp_path / "work"
    out_dir.mkdir()
    calls = {}

    def fake_compress(filePath, level, keep, *rest):
        calls["keep"] = keep
        out = out_dir / os.path.basename(str(compression.compressed_path(source)))
        out.write_bytes(b"C")
        return out

    def fake_verify(out, fixPadding, raiseExc, raisePfs0, originalFilePath):
        calls["verify_original"] = str(originalFilePath)

    monkeypatch.setattr(compression, "_ensure_keys", lambda: None)
    monkeypatch.setattr(compression.nsz, fn, fake_compress)
    monkeypatch.setattr(compression.nsz, "verify", fake_verify)

    compression.compress_to(source, out_dir, {"solid": "auto"})

    assert calls["keep"] is True                       # bit-identical restore possible
    assert calls["verify_original"] == str(source.resolve())  # compared against the source


# --- compress_file -----------------------------------------------------------------------

@pytest.mark.parametrize("name,comp_ext", [("Game.nsp", "nsz"), ("Cart.xci", "xcz")])
def test_compress_file_success(env, name, comp_ext):
    env.monkeypatch.setattr(compression, "compress_to", _stub_produce())
    f = env.seed(name, subdir="sub")
    source = f.filepath
    fid = f.id

    tasks.compress_file_task(file_id=fid)

    f = db.session.get(Files, fid)
    target = source.rsplit(".", 1)[0] + "." + comp_ext
    assert f.compressed is True
    assert f.extension == comp_ext
    assert f.filepath == target
    assert f.size == len(b"COMPRESSEDPAYLOAD")
    assert os.path.exists(target) and not os.path.exists(source)
    # Source deletion is suppressed; the in-progress mark is cleared at the end.
    assert _ignored(source, "")
    assert TempFile.query.count() == 0


def test_compress_file_verify_failure_keeps_source(env):
    def boom(*a, **k):
        raise VerificationException("hash mismatch")
    env.monkeypatch.setattr(compression, "compress_to", boom)
    f = env.seed("Game.nsp")
    source, fid = f.filepath, f.id

    with pytest.raises(VerificationException):
        tasks.compress_file_task(file_id=fid)

    f = db.session.get(Files, fid)
    assert f.compressed is False and f.filepath == source and f.extension == "nsp"
    assert os.path.exists(source)
    assert not os.path.exists(source.rsplit(".", 1)[0] + ".nsz")
    assert IgnoredEvent.query.count() == 0        # nothing queued before the failure
    assert TempFile.query.count() == 0  # in-progress mark cleared in finally


@pytest.mark.parametrize("name,identified,compressed", [
    ("Game.nsz", True, True),    # already compressed
    ("Game.kip", True, False),   # not a compressible extension
])
def test_compress_file_noop(env, name, identified, compressed):
    called = []
    env.monkeypatch.setattr(compression, "compress_to",
                            lambda *a, **k: called.append(1))
    f = env.seed(name, identified=identified, compressed=compressed)
    tasks.compress_file_task(file_id=f.id)
    assert called == []


def test_compress_file_missing_source_noop(env):
    env.monkeypatch.setattr(compression, "compress_to", lambda *a, **k: 1 / 0)
    f = env.seed("Game.nsp")
    os.remove(f.filepath)
    tasks.compress_file_task(file_id=f.id)  # returns cleanly, does not call nsz


def test_compress_file_defers_unorganized(env):
    """With the organizer on, an identified-but-unorganized file must not be compressed;
    organize_file re-triggers compression once the file is placed."""
    env.monkeypatch.setattr(tasks, "get_settings", lambda: _settings(organizer=True))
    env.monkeypatch.setattr(compression, "compress_to", lambda *a, **k: 1 / 0)
    f = env.seed("Game.nsp")           # organized defaults to False
    source, fid = f.filepath, f.id

    tasks.compress_file_task(file_id=fid)  # returns without invoking nsz

    f = db.session.get(Files, fid)
    assert f.compressed is False and f.filepath == source and f.extension == "nsp"


def test_compress_file_compresses_organized(env):
    """The same file, once organized, is compressed even with the organizer on."""
    env.monkeypatch.setattr(tasks, "get_settings", lambda: _settings(organizer=True))
    env.monkeypatch.setattr(compression, "compress_to", _stub_produce())
    f = env.seed("Game.nsp")
    f.organized = True
    db.session.commit()
    fid = f.id

    tasks.compress_file_task(file_id=fid)

    f = db.session.get(Files, fid)
    assert f.compressed is True and f.extension == "nsz"


# --- decompress_file ---------------------------------------------------------------------

def test_decompress_file_success(env):
    env.monkeypatch.setattr(compression, "decompress_to", _stub_produce(b"RAWAGAIN"))
    f = env.seed("Game.nsz", compressed=True)
    source, fid = f.filepath, f.id

    tasks.decompress_file_task(file_id=fid)

    f = db.session.get(Files, fid)
    target = source.rsplit(".", 1)[0] + ".nsp"
    assert f.compressed is False and f.extension == "nsp" and f.filepath == target
    assert os.path.exists(target) and not os.path.exists(source)


# --- cleanup hook & startup purge --------------------------------------------------------

def test_compression_cleanup_removes_partial_and_mark(env):
    f = env.seed("Game.nsp")
    target = str(compression.compressed_path(f.filepath))
    add_temp_file(target)
    add_ignored_event(f.filepath, "")
    open(target, "wb").close()  # a partial, uncommitted output

    tasks._compression_cleanup(file_id=f.id)

    assert not os.path.exists(target)                 # partial removed
    assert TempFile.query.count() == 0   # mark cleared
    assert IgnoredEvent.query.count() == 0            # source-deletion event popped


def test_startup_purge_keeps_committed_output(env):
    # An interrupted task that had already flipped the row: output is committed, keep it.
    f = env.seed("Game.nsz", compressed=True)         # row already points at the output
    add_temp_file(f.filepath)
    # And a genuinely partial one that no row points at.
    partial = str(env.lib_dir / "Half.nsz")
    open(partial, "wb").close()
    add_temp_file(partial)

    from db import purge_temp_files
    purge_temp_files()

    assert os.path.exists(f.filepath)      # committed output kept
    assert not os.path.exists(partial)     # orphan partial removed
    assert TempFile.query.count() == 0


def test_scan_and_watcher_skip_in_progress(env):
    """Scanner and watcher both ignore a file while it is marked in-progress."""
    target = str(env.lib_dir / "Being.nsz")
    open(target, "wb").close()
    add_temp_file(target)

    from db import get_temp_file_paths, is_temp_file
    # Scanner: in-progress path is excluded from the scan's new-file set.
    _, files = __import__("titles").getDirsAndFiles(str(env.lib_dir))
    skip = get_temp_file_paths()
    assert target in files and target not in [f for f in files if f not in skip]
    # Watcher gate.
    assert is_temp_file(target) is True


# --- compress_library fan-out ------------------------------------------------------------

def test_compress_library_selects_eligible(env):
    enqueued = []
    env.monkeypatch.setattr(tasks, "enqueue_or_child",
                            lambda name, data=None: enqueued.append((name, data["file_id"])))
    env.monkeypatch.setattr(tasks, "set_waiting_for_children", lambda: None)

    ok1 = env.seed("A.nsp")
    ok2 = env.seed("B.xci")
    ok3 = env.seed("D.nsp", identified=False)  # unidentified files are compressed too
    env.seed("C.nsz", compressed=True)         # already compressed: excluded

    tasks.compress_library_task()

    assert sorted(fid for _, fid in enqueued) == sorted([ok1.id, ok2.id, ok3.id])
    assert all(name == "compress_file" for name, _ in enqueued)


def test_compress_library_excludes_awaiting_organization(env):
    """With the organizer on, files still awaiting organization are skipped by the sweep;
    organized files and unorganizable (unidentified) files stay eligible."""
    enqueued = []
    env.monkeypatch.setattr(tasks, "get_settings", lambda: _settings(organizer=True))
    env.monkeypatch.setattr(tasks, "enqueue_or_child",
                            lambda name, data=None: enqueued.append(data["file_id"]))
    env.monkeypatch.setattr(tasks, "set_waiting_for_children", lambda: None)

    organized = env.seed("A.nsp")
    organized.organized = True
    unidentified = env.seed("D.nsp", identified=False)  # organizer can't place it
    env.seed("B.xci")  # identified, not organized: deferred
    db.session.commit()

    tasks.compress_library_task()

    assert sorted(enqueued) == sorted([organized.id, unidentified.id])


def test_compress_library_disabled_noop(env):
    env.monkeypatch.setattr(tasks, "get_settings", lambda: _settings(compress_files=False))
    env.monkeypatch.setattr(tasks, "enqueue_or_child",
                            lambda *a, **k: pytest.fail("should not enqueue"))
    env.seed("A.nsp")
    tasks.compress_library_task()


# --- pipeline wiring ---------------------------------------------------------------------

@pytest.mark.parametrize("enabled,expected", [(True, True), (False, False)])
def test_organize_done_chains_compression(env, enabled, expected):
    enqueued = []
    env.monkeypatch.setattr(tasks, "get_settings",
                            lambda: _settings(compress_files=enabled))
    env.monkeypatch.setattr(tasks, "enqueue_task",
                            lambda name, data=None: enqueued.append(name))
    tasks._organize_library_done()
    assert ("compress_library" in enqueued) is expected
