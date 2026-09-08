"""Behavioural tests for the file-compression pipeline.

nsz itself is stubbed: these assert the *orchestration contract* — what ends up on
disk and in the database — independently of how (solid/block) a file is compressed.
The invariant under test is that the source is only ever removed after a verified
replacement exists, and that the same Files row is carried across the extension change.
"""
import datetime
import os
import time
import types
from pathlib import Path

import pytest

from containers import compression
import tasks
from db import (db, Files, Libraries, IgnoredEvent, TempFile, Task,
                add_ignored_event, add_temp_file)
from nsz.Decompressor import VerificationException

from app import create_app


# --- Harness -----------------------------------------------------------------------------

DEFAULT_COMPRESSION = {
    "enabled": True, "level": 18, "long_distance": False, "mode": "auto",
    "block_size_exponent": 20, "threads": 0,
}


def _settings(compress_files=True, organizer=False, delete_older=False, group_limits=None,
              verify=False, depth="signature"):
    s = {"library": {"management": {
        "compression": {**DEFAULT_COMPRESSION, "enabled": compress_files},
        "verification": {"enabled": verify, "depth": depth},
        "delete_older_updates": delete_older,
        "organizer": {"enabled": organizer, "remove_empty_folders": False},
    }}}
    if group_limits is not None:
        s["worker"] = {"group_limits": group_limits}
    return s


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
    def produce(source, out_dir, *args, **kwargs):
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
    # Compared as Paths, not strings: the result carries the host's separator.
    assert compression.compressed_path(src) == Path(expected)


@pytest.mark.parametrize("src,expected", [
    ("/games/Foo.nsz", "/games/Foo.nsp"),
    ("/games/Foo.xcz", "/games/Foo.xci"),
])
def test_decompressed_path(src, expected):
    assert compression.decompressed_path(src) == Path(expected)


# --- progress reporting + bar suppression ------------------------------------------------

def test_progress_bars_and_chatter_suppressed():
    """nsz's terminal bars are off (minimalOutput no-bar path) and info printing is silenced."""
    from nsz.nut import Print
    assert Print.minimalOutput is True
    assert Print.enableInfo is False


def test_with_progress_none_runs_inline():
    """No callback: run the phase directly with statusReportInfo=None, no poller thread."""
    seen = []
    result = compression._with_progress(lambda sri: seen.append(sri) or "out", None, 0, 50)
    assert result == "out" and seen == [None]


def test_with_progress_maps_status_report_to_span(monkeypatch):
    """The poller maps report[key]=[cur,_,total,phase] onto base..base+span and forces the end."""
    monkeypatch.setattr(compression, "POLL_INTERVAL", 0.01)
    seen = []

    def run(sri):
        report, key = sri
        for cur in (0, 50, 100):
            report[key] = [cur, 0, 100, "Verifying"]
            time.sleep(0.03)
        return "out"

    result = compression._with_progress(run, seen.append, 50, 50)
    assert result == "out"
    assert seen and all(50 <= p <= 100 for p in seen)  # mapped into the verify half
    assert seen[-1] == 100                              # phase end forced on success
    assert seen == sorted(seen)                         # monotonic


# --- verification contract: keep + NCA round-trip against source -------------------------

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

    def fake_verify(src, out, progress):
        calls["verify_source"] = str(src)
        calls["verify_out"] = str(out)

    monkeypatch.setattr(compression, "_ensure_keys", lambda: None)
    monkeypatch.setattr(compression.nsz, fn, fake_compress)
    monkeypatch.setattr(compression, "_verify_roundtrip", fake_verify)

    compression.compress_to(source, out_dir, {"mode": "auto"})

    assert calls["keep"] is True                          # bit-identical restore possible
    assert calls["verify_source"] == str(source.resolve())  # round-trip verified against the source


@pytest.mark.parametrize("name,fn", [("Game.nsp", "solidCompress"), ("Cart.xci", "blockCompress")])
def test_compress_to_removes_output_when_verification_fails(tmp_path, monkeypatch, name, fn):
    """A verification failure must leave no compressed output on disk — the produced (but
    unverified) file is unlinked before the exception propagates."""
    source = tmp_path / name
    source.write_bytes(b"RAW")
    out_dir = tmp_path / "work"
    out_dir.mkdir()
    out = out_dir / os.path.basename(str(compression.compressed_path(source)))

    def fake_compress(filePath, level, keep, *rest):
        out.write_bytes(b"C")
        return out

    def boom(src, o, progress):
        raise VerificationException("hash mismatch")

    monkeypatch.setattr(compression, "_ensure_keys", lambda: None)
    monkeypatch.setattr(compression.nsz, fn, fake_compress)
    monkeypatch.setattr(compression, "_verify_roundtrip", boom)

    with pytest.raises(VerificationException):
        compression.compress_to(source, out_dir, {"mode": "auto"})
    assert not out.exists()   # unverified output removed, not left in the library dir


