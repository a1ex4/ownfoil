"""Behavioural tests for the file watcher's Handler and Watcher.

The Handler consumes raw watchdog events and emits *library events* (created / modified /
deleted / moved / dir_deleted) to its callback. Those raw events are produced by two very
different backends:

  * the native ``Observer`` (inotify on Linux), and
  * the ``PollingObserver`` (directory-snapshot diffing).

Every event case below is run against *both* backends via the parametrised ``observer_env``
fixture. Assertions are written against the Handler's **library-event output**, i.e. the
functional outcome (which file ends up created/deleted/moved), never against the raw
watchdog event stream — so the expectations are identical regardless of backend, even where
the backends emit different raw events (e.g. a folder moved out: inotify emits only a
directory delete, polling additionally emits per-file deletes; both yield ``dir_deleted``).

The test cases (inputs) and their expected library events (outputs) are declared separately
in the ``CASES`` table; a single parametrised test drives them uniformly.
"""

import itertools
import os
import sys
import threading
import time
import types

import pytest
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

import file_watcher


# --- Handler harness ---------------------------------------------------------------------

_key_counter = itertools.count()
STABILITY = 0.5  # short stability/debounce windows so the suite runs quickly


def _make_handler(callback):
    """A Handler with short, per-instance debounce keys so tests never share debounce state."""
    h = file_watcher.Handler(callback, stability_duration=STABILITY)
    n = next(_key_counter)
    h.debounced_check_final = h._debounce(h._check_file_stability, STABILITY, f"test-stab-{n}")
    h.debounced_dir_walk = h._debounce(h._flush_dir_walks, STABILITY, f"test-dir-{n}")
    return h


