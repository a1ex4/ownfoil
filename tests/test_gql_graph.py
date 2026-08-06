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
        {"taskName": "scan_library", "status": "RUNNING", "completionPct": 40}]


def test_including_children_flattens_them_into_the_list(library):
    data = query(library, "query { tasks(includeChildren: true) { taskName } }")

    assert {t["taskName"] for t in data["tasks"]} == {"scan_libraries", "scan_library"}


def test_tasks_filter_by_status(library):
    data = query(library, """
        query { tasks(includeChildren: true, status: RUNNING) { taskName } }""")

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


# ---- boolean filters ----
#
# Booleans take a bare `Boolean`, not an operator object. `false` is the case worth
# holding onto: read with truthiness rather than `is None`, a false predicate is
# silently dropped and the query answers as if it had never been asked.

# (filter argument, expected app count) - the fixture has 3 owned apps and 2 unowned.
OWNED_CASES = [("", 5), ("filter: {owned: true}, ", 3), ("filter: {owned: false}, ", 2)]

# (filter argument, expected file count) - all 3 fixture files are identified and none
# are compressed, so `compressed: false` is the one that must not degrade to unfiltered.
FILE_BOOL_CASES = [
    ("", 3),
    ("filter: {identified: true}, ", 3),
    ("filter: {identified: false}, ", 0),
    ("filter: {compressed: false}, ", 3),
    ("filter: {compressed: true}, ", 0),
]


@pytest.mark.parametrize("arg,expected", OWNED_CASES)
def test_apps_filter_on_a_bare_boolean(library, arg, expected):
    data = query(library, """
        query { apps(%spage: 1, pageSize: 10) { total items { owned } } }""" % arg)

    assert data["apps"]["total"] == expected
    assert len(data["apps"]["items"]) == expected


@pytest.mark.parametrize("arg,expected", OWNED_CASES)
def test_a_titles_apps_filter_on_the_same_boolean(library, arg, expected):
    """The nested path filters an already-hydrated list in memory; it has to agree
    with the SQL the top-level query runs."""
    field = "apps(%s)" % arg.rstrip(", ") if arg else "apps"
    apps = query(library, """
        query { title(titleId: "%s") { %s { owned } } }
        """ % (ALPHA, field))["title"]["apps"]

    assert len(apps) == expected


@pytest.mark.parametrize("arg,expected", FILE_BOOL_CASES)
def test_files_filter_on_a_bare_boolean(library, arg, expected):
    data = query(library, """
        query { files(%spage: 1, pageSize: 10) { total items { filename } } }""" % arg)

    assert data["files"]["total"] == expected


def test_a_files_apps_are_all_owned(library):
    """`File.apps` has no `owned` argument because it could not discriminate: an app
    is owned exactly when it has files. If a write path ever links a file without
    flipping `owned`, this is what says so."""
    data = query(library, """
        query { files(page: 1, pageSize: 10) { items { apps { appId owned } } } }""")

    apps = [a for f in data["files"]["items"] for a in f["apps"]]
    assert len(apps) == 3
    assert all(a["owned"] for a in apps)


# The fixture's only library title is ALPHA (haveBase, complete, not upToDate);
# ALPHA_DLC exists in titledb alone. An ownership filter has to answer for both, so
# `false` must reach the catalogue title rather than dropping it for having no
# ownership row at all.
# (filter, expected total over the whole catalogue)
CATALOGUE_OWNERSHIP_CASES = [
    ("{complete: true}", 1),
    ("{complete: false}", 1),
    ("{haveBase: true}", 1),
    ("{haveBase: false}", 1),
    ("{upToDate: false}", 2),
    ("{upToDate: true}", 0),
]


@pytest.mark.parametrize("filter_literal,expected", CATALOGUE_OWNERSHIP_CASES)
def test_ownership_filters_reach_catalogue_titles(library, filter_literal, expected):
    """A title with no library row has no ownership row either. Compared bare, its
    NULL matched neither `true` nor `false`, so catalogue titles silently vanished
    from any ownership-filtered query."""
    total = query(library, """
        query { titles(filter: %s, page: 1, pageSize: 50) { total } }
        """ % filter_literal)["titles"]["total"]

    assert total == expected


# (filter, expected total among titles that are not in the library at all)
UNOWNED_OWNERSHIP_CASES = [
    ("{haveBase: false}", 1), ("{haveBase: true}", 0),
    ("{complete: false}", 1), ("{complete: true}", 0),
]