def test_verify_roundtrip_passes_when_every_nca_matches(monkeypatch):
    monkeypatch.setattr(compression, "_nca_content_hashes",
                        lambda p, sri=None: {"AAAA": "h1", "BBBB.cnmt": "h2"})
    compression._verify_roundtrip(Path("/x/Game.nsp"), Path("/x/Game.nsz"), None)  # no raise


@pytest.mark.parametrize("got", [
    {"AAAA": "WRONG", "BBBB.cnmt": "h2"},             # corrupted NCA
    {"AAAA": "h1"},                                   # missing NCA
    {"AAAA": "h1", "BBBB.cnmt": "h2", "X": "h3"},     # unexpected extra NCA
])
def test_verify_roundtrip_raises_on_any_divergence(monkeypatch, got):
    src = {"AAAA": "h1", "BBBB.cnmt": "h2"}
    monkeypatch.setattr(compression, "_nca_content_hashes",
                        lambda p, sri=None: src if p.suffix in (".nsp", ".xci") else got)
    with pytest.raises(VerificationException):
        compression._verify_roundtrip(Path("/x/Game.nsp"), Path("/x/Game.nsz"), None)


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


def test_compress_file_skips_when_target_row_exists(env):
    """A duplicate whose compressed target is already a library file is skipped, not compressed
    into a UNIQUE(filepath) collision at finalize."""
    called = []
    env.monkeypatch.setattr(compression, "compress_to", lambda *a, **k: called.append(1))
    src = env.seed("Game.nsp")
    other = env.seed("Game.nsz", compressed=True)  # already occupies Game.nsz (the target path)
    src_id, other_id = src.id, other.id

    tasks.compress_file_task(file_id=src_id)

    assert called == []                                        # compression skipped
    src = db.session.get(Files, src_id)
    assert src.compressed is False and src.extension == "nsp"  # source row untouched
    assert db.session.get(Files, other_id) is not None         # existing row intact


@pytest.mark.parametrize("organized", [True, False])
def test_compress_file_ignores_organization_state(env, organized):
    """compress_file no longer defers to the organizer: ordering is the driver's job, and a
    deferring guard would make the compressFile mutation a silent no-op on a placed file."""
    env.monkeypatch.setattr(tasks, "get_settings", lambda: _settings(organizer=True))
    env.monkeypatch.setattr(compression, "compress_to", _stub_produce())
    f = env.seed("Game.nsp")
    f.organized = organized
    db.session.commit()
    fid = f.id

    tasks.compress_file_task(file_id=fid)

    f = db.session.get(Files, fid)
    assert f.compressed is True and f.extension == "nsz"


# --- organizer/compression collision (per-file lock) -------------------------------------

def test_compress_file_defers_while_source_is_busy(env):
    """A conversion must not start while another task holds the file (e.g. the organizer
    mid-move): it returns without invoking nsz and leaves the row untouched."""
    from db import claim_temp_file
    env.monkeypatch.setattr(compression, "compress_to", lambda *a, **k: 1 / 0)
    f = env.seed("Game.nsp")
    source, fid = f.filepath, f.id
    assert claim_temp_file(source)   # another task holds the file

    tasks.compress_file_task(file_id=fid)   # returns without invoking nsz

    f = db.session.get(Files, fid)
    assert f.compressed is False and f.filepath == source and f.extension == "nsp"


def test_organize_stage_defers_while_source_is_being_converted(env):
    """The organizer must not move a file a conversion holds: it neither relocates the file
    nor enqueues anything while the claim is held (the next sweep re-drives it)."""
    from db import claim_temp_file
    moved, enqueued = [], []
    env.monkeypatch.setattr(tasks, "get_settings",
                            lambda: _settings(organizer=True, compress_files=False))
    env.monkeypatch.setattr(tasks, "organize_file", lambda *a, **k: moved.append(1) or True)
    env.monkeypatch.setattr(tasks, "enqueue_task", lambda name, data=None, **k: enqueued.append(name))
    f = env.seed("Game.nsp")
    fid = f.id
    assert claim_temp_file(f.filepath)   # a conversion holds the source

    tasks.process_file_task(file_id=fid)

    assert moved == [] and enqueued == []          # file left in place, nothing scheduled
    assert db.session.get(Files, fid).organized is False


