"""Which copies deduplication deletes: every file whose apps all have a better copy kept."""
import datetime
import json
import os
import types

import pytest

import library
import tasks
from db import db, Apps, Files, IgnoredEvent, Libraries, Task, TempFile, Titles
from library import (duplicate_files, duplicate_groups, files_with_free_base_name,
                     outdated_update_groups, remove_duplicate_files, remove_outdated_update_files)

from app import create_app

CORRUPT = {"signature_valid": True, "hash_valid": False}

# (case, files as (name, apps it carries, overrides), prefer_multicontent, names deleted).
# Overrides beyond Files columns: `pending` (still has a pipeline stage to run), `missing`
# (not on disk), `library` (index of the library path it sits in).
CASES = [
    ("a unique file is untouched",
     [("a.nsp", "A", {}), ("b.nsp", "B", {})], False, []),
    ("the uncompressed copy of an app goes",
     [("a.nsp", "A", {}), ("a.nsz", "A", {"compressed": True})], False, ["a.nsp"]),
    ("copies in different libraries are duplicates",
     [("a.nsp", "A", {}), ("a.nsz", "A", {"compressed": True, "library": 1})], False, ["a.nsp"]),
    ("a bundle every app of which has its own file goes",
     [("ab.nsp", "AB", {}), ("a.nsp", "A", {}), ("b.nsp", "B", {})], False, ["ab.nsp"]),
    ("a bundle carrying an app nothing else does stays",
     [("ab.nsp", "AB", {}), ("a.nsp", "A", {})], False, []),
    ("overlapping bundles both stay",
     [("ab.nsp", "AB", {}), ("bc.nsp", "BC", {})], False, []),
    ("preferring bundles deletes the files they cover",
     [("ab.nsp", "AB", {}), ("a.nsp", "A", {}), ("b.nsp", "B", {})], True, ["a.nsp", "b.nsp"]),
    ("a broken single-content copy loses to an intact bundle",
     [("ab.nsp", "AB", {}), ("a.nsp", "A", CORRUPT), ("b.nsp", "B", {})], False, ["a.nsp"]),
    ("an app with a pending copy is not judged",
     [("a.nsp", "A", {}), ("a.nsz", "A", {"compressed": True, "pending": True})], False, []),
    ("a pending copy also holds the bundle sharing its app",
     [("ab.nsp", "AB", {}), ("a.nsp", "A", {}), ("b.nsp", "B", {"pending": True})], False, []),
    ("a file identified from its filename may carry more than recorded",
     [("abc.xci", "A", {"identification_type": "filename"}),
      ("a.nsz", "A", {"compressed": True})], False, []),
    ("a filename claim does not cover a bundle",
     [("ab.nsp", "AB", {}), ("a.nsp", "A", {"identification_type": "filename"}),
      ("b.nsp", "B", {})], False, []),
    ("an app whose best copy is gone from disk is not judged",
     [("a.nsp", "A", {}), ("a.nsz", "A", {"compressed": True, "missing": True})], False, []),
]


@pytest.fixture
def env(tmp_path):
    app = create_app(f"sqlite:///{tmp_path/'test.db'}")
    ctx = app.app_context()
    ctx.push()
    db.create_all()
    libraries = []
    for name in ("games", "more"):
        (tmp_path / name).mkdir()
        libraries.append(Libraries(path=str(tmp_path / name)))
    title = Titles(title_id="0100000000010000")
    db.session.add_all(libraries + [title])
    db.session.commit()
    apps = {}

    def seed(name, app_letters, overrides):
        overrides = dict(overrides)
        library = libraries[overrides.pop("library", 0)]
        overrides.pop("pending", None)
        path = os.path.join(library.path, name)
        if not overrides.pop("missing", False):
            with open(path, "wb") as fh:
                fh.write(b"x")
        f = Files(library_id=library.id, filepath=path, filename=name,
                  extension=name.rsplit(".", 1)[-1], identified=True,
                  multicontent=len(app_letters) > 1,
                  **{"identification_type": "cnmt", **overrides})
        db.session.add(f)
        for letter in app_letters:
            if letter not in apps:
                apps[letter] = Apps(title_id=title.id, app_id=letter, app_version="0",
                                    app_type="BASE", owned=True)
                db.session.add(apps[letter])
            apps[letter].files.append(f)
        db.session.commit()
        return f

    yield types.SimpleNamespace(seed=seed, apps=apps)
    ctx.pop()


