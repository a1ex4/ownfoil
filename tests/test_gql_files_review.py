"""The Files review page's GraphQL: cleanup previews, pending work, and the writes acting on them."""
import base64
import json
import types

import pytest

import db as db_mod
import tasks as tasks_mod
import titledb
from app import create_app
from capture.fixture import PASSWORDS
from constants import APP_TYPE_BASE, APP_TYPE_UPD
from db import Apps, Files, Libraries, Task, Titles, db, init_db
from gql import graphql_dispatch

ALPHA = "0100000000AAA000"
ALPHA_UPD = "0100000000AAA800"
BETA = "0100000000BBB000"
NAMELESS = "0100000000CCC000"  # owned, but unknown to titledb
TITLEDB_JSON = {ALPHA: {"id": ALPHA, "name": "Alpha Game"}, BETA: {"id": BETA, "name": "beta Game"}}

# (filename, app id, app type, version, overrides)
FILES = [
    ("a.nsp", ALPHA, APP_TYPE_BASE, "0", {}),
    ("a.nsz", ALPHA, APP_TYPE_BASE, "0", {"compressed": True}),
    ("u1.nsp", ALPHA_UPD, APP_TYPE_UPD, "65536", {}),
    ("u2.nsp", ALPHA_UPD, APP_TYPE_UPD, "131072", {}),
    ("unknown.nsp", None, None, None, {"identified": False, "identification_type": None,
                                       "identification_attempts": 1}),
]