def test_compress_file_redrives_only_when_it_advanced(env):
    """The re-drive is conditional on the conversion actually happening: re-driving after a
    silent drop would delegate the same stage forever."""
    from db import claim_temp_file
    enqueued = []
    env.monkeypatch.setattr(tasks, "enqueue_task", lambda name, data=None, **k: enqueued.append(name))
    env.monkeypatch.setattr(compression, "compress_to", _stub_produce())

    done = env.seed("Game.nsp")
    tasks.compress_file_task(file_id=done.id)
    assert enqueued == ["process_file"]

    enqueued.clear()
    busy = env.seed("Other.nsp")
    assert claim_temp_file(busy.filepath)      # another task holds it: conversion drops
    tasks.compress_file_task(file_id=busy.id)
    assert enqueued == []


# --- decompress_file ---------------------------------------------------------------------

def test_decompress_file_success(env):
    enqueued = []
    env.monkeypatch.setattr(tasks, "enqueue_task", lambda name, data=None, **k: enqueued.append(name))
    env.monkeypatch.setattr(compression, "decompress_to", _stub_produce(b"RAWAGAIN"))
    f = env.seed("Game.nsz", compressed=True)
    source, fid = f.filepath, f.id

    tasks.decompress_file_task(file_id=fid)

    f = db.session.get(Files, fid)
    target = source.rsplit(".", 1)[0] + ".nsp"
    assert f.compressed is False and f.extension == "nsp" and f.filepath == target
    assert os.path.exists(target) and not os.path.exists(source)
    assert enqueued == []   # no re-drive: the compress stage would undo this immediately


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
    assert not _ignored(f.filepath, "")               # source-deletion event popped
    assert _ignored(target, "")                       # our own delete of the partial, for the watcher


def test_cleanup_clears_pending_and_fails_running(env):
    """The whole pending queue is cleared on startup (regenerable; must not preempt the fresh
    startup pipeline), while interrupted running/waiting tasks are failed."""
    def task(name, status):
        t = Task(task_name=name, status=status, input_hash="x")
        db.session.add(t)
        return t

    task("compress_file", "pending")
    task("process_library", "pending")    # library-level leftover that would re-spawn per-file work
    task("identify_file", "pending")
    running = task("compress_file", "running")
    waiting = task("process_library", "waiting_for_children")
    db.session.commit()
    running_id, waiting_id = running.id, waiting.id

    tasks.cleanup_tasks()

    assert Task.query.filter_by(status="pending").count() == 0     # entire queue cleared
    assert db.session.get(Task, running_id).status == "failed"     # interrupted run failed
    assert db.session.get(Task, waiting_id).status == "failed"


def test_reap_worker_task_runs_cleanup(env):
    """Stopping a worker mid-compression fails its running task and runs the cleanup hook,
    so the partial output and TempFile mark are removed (no explicit cancel needed)."""
    f = env.seed("Game.nsp")
    target = str(compression.compressed_path(f.filepath))
    add_temp_file(target)
    add_ignored_event(f.filepath, "")
    open(target, "wb").close()  # partial output left by the killed worker

    t = Task(task_name="compress_file", status="running", worker_id=7,
             input_hash="x", input_json='{"file_id": %d}' % f.id)
    db.session.add(t)
    db.session.commit()
    tid = t.id

    tasks.reap_worker_task(7)

    assert db.session.get(Task, tid).status == "failed"
    assert not os.path.exists(target)           # partial removed by the cleanup hook
    assert TempFile.query.count() == 0          # in-progress mark cleared
    assert not _ignored(f.filepath, "")         # source-deletion event popped
    assert _ignored(target, "")                 # our own delete of the partial, for the watcher


def test_reap_worker_task_noop_without_running_task(env):
    """No running task for the worker (clean exit / already cancelled): reap does nothing."""
    tasks.reap_worker_task(99)  # must not raise


