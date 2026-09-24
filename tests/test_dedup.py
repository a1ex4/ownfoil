"""Which copies deduplication deletes: every file whose apps all have a better copy kept."""
import os
import types

import pytest

import tasks
from db import db, Apps, Files, IgnoredEvent, Libraries, Titles
from library import duplicate_files, remove_duplicate_files, remove_outdated_update_files

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


def test_remove_outdated_update_files_deletes_the_file_and_its_row(env):
    old = env.seed("old.nsp", "U", {})
    env.seed("new.nsp", "V", {})
    for letter, version in (("U", "65536"), ("V", "131072")):
        app = env.apps[letter]
        app.app_id, app.app_version, app.app_type = "0100000000010800", version, "UPDATE"
    db.session.commit()
    path = old.filepath

    remove_outdated_update_files()

    assert not os.path.exists(path)
    assert Files.query.filter_by(filepath=path).first() is None
    assert [f.filename for f in Files.query.all()] == ["new.nsp"]


# (delete_older_updates, deduplication, remove_empty_folders, steps run in order)
MAINTENANCE_CASES = [
    (False, False, False, []),
    (True, False, False, ["outdated"]),
    (False, True, False, ["duplicates"]),
    (False, False, True, ["folders"]),
    (True, True, True, ["outdated", "duplicates", "folders"]),
]


@pytest.mark.parametrize("older,dedup,folders,expected", MAINTENANCE_CASES)
def test_library_maintenance_runs_enabled_steps_in_order(monkeypatch, older, dedup, folders, expected):
    """Dedup judges what outdated-update removal left, and folders are pruned after both."""
    steps = []
    mgmt = {"delete_older_updates": older,
            "deduplication": {"enabled": dedup, "prefer_multicontent": False},
            "organizer": {"enabled": True, "remove_empty_folders": folders}}
    monkeypatch.setattr(tasks, "get_settings", lambda: {"library": {"management": mgmt}})
    monkeypatch.setattr(tasks, "enqueue_task", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "remove_outdated_update_files", lambda: steps.append("outdated"))
    monkeypatch.setattr(tasks, "remove_duplicate_files", lambda *a: steps.append("duplicates"))
    monkeypatch.setattr(tasks, "delete_empty_folders", lambda path: steps.append("folders"))

    tasks.library_maintenance_task(library_path="/games")

    assert steps == expected