@pytest.fixture
def library(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    titledb_dir = tmp_path / "titledb"
    titledb_dir.mkdir()
    games = tmp_path / "games"
    games.mkdir()
    monkeypatch.setattr(db_mod, "DB_FILE", str(config / "ownfoil.db"))
    monkeypatch.setattr(db_mod, "TITLES_DB_FILE", str(config / "titles.db"))
    monkeypatch.setattr(titledb.store, "TITLES_DB_FILE", str(config / "titles.db"))
    monkeypatch.setattr(titledb.store, "DB_FILE", str(config / "ownfoil.db"))
    monkeypatch.setattr(tasks_mod, "_has_pending_stage", lambda f, mgmt: False)

    app = create_app(f"sqlite:///{config / 'ownfoil.db'}")
    app.add_url_rule("/api/graphql", view_func=graphql_dispatch, methods=["GET", "POST"])
    init_db(app)

    region_file = titledb_dir / "titles.US.en.json"
    region_file.write_text(json.dumps(TITLEDB_JSON))
    (titledb_dir / "cnmts.json").write_text("{}")
    (titledb_dir / "versions.json").write_text("{}")
    with app.app_context():
        titledb.store.import_from_json(str(region_file), "US.en")
        title = Titles(title_id=ALPHA, have_base=True)
        library_row = Libraries(path=str(games))
        db.session.add_all([title, library_row])
        db.session.flush()
        apps = {}
        for name, app_id, app_type, version, overrides in FILES:
            (games / name).write_bytes(b"x")
            f = Files(library_id=library_row.id, filename=name, extension=name[-3:],
                      filepath=str(games / name), size=1,
                      **{"identified": True, "identification_type": "cnmt", **overrides})
            db.session.add(f)
            if app_id:
                key = (app_id, version)
                if key not in apps:
                    apps[key] = Apps(title_id=title.id, app_id=app_id, app_version=version,
                                     app_type=app_type, owned=True)
                    db.session.add(apps[key])
                apps[key].files.append(f)
        db.session.commit()

    return types.SimpleNamespace(app=app, client=app.test_client(), games=games)


def query(library, document):
    resp = library.client.get("/api/graphql", query_string={"query": document})
    body = resp.get_json()
    assert "errors" not in body, body["errors"]
    return body["data"]


def mutate(library, document, variables=None, expect_error=False):
    body = library.client.post("/api/graphql", json={"query": document,
                                                     "variables": variables or {}}).get_json()
    if expect_error:
        assert body.get("errors"), f"expected an error, got {body}"
        return body["errors"][0]["message"]
    assert "errors" not in body, body["errors"]
    return body["data"]


def ref(library, filename):
    with library.app.app_context():
        f = Files.query.filter_by(filename=filename).one()
        return {"id": str(f.id), "filepath": f.filepath}


def queued(library, name):
    with library.app.app_context():
        return [json.loads(t.input_json) for t in Task.query.filter_by(task_name=name).all()]


GROUP = """{ app { appType appVersion title { name } } keep { filename library { path } }
             remove { reason file { filename apps { appVersion } } } }"""


def test_duplicates_groups_each_app_with_its_kept_and_deleted_copies(library):
    groups = query(library, "{ duplicates %s }" % GROUP)["duplicates"]

    assert groups == [{
        "app": {"appType": "BASE", "appVersion": 0, "title": {"name": "Alpha Game"}},
        "keep": [{"filename": "a.nsz", "library": {"path": str(library.games)}}],
        "remove": [{"reason": "COMPRESSED", "file": {"filename": "a.nsp", "apps": [{"appVersion": 0}]}}],
    }]


def test_outdated_updates_keep_the_newest_update(library):
    groups = query(library, "{ outdatedUpdates %s }" % GROUP)["outdatedUpdates"]

    assert groups == [{
        "app": {"appType": "UPDATE", "appVersion": 131072, "title": {"name": "Alpha Game"}},
        "keep": [{"filename": "u2.nsp", "library": {"path": str(library.games)}}],
        "remove": [{"reason": "OLDER_VERSION", "file": {"filename": "u1.nsp", "apps": [{"appVersion": 65536}]}}],
    }]


def add_file(library, name, apps=(), size=1, **overrides):
    """A file on disk carrying the fixture's apps keyed (app id, version)."""
    (library.games / name).write_bytes(b"x")
    with library.app.app_context():
        f = Files(library_id=Libraries.query.one().id, filename=name, extension=name[-3:],
                  filepath=str(library.games / name), size=size,
                  **{"identified": True, "identification_type": "cnmt", **overrides})
        db.session.add(f)
        carried = [Apps.query.filter_by(app_id=app_id, app_version=version).one()
                   for app_id, version in apps]
        for app in carried:
            app.files.append(f)
        db.session.commit()


SUMMARY = "{ files size }"


def test_cleanup_summaries_count_what_the_previews_list(library):
    # Deleted in the group of both apps it carries, counted once.
    add_file(library, "bundle.nsp", [(ALPHA, "0"), (ALPHA_UPD, "131072")], size=10, multicontent=True)

    data = query(library, "{ stats { duplicates %s outdatedUpdates %s } duplicates %s outdatedUpdates %s }"
                 % (SUMMARY, SUMMARY, GROUP, GROUP))

    for field in ("duplicates", "outdatedUpdates"):
        listed = {r["file"]["filename"]: r for g in data[field] for r in g["remove"]}
        sizes = {"a.nsp": 1, "u1.nsp": 1, "bundle.nsp": 10}
        assert data["stats"][field] == {"files": len(listed), "size": sum(sizes[n] for n in listed)}
    assert data["stats"]["duplicates"] == {"files": 2, "size": 11}


def test_pending_files_stat_is_the_pending_total(library, monkeypatch):
    monkeypatch.setattr(tasks_mod, "STAGES", [
        tasks_mod.Stage("compress", lambda f, mgmt: f.filename in ("u1.nsp", "u2.nsp"), None, None)])

    data = query(library, "{ stats { pendingFiles } pendingFiles { total } }")

    assert data["stats"]["pendingFiles"] == data["pendingFiles"]["total"] == 2


# (direction, files in order): named titles alphabetically, then files with no title name
# (unidentified, or a title titledb does not know) by id, whichever the direction.
TITLE_ORDER_CASES = [
    ("ASC", ["a.nsp", "a.nsz", "u1.nsp", "u2.nsp", "beta.nsp", "unknown.nsp", "nameless.nsp"]),
    ("DESC", ["beta.nsp", "a.nsp", "a.nsz", "u1.nsp", "u2.nsp", "unknown.nsp", "nameless.nsp"]),
]


@pytest.mark.parametrize("direction,expected", TITLE_ORDER_CASES)
def test_files_order_by_title(library, direction, expected):
    with library.app.app_context():
        for title_id, name in ((BETA, "beta.nsp"), (NAMELESS, "nameless.nsp")):
            title = Titles(title_id=title_id, have_base=True)
            db.session.add(title)
            db.session.flush()
            db.session.add(Apps(title_id=title.id, app_id=title_id, app_version="0",
                                app_type=APP_TYPE_BASE, owned=True))
        db.session.commit()
    add_file(library, "beta.nsp", [(BETA, "0")])
    add_file(library, "nameless.nsp", [(NAMELESS, "0")])

    data = query(library, "{ files(orderBy: {field: TITLE, direction: %s}) { items { filename } } }"
                 % direction)

    assert [f["filename"] for f in data["files"]["items"]] == expected


def test_pending_files_lists_the_stages_due(library, monkeypatch):
    monkeypatch.setattr(tasks_mod, "STAGES", [
        tasks_mod.Stage("read", lambda f, mgmt: not f.identified, None, None),
        tasks_mod.Stage("compress", lambda f, mgmt: f.filename.startswith("u"), None, None),
    ])

    data = query(library, "{ pendingFiles(limit: 2) { total items { stages file { filename } } } }")

    assert data["pendingFiles"] == {"total": 3, "items": [
        {"stages": ["compress"], "file": {"filename": "u1.nsp"}},
        {"stages": ["compress"], "file": {"filename": "u2.nsp"}},
    ]}


# (case, title filter, files matched). The fixture's files all carry Alpha Game, bar the
# unidentified one, which carries no title at all.
TITLE_CASES = [
    ("the name, in any case", "alpha GAME", ["a.nsp", "a.nsz", "u1.nsp", "u2.nsp"]),
    ("the whole title id, in any case", ALPHA.lower(), ["a.nsp", "a.nsz", "u1.nsp", "u2.nsp"]),
    ("four hex digits of the id", "aaa0", ["a.nsp", "a.nsz", "u1.nsp", "u2.nsp"]),
    ("fewer hex digits are not read as an id", "aa0", []),
    ("an update's own id is not its title's", ALPHA_UPD, []),
    ("a name nothing carries", "Beta", []),
]


@pytest.mark.parametrize("case,needle,expected", TITLE_CASES, ids=[c[0] for c in TITLE_CASES])
def test_files_filter_by_title(library, case, needle, expected):
    """The SQL of `files` and the in-memory match under `App.files` must agree."""
    top = query(library, '{ files(filter: {title: "%s"}) { items { filename } } }' % needle)
    nested = query(library, '{ apps { items { files(filter: {title: "%s"}) { filename } } } }' % needle)

    assert sorted(f["filename"] for f in top["files"]["items"]) == expected
    assert sorted({f["filename"] for a in nested["apps"]["items"] for f in a["files"]}) == expected


# (app type, files matched): the fixture carries bases and updates, no DLC.
APP_TYPE_CASES = [
    ("BASE", ["a.nsp", "a.nsz"]),
    ("UPDATE", ["u1.nsp", "u2.nsp"]),
    ("DLC", []),
    ("[BASE, DLC]", ["a.nsp", "a.nsz"]),
    ("[BASE, UPDATE]", ["a.nsp", "a.nsz", "u1.nsp", "u2.nsp"]),
]


@pytest.mark.parametrize("app_type,expected", APP_TYPE_CASES)
def test_files_filter_by_app_type(library, app_type, expected):
    """The SQL of `files` and the in-memory match under `App.files` must agree."""
    top = query(library, "{ files(filter: {appType: %s}) { items { filename } } }" % app_type)
    nested = query(library, "{ apps { items { files(filter: {appType: %s}) { filename } } } }" % app_type)

    assert sorted(f["filename"] for f in top["files"]["items"]) == expected
    assert sorted({f["filename"] for a in nested["apps"]["items"] for f in a["files"]}) == expected


# (case, filter, files matched)
ANY_OF_CASES = [
    ("either branch", '{anyOf: [{appType: UPDATE}, {filename: {eq: "a.nsz"}}]}',
     ["a.nsz", "u1.nsp", "u2.nsp"]),
    ("the same field at both levels binds apart",
     '{appType: BASE, anyOf: [{appType: BASE, filename: {eq: "a.nsp"}}, {filename: {eq: "a.nsz"}}]}',
     ["a.nsp", "a.nsz"]),
    ("no branch matching matches nothing", '{anyOf: [{appType: DLC}, {multicontent: true}]}', []),
    ("an empty list is no constraint", '{appType: UPDATE, anyOf: []}', ["u1.nsp", "u2.nsp"]),
]


@pytest.mark.parametrize("case,flt,expected", ANY_OF_CASES, ids=[c[0] for c in ANY_OF_CASES])
def test_files_filter_any_of(library, case, flt, expected):
    """The SQL of `files` and the in-memory match under `App.files` must agree."""
    top = query(library, "{ files(filter: %s) { items { filename } } }" % flt)
    nested = query(library, "{ apps { items { files(filter: %s) { filename } } } }" % flt)

    assert sorted(f["filename"] for f in top["files"]["items"]) == expected
    assert sorted({f["filename"] for a in nested["apps"]["items"] for f in a["files"]}) == expected


# (mutation, file shown, task queued)
REMOVALS = [
    ("removeDuplicates", "a.nsp", "remove_duplicates"),
    ("removeOutdatedUpdates", "u1.nsp", "remove_outdated_updates"),
]


@pytest.mark.parametrize("mutation,filename,task", REMOVALS)
def test_a_removal_queues_the_files_shown(library, mutation, filename, task):
    shown = ref(library, filename)

    mutate(library, "mutation ($files: [FileRefInput!]!) { %s(files: $files) { id } }" % mutation,
           {"files": [shown]})

    assert queued(library, task) == [{"files": [{"id": int(shown["id"]),
                                                 "filepath": shown["filepath"]}]}]


def test_a_removal_of_nothing_is_refused(library):
    message = mutate(library, "mutation { removeDuplicates(files: []) { id } }", expect_error=True)

    assert "No files" in message


def test_delete_file_queues_the_file_shown(library):
    shown = ref(library, "a.nsp")

    mutate(library, "mutation ($file: FileRefInput!) { deleteFile(file: $file) { id } }",
           {"file": shown})

    assert queued(library, "delete_file") == [{"file_id": int(shown["id"]),
                                               "filepath": shown["filepath"]}]


def test_delete_file_refuses_a_file_that_moved_since_shown(library):
    shown = dict(ref(library, "a.nsp"), filepath="/elsewhere/a.nsp")

    message = mutate(library, "mutation ($file: FileRefInput!) { deleteFile(file: $file) { id } }",
                     {"file": shown}, expect_error=True)

    assert "changed" in message
    assert queued(library, "delete_file") == []


def test_retry_identification_resets_attempts_and_reprocesses(library):
    shown = ref(library, "unknown.nsp")

    mutate(library, "mutation ($id: ID!) { retryIdentification(fileId: $id) { id } }",
           {"id": shown["id"]})

    with library.app.app_context():
        assert db.session.get(Files, int(shown["id"])).identification_attempts == 0
    assert queued(library, "process_file") == [{"file_id": int(shown["id"])}]


def test_a_manual_compression_is_marked_manual(library):
    shown = ref(library, "a.nsp")

    mutate(library, "mutation ($id: ID!) { compressFile(fileId: $id) { id } }", {"id": shown["id"]})

    assert queued(library, "compress_file") == [{"file_id": int(shown["id"]), "manual": True}]


def test_verification_without_keys_is_refused_and_keeps_the_verdicts(library, monkeypatch):
    import settings
    monkeypatch.setattr(settings, "load_keys", lambda: (None, [], []))
    shown = ref(library, "a.nsp")
    with library.app.app_context():
        f = db.session.get(Files, int(shown["id"]))
        f.signature_valid = True
        db.session.commit()

    message = mutate(library, "mutation ($id: ID!) { verifyFile(fileId: $id) { id } }",
                     {"id": shown["id"]}, expect_error=True)

    assert "keys" in message
    with library.app.app_context():
        assert db.session.get(Files, int(shown["id"])).signature_valid is True
    assert queued(library, "verify_file") == []


def test_a_manual_verification_is_marked_manual(library, monkeypatch):
    import settings
    monkeypatch.setattr(settings, "load_keys", lambda: (True, [], []))
    shown = ref(library, "a.nsp")

    mutate(library, "mutation ($id: ID!) { verifyFile(fileId: $id) { id } }", {"id": shown["id"]})

    assert queued(library, "verify_file") == [{"file_id": int(shown["id"]), "manual": True}]


def basic(user):
    return {"Authorization": "Basic " + base64.b64encode(
        f"{user}:{PASSWORDS[user]}".encode()).decode()}


def test_review_is_admin_only(shop_app):
    client = shop_app.app.test_client()
    body = client.get("/api/graphql", headers=basic("shopper"), query_string={"query": """
        { duplicates { app { id } } outdatedUpdates { app { id } } pendingFiles { total } }"""}).json

    assert body["data"] == {"duplicates": [], "outdatedUpdates": [], "pendingFiles": {"total": 0}}

    body = client.get("/api/graphql", headers=basic("shopper"), query_string={"query": """
        { stats { duplicates { files } outdatedUpdates { files } pendingFiles } }"""}).json
    assert body["data"] == {"stats": {"duplicates": None, "outdatedUpdates": None, "pendingFiles": 0}}

    body = client.post("/api/graphql", headers=basic("shopper"), json={
        "query": 'mutation { deleteFile(file: {id: "1", filepath: "/x"}) { id } }'}).json
    assert body["errors"]