def test_parent_completes_when_children_finish_before_it_parks(env):
    """A fan-out creates its children and only then parks. If another worker finishes them
    all inside that window their completion checks bail (the parent still reads 'running'),
    so the worker must re-check on the way out or the parent waits forever."""
    from worker import TaskWorker
    fired = []

    def fanout(**kwargs):
        parent_id = tasks._current_task_id
        child_id = tasks.create_child_task(parent_id, "update_titles")
        child = db.session.get(Task, child_id)
        child.status = "completed"                    # another worker got there first
        db.session.commit()
        tasks.on_task_completed(child_id, parent_id)  # bails: parent is still 'running'
        tasks.set_waiting_for_children()

    env.monkeypatch.setitem(tasks.TASK_REGISTRY, "fanout_probe", fanout)
    env.monkeypatch.setitem(tasks.TASK_CONTINUATIONS, "fanout_probe",
                            lambda **k: fired.append(True))

    t = Task(task_name="fanout_probe", status="running", worker_id=1,
             input_hash="x", input_json="{}")
    db.session.add(t)
    db.session.commit()
    tid = t.id

    TaskWorker(env.app, worker_id=1).execute_task(tid)

    assert db.session.get(Task, tid) is None   # completed, and parent+children deleted
    assert fired == [True]                     # continuation ran


def test_failed_compress_task_leaves_no_incomplete_file(env):
    """A compress_file that raises inside the worker runs its cleanup hook: the partial output
    and the in-progress marks are removed, so an ordinary failure leaves no incomplete file."""
    from worker import TaskWorker
    f = env.seed("Game.nsp")
    source, fid = f.filepath, f.id
    target = source.rsplit(".", 1)[0] + ".nsz"

    def boom(source_, out_dir, *a, **k):
        open(target, "wb").close()          # a partial output on disk at the moment of failure
        raise RuntimeError("compression blew up")
    env.monkeypatch.setattr(compression, "compress_to", boom)

    t = Task(task_name="compress_file", status="running", worker_id=1,
             input_hash="x", input_json='{"file_id": %d}' % fid)
    db.session.add(t)
    db.session.commit()
    tid = t.id

    TaskWorker(env.app, worker_id=1).execute_task(tid)

    assert db.session.get(Task, tid).status == "failed"
    assert not os.path.exists(target)       # partial removed by the cleanup hook
    assert os.path.exists(source)           # source untouched
    assert TempFile.query.count() == 0      # source claim + target mark cleared


def test_compress_decompress_registered_in_io_group():
    """The disk-heavy (de)compression tasks belong to the 'io' concurrency group."""
    assert tasks.TASK_GROUPS.get("compress_file") == "io"
    assert tasks.TASK_GROUPS.get("decompress_file") == "io"


def test_blocked_task_names(env):
    """A group at its limit blocks every task in that group; ungrouped tasks are never blocked."""
    env.monkeypatch.setattr(tasks, "get_settings", lambda: _settings(group_limits={"io": 1}))
    assert tasks.blocked_task_names([]) == set()
    blocked = tasks.blocked_task_names(["compress_file"])
    assert {"compress_file", "decompress_file"} <= blocked   # whole io group blocked
    assert "process_file" not in blocked                     # ungrouped stays claimable
    # No limits configured -> nothing is blocked.
    env.monkeypatch.setattr(tasks, "get_settings", lambda: _settings())
    assert tasks.blocked_task_names(["compress_file"]) == set()


def test_claim_task_respects_group_cap(env):
    """With io capped at 1 and one io task running, the worker skips an older pending io task
    and claims a newer light task instead; when only io work remains it claims nothing."""
    env.monkeypatch.setattr(tasks, "get_settings", lambda: _settings(group_limits={"io": 1}))
    from worker import TaskWorker

    base = datetime.datetime(2026, 1, 1)
    running = Task(task_name="compress_file", status="running", worker_id=2,
                   input_hash="r", created_at=base)
    io_pending = Task(task_name="compress_file", status="pending",
                      input_hash="a", created_at=base + datetime.timedelta(seconds=1))
    light_pending = Task(task_name="process_file", status="pending",
                         input_hash="b", created_at=base + datetime.timedelta(seconds=2))
    db.session.add_all([running, io_pending, light_pending])
    db.session.commit()
    light_id, io_id = light_pending.id, io_pending.id

    worker = TaskWorker(env.app, worker_id=1)
    # io slot is full -> older io_pending is skipped for the newer light task.
    assert worker.claim_task() == light_id
    # io still capped (only the light task also runs now) -> the pending io task stays unclaimed.
    assert worker.claim_task() is None
    db.session.expire_all()
    assert db.session.get(Task, io_id).status == "pending"