@pytest.mark.parametrize("filter_literal,expected", UNOWNED_OWNERSHIP_CASES)
def test_ownership_filters_discriminate_under_owned_false(library, filter_literal, expected):
    """`owned: false` adds `ot.id IS NULL`, which used to make every ownership filter
    match nothing in both polarities - an empty page that looked like an answer."""
    total = query(library, """
        query { titles(owned: false, filter: %s, page: 1, pageSize: 50) { total } }
        """ % filter_literal)["titles"]["total"]

    assert total == expected


# (grouped?, owned value) - the shorthand argument and the filter field have to give
# the same answer. Grouped they did not: the shorthand asked MAX(owned) of the group
# while the filter asked the row, so `false` returned 0 one way and 2 the other.
@pytest.mark.parametrize("grouped", [True, False])
@pytest.mark.parametrize("value", ["true", "false"])
def test_owned_shorthand_and_filter_agree(library, grouped, value):
    def total(arg):
        return query(library, """
            query { apps(groupByAppId: %s, %s, page: 1, pageSize: 50) { total } }
            """ % ("true" if grouped else "false", arg))["apps"]["total"]

    assert total("owned: %s" % value) == total("filter: {owned: %s}" % value)


def test_grouped_owned_means_any_version_owned(library):
    """The fixture has an app id with one owned and one unowned version. Grouped, it
    is owned - and so must not also appear under `owned: false`."""
    def app_ids(arg):
        return sorted(i["appId"] for i in query(library, """
            query { apps(groupByAppId: true, %s, page: 1, pageSize: 50) { items { appId } } }
            """ % arg)["apps"]["items"])

    assert app_ids("owned: true") == app_ids("filter: {owned: true}")
    assert app_ids("owned: false") == app_ids("filter: {owned: false}") == []


def test_contradictory_owned_spellings_match_nothing(library):
    """Given both, they AND - asking for owned and unowned at once is not a conflict
    to resolve, it is a query with no answer."""
    total = query(library, """
        query { apps(owned: true, filter: {owned: false}, page: 1, pageSize: 50) {
            total } }""")["apps"]["total"]

    assert total == 0


def test_file_apps_rejects_the_owned_argument(library):
    """Removed from the schema, not merely ignored - a client that still sends it
    should hear about it rather than silently get an unfiltered list."""
    resp = library.client.get("/api/graphql", query_string={
        "query": "query { files(page: 1, pageSize: 10) { items { apps(owned: false) { appId } } } }"})
    body = resp.get_json()

    assert body.get("errors"), body


# ---- typed surfaces ----
#
# Four places where the schema described something other than what the data is: a
# version as a string, a grouped row as a composite, a status as free text, and a
# list column as a scalar.


def test_app_version_is_an_integer_everywhere(library):
    """`appVersion` and `versions { version }` are the same quantity, so they were
    the same type - one of them just said String."""
    item = query(library, """
        query { apps(appType: ["UPDATE"], orderBy: {field: VERSION, direction: DESC},
                     page: 1, pageSize: 1) {
            items { appVersion versions { version } } } }""")["apps"]["items"][0]

    assert item["appVersion"] == 131072
    assert item["versions"][0]["version"] == 65536


# (filter, expected app versions) - a range is the point: as strings "9" > "65536",
# so `gte` could not have been offered on the old StringFilter at all.
VERSION_FILTER_CASES = [
    ("{appVersion: {gte: 65536}}", [65536, 65536, 131072]),
    ("{appVersion: {lte: 0}}", [0, 0]),
    ("{appVersion: {eq: 131072}}", [131072]),
    ("{appVersion: {in: [0, 131072]}}", [0, 0, 131072]),
]


@pytest.mark.parametrize("filter_literal,expected", VERSION_FILTER_CASES)
def test_apps_filter_by_version_numerically(library, filter_literal, expected):
    versions = [i["appVersion"] for i in query(library, """
        query { apps(filter: %s, orderBy: {field: VERSION}, page: 1, pageSize: 50) {
            items { appVersion } } }""" % filter_literal)["apps"]["items"]]

    assert sorted(versions) == expected