class Ctx:
    """Test context: the watched/outside dirs plus the library events the Handler emitted."""

    def __init__(self, watched, outside, events, lock):
        self.watched = watched
        self.outside = outside
        self._events = events
        self._lock = lock

    def snapshot(self):
        with self._lock:
            return list(self._events)

    def matches(self, etype, name, dest=None):
        """True if some emitted library event has this type and mentions ``name`` (src or dest)."""
        with self._lock:
            for kind, src, dst in self._events:
                if kind != etype:
                    continue
                if name not in (src, dst):
                    continue
                if dest is not None and dst != dest:
                    continue
                return True
        return False

    def wait(self, etype, name, dest=None, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.matches(etype, name, dest):
                return True
            time.sleep(0.05)
        return False


@pytest.fixture(params=["native", "polling"])
def observer_env(request, tmp_path):
    """Run the Handler behind either the native or the polling observer over a temp dir."""
    watched = tmp_path / "watched"
    watched.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    events = []
    lock = threading.Lock()

    def callback(library_events):
        with lock:
            for e in library_events:
                src = os.path.basename(e.src_path.rstrip("/"))
                dst = os.path.basename((getattr(e, "dest_path", "") or "").rstrip("/"))
                events.append((e.type, src, dst))

    handler = _make_handler(callback)
    handler.add_directory(str(watched))

    observer = Observer() if request.param == "native" else PollingObserver(timeout=0.2)
    observer.schedule(handler, str(watched), recursive=True)
    observer.start()
    time.sleep(0.4)  # let inotify arm / polling take its initial snapshot

    try:
        yield Ctx(watched, outside, events, lock)
    finally:
        observer.stop()
        observer.join()


# --- Case actions (inputs) ---------------------------------------------------------------
# Each action performs a filesystem scenario. Where a step needs a settled precondition it
# waits for the earlier library event, so ordering is deterministic across both backends.

def _file_created(c):
    (c.watched / "a.nsp").write_text("data")


def _file_modified(c):
    (c.watched / "a.nsp").write_text("data")
    c.wait("created", "a.nsp")
    (c.watched / "a.nsp").write_text("data" * 5000)  # size change


def _file_deleted(c):
    (c.watched / "a.nsp").write_text("data")
    c.wait("created", "a.nsp")
    (c.watched / "a.nsp").unlink()


def _file_moved_within(c):
    (c.watched / "a.nsp").write_text("data")
    c.wait("created", "a.nsp")
    (c.watched / "a.nsp").rename(c.watched / "b.nsp")


def _file_moved_out(c):
    (c.watched / "a.nsp").write_text("data")
    c.wait("created", "a.nsp")
    (c.watched / "a.nsp").rename(c.outside / "a.nsp")


def _file_moved_in(c):
    (c.outside / "a.nsp").write_text("data")
    (c.outside / "a.nsp").rename(c.watched / "a.nsp")


def _non_allowed_extension(c):
    (c.watched / "readme.txt").write_text("data")


def _dir_moved_in_atomic(c):
    game = c.outside / "Game"
    game.mkdir()
    (game / "g.nsp").write_text("data")
    game.rename(c.watched / "Game")


def _dir_created_then_populated(c):
    # Cross-filesystem copy shape: an empty folder appears, then files arrive later — after
    # the first (empty) walk has already run.
    d = c.watched / "Game"
    d.mkdir()
    time.sleep(STABILITY + 0.6)
    (d / "g.nsp").write_text("data")


def _dir_moved_out(c):
    game = c.watched / "Game"
    game.mkdir()
    (game / "g.nsp").write_text("data")
    c.wait("created", "g.nsp")
    game.rename(c.outside / "Game")


def _dir_renamed_within(c):
    game = c.watched / "Game"
    game.mkdir()
    (game / "g.nsp").write_text("data")
    c.wait("created", "g.nsp")
    game.rename(c.watched / "Game2")


# --- Case table: (id, action, expected library events, forbidden library events) ---------
# Expectations are (type, name[, dest]); `name` matches an event's src or dest basename.

CASES = [
    ("file_created",               _file_created,             [("created", "a.nsp")],        []),
    ("file_modified",              _file_modified,            [("modified", "a.nsp")],       []),
    ("file_deleted",               _file_deleted,             [("deleted", "a.nsp")],        []),
    ("file_moved_within",          _file_moved_within,        [("moved", "b.nsp")],          []),
    ("file_moved_out",             _file_moved_out,           [("deleted", "a.nsp")],        []),
    ("file_moved_in",              _file_moved_in,            [("created", "a.nsp")],        []),
    ("non_allowed_extension",      _non_allowed_extension,    [],
        [("created", "readme.txt"), ("modified", "readme.txt")]),
    ("dir_moved_in_atomic",        _dir_moved_in_atomic,      [("created", "g.nsp")],        []),
    ("dir_created_then_populated", _dir_created_then_populated, [("created", "g.nsp")],      []),
    ("dir_moved_out",              _dir_moved_out,            [("dir_deleted", "Game")],     []),
    ("dir_renamed_within",         _dir_renamed_within,       [("moved", "g.nsp")],          []),
]


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_event_case(observer_env, case):
    _id, action, expect, forbid = case
    action(observer_env)

    for etype, *rest in expect:
        name = rest[0]
        dest = rest[1] if len(rest) > 1 else None
        assert observer_env.wait(etype, name, dest), (
            f"expected library event {(etype, *rest)} was not emitted; "
            f"got {observer_env.snapshot()}"
        )

    if forbid:
        time.sleep(STABILITY + 1.5)  # give any (unwanted) event time to surface
        for etype, name in forbid:
            assert not observer_env.matches(etype, name), (
                f"forbidden library event {(etype, name)} was emitted; "
                f"got {observer_env.snapshot()}"
            )


def test_growing_file_not_reported_until_stable(observer_env):
    """A file still being written must not be reported until its size settles."""
    c = observer_env
    target = c.watched / "big.nsp"
    stop = threading.Event()

    def grow():
        with open(target, "wb") as fh:
            while not stop.is_set():
                fh.write(b"x" * 100_000)
                fh.flush()
                os.fsync(fh.fileno())
                time.sleep(0.2)  # < STABILITY, so it never looks "stable" while growing

    writer = threading.Thread(target=grow)
    writer.start()
    try:
        time.sleep(STABILITY * 4)  # actively growing for well over one stability window
        assert not c.matches("created", "big.nsp"), (
            f"a still-growing file was reported as stable: {c.snapshot()}"
        )
    finally:
        stop.set()
        writer.join()

    assert c.wait("created", "big.nsp"), (
        f"file was never reported after it stopped growing: {c.snapshot()}"
    )


def _feed(handler, src_path, event_type="created", is_directory=False, dest_path=""):
    """Dispatch a raw watchdog-shaped event straight into the Handler."""
    handler.on_any_event(types.SimpleNamespace(
        event_type=event_type, is_directory=is_directory,
        src_path=src_path, dest_path=dest_path))


def _wait_for(seen, timeout=5.0):
    """Wait for the stability debounce to flush at least one library event."""
    deadline = time.time() + timeout
    while time.time() < deadline and not seen:
        time.sleep(0.05)
    return seen


def test_sibling_directory_is_not_matched_by_prefix(tmp_path):
    """A watched /mnt/games must not swallow events from the sibling /mnt/games-old: the
    emitted `directory` becomes library_path downstream, where it is looked up by exact match."""
    games = tmp_path / "games"
    games.mkdir()
    sibling = tmp_path / "games-old"
    sibling.mkdir()
    stray = sibling / "zelda.nsp"
    stray.write_text("data")

    seen = []
    h = _make_handler(lambda evs: seen.extend(evs))
    h.add_directory(str(games))

    _feed(h, str(stray))
    time.sleep(STABILITY + 0.5)

    assert seen == [], f"event from an unwatched sibling was attributed to {str(games)}: {seen}"


def test_nested_watched_paths_attribute_to_deepest(tmp_path):
    """With both a library and a sub-library configured, the deepest one owns the file."""
    outer = tmp_path / "library"
    inner = outer / "switch"
    inner.mkdir(parents=True)
    game = inner / "zelda.nsp"
    game.write_text("data")

    seen = []
    h = _make_handler(lambda evs: seen.extend(evs))
    h.add_directory(str(outer))
    h.add_directory(str(inner))

    _feed(h, str(game))
    _wait_for(seen)
    assert any(e.directory == str(inner) for e in seen), (
        f"expected attribution to {str(inner)}, got {[(e.type, e.directory) for e in seen]}"
    )


def test_trailing_separator_still_matches(tmp_path):
    """A library path configured with a trailing slash still matches its own files."""
    lib = tmp_path / "library"
    lib.mkdir()
    game = lib / "zelda.nsp"
    game.write_text("data")

    seen = []
    h = _make_handler(lambda evs: seen.extend(evs))
    h.add_directory(str(lib) + os.sep)

    _feed(h, str(game))
    _wait_for(seen)
    assert [e.type for e in seen] == ["created"], f"got {seen}"


def test_windows_reports_a_removed_folder_as_a_file_delete(tmp_path, monkeypatch):
    """watchdog's Windows emitter turns every FILE_ACTION_REMOVED into a FileDeletedEvent, even
    for a folder, so a delete of a non-media entry has to be read as a possible folder removal —
    otherwise a deleted game folder leaves its files in the library forever."""
    lib = tmp_path / "library"
    lib.mkdir()

    seen = []
    h = _make_handler(lambda evs: seen.extend(evs))
    h.add_directory(str(lib))

    monkeypatch.setattr(file_watcher.sys, "platform", "win32")
    _feed(h, str(lib / "Zelda"), event_type="deleted")

    assert [(e.type, os.path.basename(e.src_path)) for e in seen] == [("dir_deleted", "Zelda")]


def test_non_media_delete_is_ignored_off_windows(tmp_path, monkeypatch):
    """Elsewhere a real DirDeletedEvent arrives, so a plain file delete must stay ignored."""
    lib = tmp_path / "library"
    lib.mkdir()

    seen = []
    h = _make_handler(lambda evs: seen.extend(evs))
    h.add_directory(str(lib))

    monkeypatch.setattr(file_watcher.sys, "platform", "linux")
    _feed(h, str(lib / "notes.txt"), event_type="deleted")

    assert seen == []


def test_track_file_ignores_vanished_file(tmp_path):
    """A file that disappears between its event and the size probe (e.g. a conversion's
    transient output) is skipped, not allowed to crash the observer dispatch thread."""
    h = _make_handler(lambda events: None)
    gone = tmp_path / "poof.xcz"  # never created
    event = types.SimpleNamespace(type="created", src_path=str(gone), dest_path="")

    h._track_file(event)  # must not raise

    assert str(gone) not in h.tracked_files


# --- Observer selection (Watcher routing) ------------------------------------------------

def _stub_settings(monkeypatch, **funcs):
    """Inject a fake `settings` module so Watcher tests don't import the real one (which pulls
    in nsz/keys). file_watcher imports settings lazily, so this stub is what it picks up."""
    module = types.ModuleType("settings")
    for name, fn in funcs.items():
        setattr(module, name, fn)
    monkeypatch.setitem(sys.modules, "settings", module)


def test_watcher_routes_by_filesystem(monkeypatch, tmp_path):
    """Local paths go to the shared native observer; network paths get a dedicated poller."""
    _stub_settings(monkeypatch,
                   get_library_paths=lambda: [],
                   get_watcher_config=lambda: {"enabled": True, "polling_interval": 5})

    local = tmp_path / "local"
    local.mkdir()
    net = tmp_path / "net"
    net.mkdir()
    monkeypatch.setattr(file_watcher, "is_network_path", lambda p: p == str(net))

    w = file_watcher.Watcher(lambda evs: None)
    w.run()
    try:
        assert w.add_directory(str(local))
        assert w.add_directory(str(net))

        local_obs, _ = w.scheduler_map[str(local)]
        net_obs, _ = w.scheduler_map[str(net)]

        assert local_obs is w.native
        assert isinstance(net_obs, PollingObserver)
        assert net_obs is not w.native
    finally:
        w.stop()


def test_watcher_skips_disabled_path(monkeypatch, tmp_path):
    """A path whose watcher is disabled is not scheduled on any observer."""
    _stub_settings(monkeypatch,
                   get_library_paths=lambda: [],
                   get_watcher_config=lambda: {"enabled": False, "polling_interval": 5})
    monkeypatch.setattr(file_watcher, "is_network_path", lambda p: False)

    lib = tmp_path / "lib"
    lib.mkdir()
    w = file_watcher.Watcher(lambda evs: None)
    w.run()
    try:
        assert w.add_directory(str(lib)) is False
        assert str(lib) not in w.scheduler_map
    finally:
        w.stop()