def test_task_progress_writes_completion_pct(env):
    """The progress callback updates a running task's completion_pct; no-op outside a task."""
    assert tasks._task_progress(None) is None

    t = Task(task_name="compress_file", status="running", input_hash="x")
    db.session.add(t)
    db.session.commit()

    report = tasks._task_progress(t.id)
    report(37)
    db.session.expire_all()
    assert db.session.get(Task, t.id).completion_pct == 37

    # Only running tasks are updated (a finished task isn't dragged back).
    t2 = Task(task_name="compress_file", status="completed", completion_pct=100, input_hash="y")
    db.session.add(t2)
    db.session.commit()
    tasks._task_progress(t2.id)(10)
    db.session.expire_all()
    assert db.session.get(Task, t2.id).completion_pct == 100


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
    _, files = __import__("titles").get_dirs_and_files(str(env.lib_dir))
    skip = get_temp_file_paths()
    assert target in files and target not in [f for f in files if f not in skip]
    # Watcher gate.
    assert is_temp_file(target) is True


def _deliver(env, *paths):
    """Push delete events for paths through the watcher callback; return the tasks it enqueued."""
    import app as app_mod
    enqueued = []
    env.monkeypatch.setattr(app_mod, "app", env.app)
    env.monkeypatch.setattr(tasks, "enqueue_task",
                            lambda name, data=None: enqueued.append((name, data)))
    app_mod.on_library_change([
        types.SimpleNamespace(type="deleted", directory=str(env.lib_dir), src_path=p, dest_path="")
        for p in paths
    ])
    return enqueued


def test_cancelled_compression_delete_is_not_a_library_event(env):
    """Cancelling a compression removes the partial .nsz we created: the delete is ours and
    must not reach the library, the way the file's creation never did."""
    f = env.seed("Game.nsp")
    target = str(compression.compressed_path(f.filepath))
    add_temp_file(target)
    open(target, "wb").close()

    tasks._compression_cleanup(file_id=f.id)

    assert _deliver(env, target) == []
    assert not _ignored(target)   # mark consumed by the event, not leaked


def test_delete_of_committed_file_survives_its_temp_mark(env):
    """A file the user really deleted leaves the library even while a task holds it in-progress
    (a compression source is temp-claimed for the whole run)."""
    f = env.seed("Game.nsp")
    add_temp_file(f.filepath)

    assert _deliver(env, f.filepath) == [("handle_file_deleted", {"filepath": f.filepath})]


# --- process_library sweep -----------------------------------------------------------------

def _sweep(env, **settings):
    """Run process_library with its fan-out captured, returning the file_ids it drove."""
    enqueued = []
    if settings:
        env.monkeypatch.setattr(tasks, "get_settings", lambda: _settings(**settings))
    env.monkeypatch.setattr(tasks, "enqueue_or_child",
                            lambda name, data=None: enqueued.append((name, data["file_id"])))
    env.monkeypatch.setattr(tasks, "set_waiting_for_children", lambda: None)
    tasks.process_library_task()
    assert all(name == "process_file" for name, _ in enqueued)
    return sorted(fid for _, fid in enqueued)


def test_process_library_selects_files_with_pending_work(env):
    ok1 = env.seed("A.nsp")
    ok2 = env.seed("B.xci")
    ok3 = env.seed("D.nsp", identified=False)  # unidentified files are compressed too
    env.seed("C.nsz", compressed=True)         # nothing left to do: excluded

    assert _sweep(env) == sorted([ok1.id, ok2.id, ok3.id])


def test_process_library_includes_files_awaiting_organization(env):
    """The inverse of the old compress_library rule: a file that still needs organizing is
    swept in, because the driver organizes it before it delegates compression."""
    organized = env.seed("A.nsp")
    organized.organized = True
    unidentified = env.seed("D.nsp", identified=False)
    awaiting = env.seed("B.xci")   # identified, not organized
    db.session.commit()

    assert _sweep(env, organizer=True) == sorted([organized.id, unidentified.id, awaiting.id])


def test_process_library_skips_a_settled_library(env):
    """With every stage disabled or satisfied the sweep enqueues nothing at all."""
    f = env.seed("C.nsz", compressed=True)
    f.organized = True
    db.session.commit()
    env.seed("A.nsp")   # only the compress stage would apply, and it is off

    assert _sweep(env, compress_files=False) == []