@pytest.mark.parametrize("case,files,prefer_multicontent,expected",
                         CASES, ids=[c[0] for c in CASES])
def test_duplicate_files(env, case, files, prefer_multicontent, expected):
    for name, app_letters, overrides in files:
        env.seed(name, app_letters, overrides)
    pending = {name for name, _, overrides in files if overrides.get("pending")}

    deleted = duplicate_files(prefer_multicontent, lambda f: f.filename in pending)

    assert sorted(f.filename for f in deleted) == expected


def test_remove_duplicate_files_deletes_the_file_and_its_row(env):
    loser = env.seed("a.nsp", "A", {})
    env.seed("a.nsz", "A", {"compressed": True})
    path = loser.filepath

    remove_duplicate_files(False, lambda f: False)

    assert not os.path.exists(path)
    assert Files.query.filter_by(filepath=path).first() is None
    assert IgnoredEvent.query.filter_by(src_path=path).first() is not None
    assert [f.filename for f in env.apps["A"].files] == ["a.nsz"]
    assert env.apps["A"].owned


def seed_updates(env):
    """An older and a newer owned update of the same title: old.nsp (U) and new.nsp (V)."""
    old = env.seed("old.nsp", "U", {})
    env.seed("new.nsp", "V", {})
    for letter, version in (("U", "65536"), ("V", "131072")):
        app = env.apps[letter]
        app.app_id, app.app_version, app.app_type = "0100000000010800", version, "UPDATE"
    db.session.commit()
    return old


def test_remove_outdated_update_files_deletes_the_file_and_its_row(env):
    path = seed_updates(env).filepath

    remove_outdated_update_files()

    assert not os.path.exists(path)
    assert Files.query.filter_by(filepath=path).first() is None
    assert [f.filename for f in Files.query.all()] == ["new.nsp"]


def test_outdated_update_groups_keep_the_newest_update(env):
    seed_updates(env)

    groups = [(app_pk, [f.filename for f in keep], [f.filename for f in remove])
              for app_pk, keep, remove in outdated_update_groups()]

    assert groups == [(env.apps["V"].id, ["new.nsp"], ["old.nsp"])]


# (case, files, prefer_multicontent, {app letter: (name kept, names deleted)})
GROUP_CASES = [
    ("the uncompressed copy is the deleted one",
     [("a.nsp", "A", {}), ("a.nsz", "A", {"compressed": True})], False, {"A": ("a.nsz", ["a.nsp"])}),
    ("a deleted bundle is in the group of each app it carries",
     [("ab.nsp", "AB", {}), ("a.nsp", "A", {}), ("b.nsp", "B", {})], False,
     {"A": ("a.nsp", ["ab.nsp"]), "B": ("b.nsp", ["ab.nsp"])}),
    ("an app whose copies are all kept has no group",
     [("ab.nsp", "AB", {}), ("a.nsp", "A", {})], False, {}),
]


@pytest.mark.parametrize("case,files,prefer_multicontent,expected",
                         GROUP_CASES, ids=[c[0] for c in GROUP_CASES])
def test_duplicate_groups(env, case, files, prefer_multicontent, expected):
    for name, app_letters, overrides in files:
        env.seed(name, app_letters, overrides)
    letters = {app.id: letter for letter, app in env.apps.items()}

    groups = {letters[app_pk]: (kept.filename, [f.filename for f in removed])
              for app_pk, kept, removed in duplicate_groups(prefer_multicontent, lambda f: False)}

    assert groups == expected


