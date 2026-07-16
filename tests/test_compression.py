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
from db import db, Files, Libraries, IgnoredEvent, add_ignored_event
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
    # Watcher suppression for both the source deletion and the target's appearance.
    assert _ignored(source, "") and _ignored(target, "")
    # Working dir cleaned up.
    assert not os.path.isdir(os.path.join(str(env.lib_dir), tasks.COMPRESS_TMP_DIRNAME, str(fid)))


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
    assert IgnoredEvent.query.count() == 0  # nothing queued before the failure
    assert not os.path.isdir(os.path.join(str(env.lib_dir), tasks.COMPRESS_TMP_DIRNAME, str(fid)))


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


# --- cleanup hook ------------------------------------------------------------------------

def test_compression_cleanup_removes_workdir_and_events(env):
    f = env.seed("Game.nsp")
    tmp = os.path.join(str(env.lib_dir), tasks.COMPRESS_TMP_DIRNAME, str(f.id))
    os.makedirs(tmp)
    add_ignored_event(f.filepath, "")
    add_ignored_event(str(compression.compressed_path(f.filepath)), "")

    tasks._compression_cleanup(file_id=f.id)

    assert not os.path.isdir(tmp)
    assert IgnoredEvent.query.count() == 0


# --- compress_library fan-out ------------------------------------------------------------

def test_compress_library_selects_eligible(env):
    enqueued = []
    env.monkeypatch.setattr(tasks, "enqueue_or_child",
                            lambda name, data=None: enqueued.append((name, data["file_id"])))
    env.monkeypatch.setattr(tasks, "set_waiting_for_children", lambda: None)

    ok1 = env.seed("A.nsp")
    ok2 = env.seed("B.xci")
    env.seed("C.nsz", compressed=True)       # already compressed
    env.seed("D.nsp", identified=False)      # unidentified

    tasks.compress_library_task()

    assert sorted(fid for _, fid in enqueued) == sorted([ok1.id, ok2.id])
    assert all(name == "compress_file" for name, _ in enqueued)


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
