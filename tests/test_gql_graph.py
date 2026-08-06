"""Tests that the graph is navigable from every entry point, not just from `apps`.

A GraphQL schema promises that a field means the same thing wherever you reach it.
These tests hold the schema to that: `versions` and `title` are hydrated by the top
level `apps` query, and a title-detail page (title -> apps -> versions) or a file
list (files -> apps -> title) has to get the same data rather than silent nulls.

Assertions are on what a client can read, not on which hydrator produced it.
"""

import json
import types

import pytest

import db as db_mod
import titledb
from app import create_app
from constants import APP_TYPE_BASE, APP_TYPE_DLC, APP_TYPE_UPD
from db import Apps, Files, Libraries, Task, Titles, db, init_db
from gql import graphql_dispatch


ALPHA = "0100000000AAAAA0"[:16]
ALPHA_UPD = ALPHA[:-3] + "800"
ALPHA_DLC = ALPHA[:-4] + "1001"

# The update app deliberately has no titledb row of its own - that is the normal case
# for updates, and it is what makes `App.title` the only way to name the game.
TITLEDB_JSON = {
    ALPHA:     {"id": ALPHA, "name": "Alpha Game", "publisher": "Nintendo"},
    ALPHA_DLC: {"id": ALPHA_DLC, "name": "Alpha Extra Levels"},
}

# (app_id, type, version, owned, filename or None, size)
# Distinct sizes, deliberately not in id order, so a size sort is falsifiable.
APPS = [
    (ALPHA,     APP_TYPE_BASE, "0",      True,  "Alpha.nsp",         3000),
    (ALPHA_UPD, APP_TYPE_UPD,  "65536",  True,  "Alpha[v65536].nsp", 1000),
    (ALPHA_UPD, APP_TYPE_UPD,  "131072", False, None,                None),
    (ALPHA_DLC, APP_TYPE_DLC,  "0",      True,  "AlphaDLC.nsp",      2000),
    (ALPHA_DLC, APP_TYPE_DLC,  "65536",  False, None,                None),
]
TOTAL_SIZE = 3000 + 1000 + 2000