@pytest.fixture
def manual(env, monkeypatch):
    """The manual deletion tasks with settings, pipeline and follow-up work stubbed out."""
    enqueued = []
    mgmt = {"deduplication": {"enabled": False, "prefer_multicontent": False}}
    monkeypatch.setattr(tasks, "get_settings", lambda: {"library": {"management": mgmt}})
    monkeypatch.setattr(tasks, "_has_pending_stage", lambda f, mgmt: False)
    monkeypatch.setattr(tasks, "enqueue_task", lambda name, data=None, **k: enqueued.append(name))
    monkeypatch.setattr(tasks, "request_maintenance", lambda path=None: enqueued.append("maintenance"))
    return enqueued


def make_busy(f, how):
    if how == "task":
        db.session.add(Task(task_name="verify_file", status="running", input_hash="x",
                            input_json=json.dumps({"file_id": f["id"]})))
    elif how == "claim":
        db.session.add(TempFile(filepath=f["filepath"]))
    db.session.commit()


def refs(*files):
    return {f.filename: {"id": f.id, "filepath": f.filepath} for f in files}


# (case, files sent as a function of the shown refs, how a.nsp is busy, names left)
REMOVAL_CASES = [
    ("a previewed duplicate goes", lambda s: [s["a.nsp"]], None, ["a.nsz"]),
    ("a duplicate not previewed stays", lambda s: [], None, ["a.nsp", "a.nsz"]),
    ("a duplicate moved since the preview stays",
     lambda s: [dict(s["a.nsp"], filepath="/elsewhere/a.nsp")], None, ["a.nsp", "a.nsz"]),
    ("a previewed file no longer a duplicate stays", lambda s: [s["a.nsz"]], None, ["a.nsp", "a.nsz"]),
    ("a duplicate being verified stays", lambda s: [s["a.nsp"]], "task", ["a.nsp", "a.nsz"]),
    ("a duplicate being compressed stays", lambda s: [s["a.nsp"]], "claim", ["a.nsp", "a.nsz"]),
]


@pytest.mark.parametrize("case,sent,busy,expected", REMOVAL_CASES, ids=[c[0] for c in REMOVAL_CASES])
def test_remove_duplicates_deletes_only_previewed_duplicates(env, manual, case, sent, busy, expected):
    shown = refs(env.seed("a.nsp", "A", {}), env.seed("a.nsz", "A", {"compressed": True}))
    make_busy(shown["a.nsp"], busy)

    tasks.remove_duplicates_task(files=sent(shown))

    assert sorted(f.filename for f in Files.query.all()) == expected
    assert manual == ["maintenance"]


def test_remove_outdated_updates_deletes_the_previewed_update(env, manual):
    old = seed_updates(env)

    tasks.remove_outdated_updates_task(files=[{"id": old.id, "filepath": old.filepath}])

    assert [f.filename for f in Files.query.all()] == ["new.nsp"]
    assert manual == ["update_titles", "maintenance"]


# (case, path sent, how the file is busy, still on disk, refused, names left)
DELETE_CASES = [
    ("the file goes", None, None, True, False, []),
    ("a file moved since it was shown stays", "/elsewhere/a.nsp", None, True, False, ["a.nsp"]),
    ("a file being verified is refused", None, "task", True, True, ["a.nsp"]),
    ("a file being compressed is refused", None, "claim", True, True, ["a.nsp"]),
    ("a file already gone from disk leaves the library", None, None, False, False, []),
]


@pytest.mark.parametrize("case,path,busy,on_disk,refused,expected",
                         DELETE_CASES, ids=[c[0] for c in DELETE_CASES])