# --- process_file driver -------------------------------------------------------------------

def test_process_file_runs_inline_stages_then_delegates(env):
    """One call identifies, organizes, and hands compression off to its own task — in that
    order, and without a hop through the queue between the inline stages."""
    enqueued = []
    env.monkeypatch.setattr(tasks, "get_settings", lambda: _settings(organizer=True))
    env.monkeypatch.setattr(tasks.titles_lib, "identify_file", lambda fp: (
        "cnmt", True, [{"title_id": "0100AAA000000000", "app_id": "0100AAA000000000",
                        "type": "BASE", "version": "0"}], ""))
    env.monkeypatch.setattr(tasks, "organize_file", lambda *a, **k: True)
    env.monkeypatch.setattr(tasks, "enqueue_task",
                            lambda name, data=None, **k: enqueued.append((name, data)))
    f = env.seed("Game.nsp", identified=False)
    fid = f.id

    tasks.process_file_task(file_id=fid)

    f = db.session.get(Files, fid)
    assert f.identified is True and f.organized is True
    assert [name for name, _ in enqueued] == [
        "add_missing_apps_for_title", "library_maintenance", "compress_file"]
    assert enqueued[-1][1] == {"file_id": fid}   # delegation carries only the id


def test_process_file_skips_a_stage_that_did_not_advance(env):
    """A stage is attempted at most once per drive. organize_file gives up on a file with no
    app or no title info, so re-picking a still-applicable stage would spin the worker."""
    attempts, enqueued = [], []
    env.monkeypatch.setattr(tasks, "get_settings", lambda: _settings(organizer=True))
    env.monkeypatch.setattr(tasks, "organize_file", lambda *a, **k: attempts.append(1) or False)
    env.monkeypatch.setattr(tasks, "enqueue_task", lambda name, data=None, **k: enqueued.append(name))
    f = env.seed("Game.nsp")

    tasks.process_file_task(file_id=f.id)   # must terminate

    assert attempts == [1]                                  # tried once, then skipped
    assert db.session.get(Files, f.id).organized is False
    assert "compress_file" in enqueued                      # still falls through to compression


def test_process_file_deletes_row_when_file_vanished(env):
    """A file gone from disk is dropped from the DB, and its app ownership released with it."""
    released = []
    env.monkeypatch.setattr(tasks, "remove_file_from_apps", lambda fid: released.append(fid))
    f = env.seed("Game.nsp")
    fid = f.id
    os.remove(f.filepath)

    tasks.process_file_task(file_id=fid)

    assert db.session.get(Files, fid) is None
    assert released == [fid]


def test_identify_stage_enqueues_title_expansion_top_level(env):
    """The driver stays a leaf: per-title expansion is enqueued top-level, never as a child,
    so a scan is not held open by it and cancelling one does not cancel the other."""
    enqueued, children = [], []
    env.monkeypatch.setattr(tasks.titles_lib, "identify_file", lambda fp: (
        "cnmt", True, [{"title_id": "0100AAA000000000", "app_id": "0100AAA000000000",
                        "type": "BASE", "version": "0"}], ""))
    env.monkeypatch.setattr(tasks, "enqueue_task", lambda name, data=None, **k: enqueued.append(name))
    env.monkeypatch.setattr(tasks, "enqueue_or_child", lambda *a, **k: children.append(a))
    f = env.seed("Game.nsp", identified=False)

    tasks._identify(f, _settings()["library"]["management"])

    assert db.session.get(Files, f.id).identified is True
    assert enqueued == ["add_missing_apps_for_title"]
    assert children == []


def test_process_library_continuation_runs_maintenance(env):
    enqueued = []
    env.monkeypatch.setattr(tasks, "enqueue_task", lambda name, data=None, **k: enqueued.append(name))
    tasks._process_library_done()
    assert enqueued == ["library_maintenance", "update_titles"]


@pytest.mark.parametrize("enabled,expected", [(True, True), (False, False)])
def test_library_maintenance_chains_outdated_updates(env, enabled, expected):
    enqueued = []
    env.monkeypatch.setattr(tasks, "get_settings", lambda: _settings(delete_older=enabled))
    env.monkeypatch.setattr(tasks, "enqueue_task", lambda name, data=None: enqueued.append(name))
    tasks.library_maintenance_task()
    assert ("remove_outdated_updates" in enqueued) is expected