@pytest.fixture
def library(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    titledb_dir = tmp_path / "titledb"
    titledb_dir.mkdir()
    monkeypatch.setattr(db_mod, "DB_FILE", str(config / "ownfoil.db"))
    monkeypatch.setattr(db_mod, "TITLES_DB_FILE", str(config / "titles.db"))
    monkeypatch.setattr(titledb.store, "TITLES_DB_FILE", str(config / "titles.db"))
    monkeypatch.setattr(titledb.store, "DB_FILE", str(config / "ownfoil.db"))

    app = create_app(f"sqlite:///{config / 'ownfoil.db'}")
    app.add_url_rule("/api/graphql", view_func=graphql_dispatch, methods=["GET", "POST"])
    init_db(app)

    region_file = titledb_dir / "titles.US.en.json"
    region_file.write_text(json.dumps(TITLEDB_JSON))
    (titledb_dir / "cnmts.json").write_text("{}")
    (titledb_dir / "versions.json").write_text("{}")
    with app.app_context():
        titledb.store.import_from_json(str(region_file), "US.en")

        title = Titles(title_id=ALPHA, have_base=True, up_to_date=False, complete=True)
        db.session.add(title)
        library_row = Libraries(path=str(tmp_path / "games"))
        db.session.add_all([title, library_row])
        db.session.flush()

        for app_id, app_type, version, owned, filename, size in APPS:
            app_row = Apps(title_id=title.id, app_id=app_id, app_version=version,
                           app_type=app_type, owned=owned)
            db.session.add(app_row)
            if filename:
                file_row = Files(library_id=library_row.id,
                                 filepath=str(tmp_path / "games" / filename),
                                 filename=filename, extension="nsp", size=size,
                                 identified=True)
                db.session.add(file_row)
                app_row.files.append(file_row)

        parent = Task(task_name="scan_libraries", status="completed",
                      completion_pct=100, input_hash="a", input_json='{"path": "/games"}')
        db.session.add(parent)
        db.session.flush()
        db.session.add(Task(task_name="scan_library", status="running",
                            completion_pct=40, input_hash="b", parent_id=parent.id))
        db.session.commit()

    return types.SimpleNamespace(app=app, client=app.test_client())


def query(library, text_query, **variables):
    params = {"query": text_query}
    if variables:
        params["variables"] = json.dumps(variables)
    resp = library.client.get("/api/graphql", query_string=params)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert "errors" not in body, body["errors"]
    return body["data"]


# The version history behind a BASE app is its title's UPDATE apps, so this is the
# same answer whichever query reaches the app.
EXPECTED_BASE_VERSIONS = [(65536, True), (131072, False)]


def test_a_titles_apps_carry_their_versions(library):
    """The title-detail page's query. Previously always null on this path."""
    data = query(library, """
        query { title(titleId: "%s") { name apps(appType: ["BASE"]) {
            appId versions { version owned } } } }""" % ALPHA)

    base = data["title"]["apps"][0]
    assert [(v["version"], v["owned"]) for v in base["versions"]] == EXPECTED_BASE_VERSIONS


def test_the_titles_list_carries_versions_too(library):
    data = query(library, """
        query { titles(owned: true, page: 1, pageSize: 10) { items {
            titleId apps(appType: ["BASE"]) { versions { version owned } } } } }""")

    base = data["titles"]["items"][0]["apps"][0]
    assert [(v["version"], v["owned"]) for v in base["versions"]] == EXPECTED_BASE_VERSIONS


def test_reaching_an_app_by_title_or_by_apps_gives_the_same_versions(library):
    """The point of the fix: a field cannot mean two different things by path."""
    via_apps = query(library, """
        query { apps(groupByAppId: true, appType: ["BASE"], page: 1, pageSize: 10) {
            items { appId versions { version owned } } } }""")["apps"]["items"][0]
    via_title = query(library, """
        query { title(titleId: "%s") { apps(appType: ["BASE"]) {
            appId versions { version owned } } } }""" % ALPHA)["title"]["apps"][0]

    assert via_apps["versions"] == via_title["versions"]


def test_a_file_names_the_game_behind_an_update(library):
    """An UPDATE app has no titledb row of its own, so without `title` a file list
    cannot say which game the file belongs to."""
    data = query(library, """
        query { files(filter: {filename: {contains: "v65536"}}, page: 1, pageSize: 10) {
            items { filename apps { appId appType titledb { name } title { titleId name } } } } }""")

    app = data["files"]["items"][0]["apps"][0]
    assert app["appType"] == "UPDATE"
    assert app["titledb"] is None          # no metadata of its own, as expected
    assert app["title"]["name"] == "Alpha Game"
    assert app["title"]["titleId"] == ALPHA


def test_a_files_apps_carry_their_versions(library):
    data = query(library, """
        query { files(filter: {filename: {contains: "AlphaDLC"}}, page: 1, pageSize: 10) {
            items { apps { appId versions { version owned } } } } }""")

    dlc = data["files"]["items"][0]["apps"][0]
    assert [(v["version"], v["owned"]) for v in dlc["versions"]] == [(0, True), (65536, False)]


def test_unrequested_nested_fields_stay_null(library):
    """The hydrators are selection-gated: not asking must still cost nothing, and the
    back-link path must not start recursing."""
    data = query(library, """
        query { apps(appType: ["BASE"], page: 1, pageSize: 10) { items {
            files { filename apps { appId files { filename } } } } } }""")

    backlinked = data["apps"]["items"][0]["files"][0]["apps"][0]
    assert backlinked["files"] is None


# ---- entities that had no representation in the graph at all ----

def test_a_file_resolves_its_library(library):
    """libraryId on its own is a dangling integer; a file page needs the path."""
    data = query(library, """
        query { files(page: 1, pageSize: 1) { items { libraryId library { id path } } } }""")

    file_ = data["files"]["items"][0]
    assert file_["library"]["id"] == str(file_["libraryId"])
    assert file_["library"]["path"].endswith("games")


def test_libraries_are_listable(library):
    data = query(library, "query { libraries { id path lastScan } }")

    assert len(data["libraries"]) == 1
    assert data["libraries"][0]["lastScan"] is None   # never scanned in this fixture


def test_tasks_list_top_level_only_by_default(library):
    """The child scan_library must not appear as its own row, or an activity page
    double-counts the work."""
    data = query(library, "query { tasks { id taskName status completionPct } }")

    assert [t["taskName"] for t in data["tasks"]] == ["scan_libraries"]


def test_a_task_carries_its_children_and_payload(library):
    data = query(library, """
        query { tasks { taskName input children { taskName status completionPct } } }""")

    parent = data["tasks"][0]
    assert json.loads(parent["input"]) == {"path": "/games"}
    assert parent["children"] == [
        {"taskName": "scan_library", "status": "running", "completionPct": 40}]


def test_including_children_flattens_them_into_the_list(library):
    data = query(library, "query { tasks(includeChildren: true) { taskName } }")

    assert {t["taskName"] for t in data["tasks"]} == {"scan_libraries", "scan_library"}


def test_tasks_filter_by_status(library):
    data = query(library, """
        query { tasks(includeChildren: true, status: "running") { taskName } }""")

    assert [t["taskName"] for t in data["tasks"]] == ["scan_library"]


def test_a_single_task_resolves_by_id(library):
    listed = query(library, "query { tasks { id } }")["tasks"][0]

    data = query(library, 'query { task(id: "%s") { taskName } }' % listed["id"])

    assert data["task"]["taskName"] == "scan_libraries"


# (case, the stats selection, the expected value)
STATS_CASES = [
    ("file count",        "totalFiles",        3),
    ("bytes on disk",     "totalSize",         TOTAL_SIZE),
    ("identified",        "identifiedFiles",   3),
    ("unidentified",      "unidentifiedFiles", 0),
    # titledb holds the base game and its DLC; only the base game is owned.
    ("catalogue size",    "totalTitles",       2),
    ("owned titles",      "ownedTitles",       1),
    ("app rows",          "totalApps",         5),
    ("owned app rows",    "ownedApps",         3),
]


@pytest.mark.parametrize("case,field,expected", STATS_CASES,
                         ids=[c[0] for c in STATS_CASES])
def test_stats_aggregate_the_library(library, case, field, expected):
    data = query(library, "query { stats { %s } }" % field)

    assert data["stats"][field] == expected


def test_stats_group_by_key(library):
    data = query(library, """
        query { stats {
            filesByExtension { key count size }
            appsByType { key count }
            filesByLibrary { key count size } } }""")

    assert data["stats"]["filesByExtension"] == [
        {"key": "nsp", "count": 3, "size": TOTAL_SIZE}]
    assert {b["key"]: b["count"] for b in data["stats"]["appsByType"]} == {
        "BASE": 1, "UPDATE": 2, "DLC": 2}
    assert data["stats"]["filesByLibrary"][0]["count"] == 3


def test_unselected_stats_groups_stay_null(library):
    """Each group is its own GROUP BY; not asking must not run it."""
    data = query(library, "query { stats { totalFiles } }")["stats"]

    assert data == {"totalFiles": 3}


def test_a_single_app_hydrates_like_the_list(library):
    """`app(id:)` reuses the list resolver, so nested fields must behave identically
    even though the selection set is flat rather than under `items`."""
    listed = query(library, """
        query { apps(appType: ["BASE"], page: 1, pageSize: 1) { items {
            id appId versions { version owned } } } }""")["apps"]["items"][0]

    data = query(library, """
        query { app(id: "%s") { appId versions { version owned } title { name } } }
        """ % listed["id"])

    assert data["app"]["appId"] == listed["appId"]
    assert data["app"]["versions"] == listed["versions"]
    assert data["app"]["title"]["name"] == "Alpha Game"


def test_a_single_file_hydrates_like_the_list(library):
    listed = query(library, """
        query { files(page: 1, pageSize: 1) { items { id filename } } }""")["files"]["items"][0]

    data = query(library, """
        query { file(id: "%s") { filename library { path } apps { appId } } }
        """ % listed["id"])

    assert data["file"]["filename"] == listed["filename"]
    assert data["file"]["library"]["path"].endswith("games")
    assert len(data["file"]["apps"]) == 1


# ---- ordering ----

def test_files_sort_by_size_in_both_directions(library):
    """`files` had no orderBy at all: it was hard-wired to id order."""
    def sizes(direction):
        return [f["size"] for f in query(library, """
            query { files(orderBy: {field: SIZE, direction: %s}, page: 1, pageSize: 10) {
                items { size } } }""" % direction)["files"]["items"]]

    assert sizes("ASC") == sorted(sizes("ASC"))
    assert sizes("DESC") == sorted(sizes("ASC"), reverse=True)


def test_files_sort_by_name(library):
    names = [f["filename"] for f in query(library, """
        query { files(orderBy: {field: NAME, direction: DESC}, page: 1, pageSize: 10) {
            items { filename } } }""")["files"]["items"]]

    assert names == sorted(names, key=str.lower, reverse=True)


def test_titles_sort_by_name_descending(library):
    """DESC did not exist before: OrderBy was an enum with no direction."""
    names = [t["name"] for t in query(library, """
        query { titles(page: 1, pageSize: 10, orderBy: {field: NAME, direction: DESC}) {
            items { name } } }""")["titles"]["items"]]

    assert names == sorted(names, key=lambda n: (n is None, (n or "").lower()), reverse=True)


def test_ordering_is_stable_across_pages(library):
    """Sorting on a non-unique column needs a unique tie-break, or paging can show
    one row twice and drop another."""
    seen = []
    for page in (1, 2, 3):
        seen += [f["id"] for f in query(library, """
            query { files(orderBy: {field: SIZE}, page: %d, pageSize: 1) {
                items { id } } }""" % page)["files"]["items"]]

    assert len(seen) == len(set(seen)) == 3


def test_an_inapplicable_sort_field_falls_back(library):
    """DOWNLOAD_COUNT means nothing for titles; degrading to the default order beats
    erroring, and must never reach SQL as a column name."""
    def ids(order_clause):
        return [t["titleId"] for t in query(library, """
            query { titles(page: 1, pageSize: 10%s) { items { titleId } } }
            """ % order_clause)["titles"]["items"]]

    assert ids(", orderBy: {field: DOWNLOAD_COUNT}") == ids("")


def test_added_at_is_exposed_and_sortable(library):
    data = query(library, """
        query { files(orderBy: {field: ADDED_AT, direction: DESC}, page: 1, pageSize: 10) {
            items { filename addedAt } } }""")

    assert all(f["addedAt"] for f in data["files"]["items"])


def test_a_missing_id_resolves_to_null(library):
    data = query(library, 'query { app(id: "99999") { appId } file(id: "99999") { filename } task(id: "99999") { taskName } }')

    assert data == {"app": None, "file": None, "task": None}