def test_apps_sort_by_version(library):
    def versions(direction):
        return [i["appVersion"] for i in query(library, """
            query { apps(appType: ["UPDATE"], orderBy: {field: VERSION, direction: %s},
                         page: 1, pageSize: 50) { items { appVersion } } }
            """ % direction)["apps"]["items"]]

    assert versions("ASC") == [65536, 131072]
    assert versions("DESC") == [131072, 65536]


def test_a_grouped_app_is_a_real_row(library):
    """Grouped, `id` came from MIN(id) while `appVersion` came from MAX(version), so
    an item could describe a row that does not exist. Refetching its own id has to
    return the same app."""
    grouped = query(library, """
        query { apps(groupByAppId: true, page: 1, pageSize: 50) {
            items { id appId appVersion releaseDate } } }""")["apps"]["items"]

    assert grouped, "fixture should produce grouped rows"
    for item in grouped:
        refetched = query(library, """
            query { app(id: "%s") { appId appVersion } }""" % item["id"])["app"]
        assert refetched["appId"] == item["appId"]
        assert refetched["appVersion"] == item["appVersion"]


def test_a_grouped_app_still_reports_the_highest_version(library):
    """The row is real, but it is the right one: a card shows the newest version."""
    by_app = {i["appId"]: i["appVersion"] for i in query(library, """
        query { apps(groupByAppId: true, page: 1, pageSize: 50) {
            items { appId appVersion } } }""")["apps"]["items"]}

    assert by_app[ALPHA_UPD] == 131072
    assert by_app[ALPHA_DLC] == 65536


def test_grouped_ownership_is_still_group_level(library):
    """`owned` is the one field that is deliberately about the group rather than the
    row - the highest version of ALPHA_UPD is unowned, but the app id is owned."""
    by_app = {i["appId"]: i for i in query(library, """
        query { apps(groupByAppId: true, page: 1, pageSize: 50) {
            items { appId appVersion owned } } }""")["apps"]["items"]}

    assert by_app[ALPHA_UPD]["appVersion"] == 131072
    assert by_app[ALPHA_UPD]["owned"] is True


def test_task_status_is_an_enum(library):
    data = query(library, """
        query { tasks(includeChildren: true) { taskName status } }""")

    assert {t["status"] for t in data["tasks"]} == {"COMPLETED", "RUNNING"}


def test_an_unregistered_task_name_is_an_error(library):
    """[] for a typo is indistinguishable from "nothing has run"."""
    resp = library.client.get("/api/graphql", query_string={
        "query": 'query { tasks(taskName: "scan_librairies") { id } }'})
    body = resp.get_json()

    assert body.get("errors"), body
    assert "scan_librairies" in body["errors"][0]["message"]


# (filter, whether ALPHA matches) - the column holds '["Adventure","Puzzle"]', so the
# old StringFilter could only ever match that whole encoded string.
CATEGORY_CASES = [
    ('{has: "Adventure"}', True),
    ('{has: "Racing"}', False),
    ('{hasAny: ["Racing", "Puzzle"]}', True),
    ('{hasAny: ["Racing"]}', False),
    ('{hasAll: ["Adventure", "Puzzle"]}', True),
    ('{hasAll: ["Adventure", "Racing"]}', False),
]


@pytest.mark.parametrize("filter_literal,matches", CATEGORY_CASES)
def test_category_filters_by_element(library, filter_literal, matches):
    from db import db
    from sqlalchemy import text as sql_text
    with library.app.app_context():
        db.session.execute(
            sql_text("UPDATE titledb.titles SET category = :c WHERE id = :i"),
            {"c": '["Adventure","Puzzle"]', "i": ALPHA})
        db.session.commit()

    ids = [t["titleId"] for t in query(library, """
        query { titles(filter: {category: %s}, page: 1, pageSize: 50) {
            items { titleId } } }""" % filter_literal)["titles"]["items"]]

    assert (ALPHA in ids) is matches


def test_category_filter_survives_a_non_json_column(library):
    """These columns default to an empty string, and `json_each` raises on malformed
    JSON rather than yielding nothing."""
    from db import db
    from sqlalchemy import text as sql_text
    with library.app.app_context():
        db.session.execute(sql_text("UPDATE titledb.titles SET category = ''"))
        db.session.commit()

    total = query(library, """
        query { titles(filter: {category: {has: "Adventure"}}, page: 1, pageSize: 50) {
            total } }""")["titles"]["total"]

    assert total == 0


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