def test_delete_file(env, manual, case, path, busy, on_disk, refused, expected):
    shown = refs(env.seed("a.nsp", "A", {}))["a.nsp"]
    make_busy(shown, busy)
    if not on_disk:
        os.remove(shown["filepath"])

    if refused:
        with pytest.raises(RuntimeError):
            tasks.delete_file_task(file_id=shown["id"], filepath=shown["filepath"])
    else:
        tasks.delete_file_task(file_id=shown["id"], filepath=path or shown["filepath"])

    assert [f.filename for f in Files.query.all()] == expected
    deleted = not expected
    assert manual == (["update_titles", "maintenance"] if deleted else [])
    # Only the claim this test planted may remain: the task releases its own.
    assert [t.filepath for t in TempFile.query.all()] == ([shown["filepath"]] if busy == "claim" else [])


# (case, files as (name, organized), the name the template renders, names released).
RELEASE_CASES = [
    ("a suffixed copy whose base name is free", [("G(2).nsp", True)], "G.{ext}", ["G(2).nsp"]),
    ("a suffixed copy whose base name is taken",
     [("G.nsp", True), ("G(2).nsp", True)], "G.{ext}", []),
    ("a later suffix whose base name is free",
     [("G(2).nsp", True), ("G(3).nsp", True)], "G.{ext}", ["G(2).nsp", "G(3).nsp"]),
    ("the base name under another extension does not hold it",
     [("G.nsz", True), ("G(2).nsp", True)], "G.{ext}", ["G(2).nsp"]),
    ("a file not organized yet", [("G(2).nsp", False)], "G.{ext}", []),
    ("a file without a suffix", [("G.nsp", True)], "G.{ext}", []),
    ("a name the template itself ends in (n)", [("G(2019).nsp", True)], "G(2019).{ext}", []),
]


@pytest.mark.parametrize("case,files,template,expected",
                         RELEASE_CASES, ids=[c[0] for c in RELEASE_CASES])
def test_files_with_free_base_name(env, monkeypatch, case, files, template, expected):
    monkeypatch.setattr(library, "organized_path", lambda f, library_path, settings:
                        os.path.join(library_path, template.format(ext=f.extension)))
    for name, organized in files:
        env.seed(name, "A", {"organized": organized})

    assert sorted(f.filename for f in files_with_free_base_name({})) == expected


@pytest.mark.parametrize("pending,queued", [(False, True), (True, False)])
def test_release_hands_the_file_back_to_the_organizer(env, monkeypatch, pending, queued):
    """An idle file is re-driven now; one with a stage in flight is re-driven by that stage."""
    enqueued = []
    monkeypatch.setattr(tasks, "_has_pending_stage", lambda f, mgmt: pending)
    monkeypatch.setattr(tasks, "enqueue_task", lambda name, data=None: enqueued.append((name, data)))
    f = env.seed("G(2).nsp", "A", {"organized": True})
    monkeypatch.setattr(tasks, "files_with_free_base_name", lambda settings: [f])

    tasks._release_suffixed_files({"organizer": {}})

    assert db.session.get(Files, f.id).organized is False
    assert enqueued == ([("process_file", {"file_id": f.id})] if queued else [])


# (delete_older_updates, deduplication, organizer, remove_empty_folders, steps run in order)
MAINTENANCE_CASES = [
    (False, False, False, False, []),
    (True, False, False, False, ["outdated"]),
    (False, True, False, False, ["duplicates"]),
    (False, False, True, False, ["release"]),
    (False, False, True, True, ["release", "folders"]),
    (False, False, False, True, []),
    (True, True, True, True, ["outdated", "duplicates", "release", "folders"]),
]


@pytest.mark.parametrize("older,dedup,organizer,folders,expected", MAINTENANCE_CASES)
def test_library_maintenance_runs_enabled_steps_in_order(monkeypatch, older, dedup, organizer,
                                                         folders, expected):
    """Dedup judges what outdated-update removal left, both can free a "(n)" name, and folders
    are pruned last."""
    steps = []
    mgmt = {"delete_older_updates": older,
            "deduplication": {"enabled": dedup, "prefer_multicontent": False},
            "organizer": {"enabled": organizer, "remove_empty_folders": folders}}
    monkeypatch.setattr(tasks, "get_settings", lambda: {"library": {"management": mgmt}})
    monkeypatch.setattr(tasks, "enqueue_task", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "remove_outdated_update_files", lambda: steps.append("outdated"))
    monkeypatch.setattr(tasks, "remove_duplicate_files", lambda *a: steps.append("duplicates"))
    monkeypatch.setattr(tasks, "_release_suffixed_files", lambda mgmt: steps.append("release"))
    monkeypatch.setattr(tasks, "delete_empty_folders", lambda path: steps.append("folders"))

    tasks.library_maintenance_task(library_path="/games")

    assert steps == expected


# (case, maintenance passes already queued, passes pending after a request)
REQUEST_CASES = [
    ("the first request queues a pass", [], 1),
    ("a request joins the pending pass", ["pending"], 1),
    ("a request while a pass runs queues the next one", ["running"], 1),
]


@pytest.mark.parametrize("case,existing,pending", REQUEST_CASES, ids=[c[0] for c in REQUEST_CASES])
def test_request_maintenance_is_throttled(env, case, existing, pending):
    """One pending pass per window: joining never postpones it, and a running pass never
    swallows a request made after it read the library."""
    before = datetime.datetime.utcnow()
    first_run_after = before + datetime.timedelta(seconds=5)
    for status in existing:
        db.session.add(Task(task_name="library_maintenance", status=status, input_json="{}",
                            input_hash=tasks.compute_input_hash({}), run_after=first_run_after))
    db.session.commit()

    tasks.request_maintenance()

    rows = Task.query.filter_by(task_name="library_maintenance", status="pending").all()
    assert len(rows) == pending
    if "pending" in existing:
        assert rows[0].run_after == first_run_after
    else:
        assert before < rows[0].run_after <= datetime.datetime.utcnow() + tasks.MAINTENANCE_THROTTLE


def test_a_settled_file_requests_maintenance(env, monkeypatch):
    """The pass that skipped a file while a stage was in flight gets a successor once it settles."""
    requested = []
    monkeypatch.setattr(tasks, "get_settings", lambda: {"library": {"management": {}}})
    monkeypatch.setattr(tasks, "STAGES", [])
    monkeypatch.setattr(tasks, "request_maintenance", lambda path=None: requested.append(path))
    f = env.seed("a.nsz", "A", {"compressed": True})

    tasks.process_file_task(file_id=f.id)

    assert requested == [os.path.dirname(f.filepath)]


# (case, group_limits in settings, tasks running, whether a maintenance pass may start)
MAINTENANCE_SLOT_CASES = [
    ("nothing running", {}, [], True),
    ("another pass running", {}, ["library_maintenance"], False),
    ("a settings limit cannot raise it", {"maintenance": 5}, ["library_maintenance"], False),
    ("unrelated work running", {"io": 1}, ["compress_file", "process_file"], True),
]


@pytest.mark.parametrize("case,limits,running,claimable",
                         MAINTENANCE_SLOT_CASES, ids=[c[0] for c in MAINTENANCE_SLOT_CASES])
def test_one_maintenance_pass_at_a_time(monkeypatch, case, limits, running, claimable):
    """Two passes would judge the same duplicates and race to delete them."""
    monkeypatch.setattr(tasks, "get_settings", lambda: {"worker": {"group_limits": limits}})

    assert ("library_maintenance" not in tasks.blocked_task_names(running)) is claimable


@pytest.mark.parametrize("task,stub,args", [
    ("handle_file_deleted_task", "delete_file_by_filepath", {"filepath": "/games/G.nsp"}),
    ("remove_missing_files_task", "remove_missing_files_from_db", {}),
])
def test_deletions_outside_ownfoil_run_maintenance(monkeypatch, task, stub, args):
    """A file deleted by hand can free a "(n)" name just like one deleted by dedup."""
    enqueued = []
    monkeypatch.setattr(tasks, stub, lambda *a: None)
    monkeypatch.setattr(tasks, "enqueue_task", lambda name, data=None, **k: enqueued.append(name))

    getattr(tasks, task)(**args)

    assert "library_maintenance" in enqueued
