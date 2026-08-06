"""Query resolvers for the GraphQL API."""
from typing import Dict, List, Optional

import strawberry
from sqlalchemy import text

from constants import APP_TYPE_BASE, APP_TYPE_UPD
from db import db

from .context import GraphQLContext
from .filters import (
    AppFilter, FileFilter, TitleFilter,
    APP_FIELDS, FILE_FIELDS, TITLE_FIELDS, build_clauses,
)
from .selection import Selection
from .types import (
    App, AppConnection, AppVersion, CountByKey, File, FileConnection, Library,
    LibraryStats, Ownership, Task, Title, TitleConnection, decode_json_list,
)


# ------------- column lists -------------

# Map of GraphQL camelCase field name on the Title type → SQL fragment to add
# to the SELECT list. Built dynamically so a query asking for only
# `{titleId, name, bannerUrl}` doesn't drag `description`, `intro`,
# `screenshots`, etc. through SQLite + Python + JSON serialization for every
# title in the page.
_TITLE_COL_MAP = {
    'name':            'td.name AS name',
    'bannerUrl':       'td.banner_url AS banner_url',
    'iconUrl':         'td.icon_url AS icon_url',
    'frontBoxArt':     'td.front_box_art AS front_box_art',
    'description':     'td.description AS description',
    'intro':           'td.intro AS intro',
    'developer':       'td.developer AS developer',
    'publisher':       'td.publisher AS publisher',
    'releaseDate':     'td.release_date AS release_date',
    'category':        'td.category AS category',
    'isDemo':          'td.is_demo AS is_demo',
    'nsuId':           'td.nsu_id AS nsu_id',
    'numberOfPlayers': 'td.number_of_players AS number_of_players',
    'parentId':        'td.parent_id AS parent_id',
    'rank':            'td.rank AS rank',
    'rating':          'td.rating AS rating',
    'ratingContent':   'td.rating_content AS rating_content',
    'region':          'td.region AS region',
    'regions':         'td.regions AS regions',
    'languages':       'td.languages AS languages',
    'language':        'td.language AS language',
    'rightsId':        'td.rights_id AS rights_id',
    'screenshots':     'td.screenshots AS screenshots',
    'size':            'td.size AS size',
    'version':         'td.version AS version',
    'ncaKey':          'td.nca_key AS nca_key',
    'ids':             'td.ids AS ids',
}


def _title_cols(driver: str, sel: "Selection") -> str:
    """Build the title SELECT list, projecting only the columns the client
    actually selected. Drives title_id/source from `ot` for owned-driven
    queries so unrecognized titles (no titledb match) still surface."""
    cols = []
    if driver == "owned":
        cols.append("UPPER(ot.title_id) AS title_id")
        cols.append("COALESCE(td.source, 'unrecognized') AS source")
    else:
        cols.append("UPPER(td.id) AS title_id")
        cols.append("td.source AS source")
    for gql_name, sql_expr in _TITLE_COL_MAP.items():
        if sel.has(gql_name):
            cols.append(sql_expr)
    if sel.has("ownership"):
        cols.append("ot.id AS ownership_pk")
        cols.append("ot.have_base AS have_base")
        cols.append("ot.up_to_date AS up_to_date")
        cols.append("ot.complete AS complete")
    return ", ".join(cols)

_FILE_COLS = """
f.id AS id, f.library_id AS library_id, f.filepath AS filepath,
f.folder AS folder, f.filename AS filename, f.extension AS extension,
f.size AS size, f.compressed AS compressed, f.multicontent AS multicontent,
f.nb_content AS nb_content, f.download_count AS download_count,
f.identified AS identified, f.identification_type AS identification_type,
f.identification_error AS identification_error,
f.identification_attempts AS identification_attempts,
f.organized AS organized, f.mtime AS mtime
"""

# `titledb.titles` already holds one row per id, merged across the metadata sources by
# titledb.store, so this join needs no dedup predicate. `td.source` is the
# highest-priority source that contributed a field.
_TITLEDB = "titledb.titles"

# Is the highest known version of this app id owned? Correlated so it reads the same
# whether or not the outer query groups, and NULL-safe: an app with nothing owned is
# behind, not unknown.
_LATEST_VERSION_OWNED = """(
    SELECT IFNULL(MAX(CASE WHEN a2.owned THEN CAST(a2.app_version AS INTEGER) END), -1)
           >= MAX(CAST(a2.app_version AS INTEGER))
    FROM apps a2 WHERE a2.app_id = a.app_id
)"""

# One text box, several ids and names: the app's own metadata (a DLC's name), its
# parent title's name, and either id. TitleFilter/AppFilter AND their fields, so an
# OR across columns has to be its own argument.
_APP_SEARCH = """(
    tda.name LIKE :search COLLATE NOCASE
    OR td.name LIKE :search COLLATE NOCASE
    OR a.app_id LIKE :search COLLATE NOCASE
    OR ot.title_id LIKE :search COLLATE NOCASE
)"""

_TITLE_SEARCH = """(
    td.name LIKE :search COLLATE NOCASE OR td.id LIKE :search COLLATE NOCASE
)"""


# ------------- builders -------------

def _build_title(row, *, with_apps: bool, with_files: bool) -> Title:
    # `m.get(col)` returns None for columns the resolver didn't project,
    # which lets `_title_cols` skip unselected columns without us crashing
    # on row-attribute access here.
    m = row._mapping
    ownership = None
    if 'ownership_pk' in m and m['ownership_pk'] is not None:
        ownership = Ownership(
            have_base=bool(m.get('have_base')),
            up_to_date=bool(m.get('up_to_date')),
            complete=bool(m.get('complete')),
        )
    return Title(
        title_id=strawberry.ID((m.get('title_id') or "").upper()),
        source=m.get('source') or "titledb",
        name=m.get('name'),
        banner_url=m.get('banner_url'),
        icon_url=m.get('icon_url'),
        front_box_art=m.get('front_box_art'),
        description=m.get('description'),
        intro=m.get('intro'),
        developer=m.get('developer'),
        publisher=m.get('publisher'),
        release_date=m.get('release_date'),
        category=decode_json_list(m.get('category')),
        is_demo=m.get('is_demo'),
        nsu_id=m.get('nsu_id'),
        number_of_players=m.get('number_of_players'),
        parent_id=m.get('parent_id'),
        rank=m.get('rank'),
        rating=m.get('rating'),
        rating_content=decode_json_list(m.get('rating_content')),
        region=m.get('region'),
        regions=decode_json_list(m.get('regions')),
        languages=decode_json_list(m.get('languages')),
        language=m.get('language'),
        rights_id=m.get('rights_id'),
        screenshots=decode_json_list(m.get('screenshots')),
        size=m.get('size'),
        version=m.get('version'),
        nca_key=m.get('nca_key'),
        ids=decode_json_list(m.get('ids')),
        ownership=ownership,
        apps_loaded=[] if with_apps else None,
    )


def _build_file(row, *, include_filepath: bool) -> File:
    return File(
        id=strawberry.ID(str(row.id)),
        library_id=row.library_id,
        filepath=row.filepath if include_filepath else None,
        folder=row.folder,
        filename=row.filename,
        extension=row.extension,
        size=row.size,
        compressed=bool(row.compressed),
        multicontent=bool(row.multicontent),
        nb_content=row.nb_content or 0,
        download_count=row.download_count or 0,
        identified=bool(row.identified),
        identification_type=row.identification_type,
        identification_error=row.identification_error,
        identification_attempts=row.identification_attempts or 0,
        organized=bool(row.organized),
        mtime=row.mtime,
    )


# ------------- batch helpers -------------
#
# Each *with_xxx* flag mirrors a sub-selection in the GraphQL query: if the
# client didn't ask for that nested field, we skip the SQL round-trip entirely.

def _load_apps_for_titles(
    title_ids_uc: List[str],
    app_filter: Optional[AppFilter],
    *,
    with_files: bool,
    with_titledb: bool,
    with_versions: bool = False,
    with_files_apps: bool = False,
    titledb_sel: "Selection",
) -> Dict[str, List[App]]:
    """Return apps keyed by uppercase title_id.

    `App.title` is deliberately not hydrated here: the caller already is the title."""
    if not title_ids_uc:
        return {}
    params = {f"t_{i}": tid for i, tid in enumerate(title_ids_uc)}
    placeholders = ",".join(f":t_{i}" for i in range(len(title_ids_uc)))
    extra = build_clauses(app_filter, APP_FIELDS, params)
    extra_sql = (" AND " + " AND ".join(extra)) if extra else ""
    sql = f"""
    SELECT a.id AS id, a.app_id AS app_id, a.app_version AS app_version,
           a.app_type AS app_type, a.owned AS owned,
           a.release_date AS release_date, ot.title_id AS tuc
    FROM apps a JOIN main.titles ot ON ot.id = a.title_id
    WHERE ot.title_id IN ({placeholders}){extra_sql}
    ORDER BY a.app_id, CAST(a.app_version AS INTEGER), a.id
    """
    rows = db.session.execute(text(sql), params).all()
    out: Dict[str, List[App]] = {tid: [] for tid in title_ids_uc}
    all_apps: List[App] = []
    app_pks = []
    apps_by_pk: Dict[int, App] = {}
    for r in rows:
        a = App(
            id=strawberry.ID(str(r.id)),
            title_id=r.tuc,
            app_id=r.app_id,
            app_version=r.app_version,
            app_type=r.app_type,
            owned=bool(r.owned),
            release_date=r.release_date,
            files_loaded=[] if with_files else None,
        )
        out.setdefault(r.tuc, []).append(a)
        all_apps.append(a)
        if with_files:
            apps_by_pk[int(r.id)] = a
            app_pks.append(int(r.id))

    if with_files and app_pks:
        _hydrate_app_files(app_pks, apps_by_pk, with_apps=with_files_apps)
    if with_titledb and all_apps:
        _hydrate_apps_titledb(all_apps, titledb_sel)
    if with_versions and all_apps:
        _hydrate_app_versions(all_apps)
    return out


def _hydrate_app_files(
    app_pks: List[int], apps_by_pk: Dict[int, App], *, with_apps: bool,
) -> None:
    """Populate .files_loaded on each app in apps_by_pk, including back-link apps."""
    placeholders = ",".join(f":a_{i}" for i in range(len(app_pks)))
    params = {f"a_{i}": pk for i, pk in enumerate(app_pks)}
    sql = f"""
    SELECT af.app_id AS pk, {_FILE_COLS}
    FROM app_files af JOIN files f ON f.id = af.file_id
    WHERE af.app_id IN ({placeholders})
    """
    rows = db.session.execute(text(sql), params).all()
    files_by_pk: Dict[int, File] = {}
    for r in rows:
        app = apps_by_pk.get(int(r.pk))
        if app is None:
            continue
        f = _build_file(r, include_filepath=True)
        f.apps_loaded = [] if with_apps else None
        app.files_loaded.append(f)
        files_by_pk[int(r.id)] = f
    if with_apps and files_by_pk:
        _hydrate_file_apps(list(files_by_pk.keys()), files_by_pk)


def _hydrate_file_apps(
    file_pks: List[int], files_by_pk: Dict[int, "File"], *,
    with_titledb: bool = False,
    titledb_sel: Optional["Selection"] = None,
    with_title: bool = False,
    title_sel: Optional["Selection"] = None,
    with_versions: bool = False,
) -> None:
    """Populate .apps_loaded on each file (m2m back-direction across app_files).

    titledb, title and versions are only hydrated for the top-level `files` query;
    apps reached as the back-link of an app's own files never carry them. `title` is
    what names the game behind an UPDATE file, whose own titledb row usually does not
    exist."""
    placeholders = ",".join(f":f_{i}" for i in range(len(file_pks)))
    params = {f"f_{i}": pk for i, pk in enumerate(file_pks)}
    sql = f"""
    SELECT af.file_id AS pk,
           a.id AS id, a.app_id AS app_id, a.app_version AS app_version,
           a.app_type AS app_type, a.owned AS owned,
           a.release_date AS release_date, ot.title_id AS title_id
    FROM app_files af JOIN apps a ON a.id = af.app_id
    JOIN main.titles ot ON ot.id = a.title_id
    WHERE af.file_id IN ({placeholders})
    ORDER BY a.id
    """
    rows = db.session.execute(text(sql), params).all()
    backlinked: List[App] = []
    for r in rows:
        f = files_by_pk.get(int(r.pk))
        if f is None:
            continue
        a = App(
            id=strawberry.ID(str(r.id)),
            title_id=r.title_id,
            app_id=r.app_id,
            app_version=r.app_version,
            app_type=r.app_type,
            owned=bool(r.owned),
            release_date=r.release_date,
            files_loaded=None,  # don't recursively load files for back-linked apps
        )
        f.apps_loaded.append(a)
        backlinked.append(a)
    if with_titledb and backlinked:
        _hydrate_apps_titledb(backlinked, titledb_sel)
    if with_title and backlinked:
        _hydrate_apps_title(backlinked, title_sel)
    if with_versions and backlinked:
        _hydrate_app_versions(backlinked)


def _hydrate_apps_titledb(apps: List[App], sel: "Selection") -> None:
    """For each App, attach its own titledb entry keyed by app_id (uppercase).
    Most useful for DLC apps (their titledb row is their own metadata, not the
    parent title's). Single batch SELECT, distinct app_ids only."""
    distinct_ids = list({a.app_id for a in apps if a.app_id})
    if not distinct_ids:
        return
    params = {f"i_{i}": x for i, x in enumerate(distinct_ids)}
    placeholders = ",".join(f":i_{i}" for i in range(len(distinct_ids)))
    sql = f"""
    SELECT {_title_cols('titledb', sel)}
    FROM {_TITLEDB} td
    LEFT JOIN main.titles ot ON ot.title_id = td.id
    WHERE td.id IN ({placeholders})
    """
    rows = db.session.execute(text(sql), params).all()
    by_id: Dict[str, Title] = {}
    for r in rows:
        by_id[(r.title_id or "").upper()] = _build_title(
            r, with_apps=False, with_files=False
        )
    for a in apps:
        a.titledb_loaded = by_id.get(a.app_id)


def _hydrate_apps_title(apps: List[App], sel: "Selection") -> None:
    """Attach each app's parent title, so a card can show the title's name and
    ownership without a second round trip. Driven from main.titles, so a title with
    no titledb match still comes back (with null metadata)."""
    distinct_ids = list({a.title_id for a in apps if a.title_id})
    if not distinct_ids:
        return
    params = {f"p_{i}": x for i, x in enumerate(distinct_ids)}
    placeholders = ",".join(f":p_{i}" for i in range(len(distinct_ids)))
    sql = f"""
    SELECT {_title_cols('owned', sel)}
    FROM main.titles ot
    LEFT JOIN {_TITLEDB} td ON td.id = ot.title_id
    WHERE ot.title_id IN ({placeholders})
    """
    rows = db.session.execute(text(sql), params).all()
    by_id = {(r.title_id or "").upper(): _build_title(r, with_apps=False, with_files=False)
             for r in rows}
    for a in apps:
        a.title = by_id.get((a.title_id or "").upper())


def _hydrate_app_versions(apps: List[App]) -> None:
    """Attach the version history behind each app.

    A Switch update ships as its own app id, so the versions of a BASE app are its
    title's UPDATE apps; for every other app they are the rows sharing its app id.
    Two batched queries, one per keying."""
    base_apps = [a for a in apps if a.app_type == APP_TYPE_BASE]
    other_apps = [a for a in apps if a.app_type != APP_TYPE_BASE]

    if base_apps:
        title_ids = list({a.title_id for a in base_apps if a.title_id})
        params = {f"v_{i}": x for i, x in enumerate(title_ids)}
        placeholders = ",".join(f":v_{i}" for i in range(len(title_ids)))
        sql = f"""
        SELECT ot.title_id AS key, a.app_version AS app_version,
               a.owned AS owned, a.release_date AS release_date
        FROM apps a JOIN main.titles ot ON ot.id = a.title_id
        WHERE ot.title_id IN ({placeholders}) AND a.app_type = :upd
        ORDER BY CAST(a.app_version AS INTEGER)
        """
        by_title = _group_versions(sql, dict(params, upd=APP_TYPE_UPD))
        for a in base_apps:
            a.versions = by_title.get(a.title_id, [])

    if other_apps:
        app_ids = list({a.app_id for a in other_apps if a.app_id})
        params = {f"v_{i}": x for i, x in enumerate(app_ids)}
        placeholders = ",".join(f":v_{i}" for i in range(len(app_ids)))
        sql = f"""
        SELECT a.app_id AS key, a.app_version AS app_version,
               a.owned AS owned, a.release_date AS release_date
        FROM apps a
        WHERE a.app_id IN ({placeholders})
        ORDER BY CAST(a.app_version AS INTEGER)
        """
        by_app = _group_versions(sql, params)
        for a in other_apps:
            a.versions = by_app.get(a.app_id, [])


def _group_versions(sql: str, params: dict) -> Dict[str, List[AppVersion]]:
    out: Dict[str, List[AppVersion]] = {}
    for r in db.session.execute(text(sql), params).all():
        try:
            version = int(r.app_version)
        except (TypeError, ValueError):
            continue
        out.setdefault(r.key, []).append(
            AppVersion(version=version, owned=bool(r.owned), release_date=r.release_date))
    return out


# ------------- top-level resolvers -------------

def resolve_title(title_id: str, ctx: GraphQLContext, info) -> Optional[Title]:
    if not ctx.can_shop:
        return None
    tid = title_id.upper()

    sel = Selection.from_info(info)
    apps_sel = sel.child("apps")
    files_sel = apps_sel.child("files")
    apps_titledb_sel = apps_sel.child("titledb")
    want_apps = ctx.can_shop and sel.has("apps")
    want_apps_files = ctx.can_admin and want_apps and apps_sel.has("files")
    want_apps_titledb = want_apps and apps_sel.has("titledb")
    want_apps_versions = want_apps and apps_sel.has("versions")
    want_apps_files_apps = want_apps_files and files_sel.has("apps")

    # Drive from main.titles when this title is owned (so unrecognized titles surface),
    # otherwise drive from titledb. Both stores keep title_id in uppercase.
    sql = f"""
    SELECT {_title_cols('owned', sel)}
    FROM main.titles ot
    LEFT JOIN {_TITLEDB} td ON td.id = ot.title_id
    WHERE ot.title_id = :tid
    UNION ALL
    SELECT {_title_cols('titledb', sel)}
    FROM {_TITLEDB} td
    LEFT JOIN main.titles ot ON ot.title_id = td.id
    WHERE td.id = :tid AND ot.id IS NULL
    LIMIT 1
    """
    row = db.session.execute(text(sql), {"tid": tid}).first()
    if not row:
        return None
    title = _build_title(row, with_apps=want_apps, with_files=want_apps_files)
    if want_apps:
        apps_map = _load_apps_for_titles(
            [tid], None,
            with_files=want_apps_files,
            with_titledb=want_apps_titledb,
            with_versions=want_apps_versions,
            with_files_apps=want_apps_files_apps,
            titledb_sel=apps_titledb_sel,
        )
        title.apps_loaded = apps_map.get(tid, [])
    return title


def resolve_titles(*, owned: Optional[bool], filter: Optional[TitleFilter],
                    search: Optional[str] = None, order_by: str = "id",
                    page: int, page_size: int, ctx: GraphQLContext, info) -> TitleConnection:
    if not ctx.can_shop:
        return TitleConnection(total=0, items=[])
    page = max(1, page)
    page_size = max(1, min(page_size, 500))

    sel = Selection.from_info(info)
    items_sel = sel.child("items")
    apps_sel = items_sel.child("apps")
    files_sel = apps_sel.child("files")
    apps_titledb_sel = apps_sel.child("titledb")
    want_items = sel.has("items")
    want_apps = want_items and items_sel.has("apps")
    want_apps_files = ctx.can_admin and want_apps and apps_sel.has("files")
    want_apps_titledb = want_apps and apps_sel.has("titledb")
    want_apps_versions = want_apps and apps_sel.has("versions")
    want_apps_files_apps = want_apps_files and files_sel.has("apps")
    want_total = sel.has("total")

    params: dict = {}
    where = build_clauses(filter, TITLE_FIELDS, params)

    if owned is True:
        # Driver = main.titles, LEFT JOIN titledb. Unrecognized titles surface
        # with null titledb fields rather than being dropped.
        from_sql = (
            f"FROM main.titles ot "
            f"LEFT JOIN {_TITLEDB} td ON td.id = ot.title_id"
        )
        order_by_sql = "ot.title_id"
        cols = _title_cols("owned", items_sel)
    elif owned is False:
        from_sql = (
            f"FROM {_TITLEDB} td "
            f"LEFT JOIN main.titles ot ON ot.title_id = td.id"
        )
        where.append("ot.id IS NULL")
        order_by_sql = "td.id"
        cols = _title_cols("titledb", items_sel)
    else:
        from_sql = (
            f"FROM {_TITLEDB} td "
            f"LEFT JOIN main.titles ot ON ot.title_id = td.id"
        )
        order_by_sql = "td.id"
        cols = _title_cols("titledb", items_sel)

    if search:
        params["search"] = f"%{search}%"
        where.append(_TITLE_SEARCH)
    if order_by == "name":
        order_by_sql = f"td.name IS NULL, td.name COLLATE NOCASE, {order_by_sql}"

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    total = 0
    if want_total:
        count_sql = f"SELECT COUNT(*) {from_sql}{where_sql}"
        total = db.session.execute(text(count_sql), params).scalar() or 0

    if not want_items:
        return TitleConnection(total=int(total), items=[])

    page_sql = f"""
    SELECT {cols}
    {from_sql}
    {where_sql}
    ORDER BY {order_by_sql}
    LIMIT :limit OFFSET :offset
    """
    page_params = dict(params, limit=page_size, offset=(page - 1) * page_size)
    rows = db.session.execute(text(page_sql), page_params).all()

    titles = [_build_title(r, with_apps=want_apps, with_files=want_apps_files)
              for r in rows]
    title_ids_uc = [(r.title_id or "").upper() for r in rows]

    if want_apps and title_ids_uc:
        apps_map = _load_apps_for_titles(
            title_ids_uc, None,
            with_files=want_apps_files,
            with_titledb=want_apps_titledb,
            with_versions=want_apps_versions,
            with_files_apps=want_apps_files_apps,
            titledb_sel=apps_titledb_sel,
        )
        for t, tid in zip(titles, title_ids_uc):
            t.apps_loaded = apps_map.get(tid, [])

    return TitleConnection(total=int(total), items=titles)


def resolve_apps(*, owned: Optional[bool], app_type: Optional[List[str]],
                  filter: Optional[AppFilter],
                  up_to_date: Optional[bool] = None, complete: Optional[bool] = None,
                  search: Optional[str] = None, order_by: str = "id",
                  group_by_app_id: bool = False,
                  page: int, page_size: int, ctx: GraphQLContext, info,
                  only_pk: Optional[str] = None) -> AppConnection:
    if not ctx.can_shop:
        return AppConnection(total=0, items=[])
    page = max(1, page)
    page_size = max(1, min(page_size, 1000))

    # Reached as `app(id:)` the selection set is the item's own fields, not a
    # connection's `{total, items}`, so the nested gates hang off it directly.
    sel = Selection.from_info(info)
    items_sel = sel if only_pk is not None else sel.child("items")
    want_items = True if only_pk is not None else sel.has("items")
    want_total = False if only_pk is not None else sel.has("total")

    files_sel = items_sel.child("files")
    titledb_sel = items_sel.child("titledb")
    title_sel = items_sel.child("title")
    want_files = ctx.can_admin and want_items and items_sel.has("files")
    want_titledb = want_items and items_sel.has("titledb")
    want_title = want_items and items_sel.has("title")
    want_versions = want_items and items_sel.has("versions")
    want_files_apps = want_files and files_sel.has("apps")

    params: dict = {}
    where = build_clauses(filter, APP_FIELDS, params)
    if only_pk is not None:
        params["only_pk"] = only_pk
        where.append("a.id = :only_pk")
    having: List[str] = []
    if owned is not None:
        params["owned_arg"] = 1 if owned else 0
        # Grouped, "owned" is a property of the app id as a whole: any version of it.
        (having if group_by_app_id else where).append(
            f"{'MAX(a.owned)' if group_by_app_id else 'a.owned'} = :owned_arg")
    if app_type:
        params.update({f"at_{i}": v for i, v in enumerate(app_type)})
        where.append(f"a.app_type IN ({','.join(f':at_{i}' for i in range(len(app_type)))})")
    if up_to_date is not None:
        params["utd_arg"] = 1 if up_to_date else 0
        where.append(f"""(CASE WHEN a.app_type = '{APP_TYPE_BASE}' THEN ot.up_to_date
                          ELSE {_LATEST_VERSION_OWNED} END) = :utd_arg""")
    if complete is not None:
        # Only a title knows whether its DLC set is complete, so this never matches a
        # DLC or UPDATE app - the same rows the UI's completion filter drops today.
        params["cpl_arg"] = 1 if complete else 0
        where.append(f"a.app_type = '{APP_TYPE_BASE}' AND ot.complete = :cpl_arg")
    if search:
        params["search"] = f"%{search}%"
        where.append(_APP_SEARCH)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    group_sql = " GROUP BY a.app_id" if group_by_app_id else ""
    having_sql = (" HAVING " + " AND ".join(having)) if having else ""
    from_sql = f"""
    FROM apps a
    JOIN main.titles ot ON ot.id = a.title_id
    LEFT JOIN {_TITLEDB} td ON td.id = ot.title_id
    LEFT JOIN {_TITLEDB} tda ON tda.id = a.app_id
    """

    total = 0
    if want_total:
        counted = "COUNT(*)" if not group_by_app_id else "COUNT(DISTINCT a.app_id)"
        if having:
            # A HAVING on the group can only be counted by counting the groups back.
            count_sql = (f"SELECT COUNT(*) FROM (SELECT a.app_id {from_sql}"
                         f"{where_sql}{group_sql}{having_sql})")
        else:
            count_sql = f"SELECT {counted} {from_sql}{where_sql}"
        total = db.session.execute(text(count_sql), params).scalar() or 0

    if not want_items:
        return AppConnection(total=int(total), items=[])

    # Grouped, the row stands for every version of the app id: its highest version,
    # owned if any version is. The bare columns are constant within an app id.
    version_col = ("MAX(CAST(a.app_version AS INTEGER))" if group_by_app_id else "a.app_version")
    owned_col = "MAX(a.owned)" if group_by_app_id else "a.owned"
    order_sql = ("td.name IS NULL, td.name COLLATE NOCASE, a.app_id"
                 if order_by == "name" else "a.id")
    page_sql = f"""
    SELECT MIN(a.id) AS id, a.app_id AS app_id, {version_col} AS app_version,
           a.app_type AS app_type, {owned_col} AS owned,
           MAX(a.release_date) AS release_date, ot.title_id AS title_id
    {from_sql}
    {where_sql}{group_sql}{having_sql}
    ORDER BY {order_sql}
    LIMIT :limit OFFSET :offset
    """ if group_by_app_id else f"""
    SELECT a.id AS id, a.app_id AS app_id, a.app_version AS app_version,
           a.app_type AS app_type, a.owned AS owned,
           a.release_date AS release_date, ot.title_id AS title_id
    {from_sql}
    {where_sql}
    ORDER BY {order_sql}
    LIMIT :limit OFFSET :offset
    """
    page_params = dict(params, limit=page_size, offset=(page - 1) * page_size)
    rows = db.session.execute(text(page_sql), page_params).all()

    apps_by_pk: Dict[int, App] = {}
    items: List[App] = []
    for r in rows:
        a = App(
            id=strawberry.ID(str(r.id)),
            title_id=r.title_id,
            app_id=r.app_id,
            app_version=str(r.app_version),
            app_type=r.app_type,
            owned=bool(r.owned),
            release_date=r.release_date,
            files_loaded=[] if want_files else None,
        )
        items.append(a)
        if want_files:
            apps_by_pk[int(r.id)] = a

    if want_files and apps_by_pk:
        _hydrate_app_files(list(apps_by_pk.keys()), apps_by_pk, with_apps=want_files_apps)
    if want_titledb and items:
        _hydrate_apps_titledb(items, titledb_sel)
    if want_title and items:
        _hydrate_apps_title(items, title_sel)
    if want_versions and items:
        _hydrate_app_versions(items)

    return AppConnection(total=int(total), items=items)


def resolve_files(*, filter: Optional[FileFilter], page: int, page_size: int,
                   ctx: GraphQLContext, info,
                   only_pk: Optional[str] = None) -> FileConnection:
    if not ctx.can_admin:
        return FileConnection(total=0, items=[])
    page = max(1, page)
    page_size = max(1, min(page_size, 1000))

    # As in resolve_apps: `file(id:)` selects the item's fields directly.
    sel = Selection.from_info(info)
    items_sel = sel if only_pk is not None else sel.child("items")
    want_items = True if only_pk is not None else sel.has("items")
    want_total = False if only_pk is not None else sel.has("total")

    apps_sel = items_sel.child("apps")
    apps_titledb_sel = apps_sel.child("titledb")
    apps_title_sel = apps_sel.child("title")
    want_apps = want_items and items_sel.has("apps")
    want_apps_titledb = want_apps and apps_sel.has("titledb")
    want_apps_title = want_apps and apps_sel.has("title")
    want_apps_versions = want_apps and apps_sel.has("versions")
    want_library = want_items and items_sel.has("library")

    params: dict = {}
    where = build_clauses(filter, FILE_FIELDS, params)
    if only_pk is not None:
        params["only_pk"] = only_pk
        where.append("f.id = :only_pk")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    total = 0
    if want_total:
        count_sql = f"SELECT COUNT(*) FROM files f{where_sql}"
        total = db.session.execute(text(count_sql), params).scalar() or 0

    if not want_items:
        return FileConnection(total=int(total), items=[])

    page_sql = f"""
    SELECT {_FILE_COLS}
    FROM files f
    {where_sql}
    ORDER BY f.id
    LIMIT :limit OFFSET :offset
    """
    page_params = dict(params, limit=page_size, offset=(page - 1) * page_size)
    rows = db.session.execute(text(page_sql), page_params).all()

    items: List[File] = []
    files_by_pk: Dict[int, File] = {}
    for r in rows:
        f = _build_file(r, include_filepath=True)
        f.apps_loaded = [] if want_apps else None
        items.append(f)
        if want_apps:
            files_by_pk[int(r.id)] = f
    if want_apps and files_by_pk:
        _hydrate_file_apps(
            list(files_by_pk.keys()), files_by_pk,
            with_titledb=want_apps_titledb,
            titledb_sel=apps_titledb_sel,
            with_title=want_apps_title,
            title_sel=apps_title_sel,
            with_versions=want_apps_versions,
        )
    if want_library and items:
        _hydrate_file_libraries(items)
    return FileConnection(total=int(total), items=items)


# ------------- libraries, tasks, stats -------------

def _hydrate_file_libraries(files: List[File]) -> None:
    """Attach each file's library root. The table holds a handful of rows, so read it
    whole rather than building an IN list."""
    by_id = {int(r.id): Library(id=strawberry.ID(str(r.id)), path=r.path,
                                last_scan=r.last_scan.isoformat() if r.last_scan else None)
             for r in db.session.execute(text(
                 "SELECT id, path, last_scan FROM libraries")).all()}
    for f in files:
        f.library_loaded = by_id.get(f.library_id)


def resolve_libraries(*, ctx: GraphQLContext, info) -> List[Library]:
    if not ctx.can_admin:
        return []
    rows = db.session.execute(text(
        "SELECT id, path, last_scan FROM libraries ORDER BY id")).all()
    return [Library(id=strawberry.ID(str(r.id)), path=r.path,
                    last_scan=r.last_scan.isoformat() if r.last_scan else None)
            for r in rows]


_TASK_COLS = """
t.id AS id, t.parent_id AS parent_id, t.task_name AS task_name, t.status AS status,
t.completion_pct AS completion_pct, t.exit_code AS exit_code,
t.error_message AS error_message, t.created_at AS created_at,
t.started_at AS started_at, t.completed_at AS completed_at, t.run_after AS run_after,
t.input_json AS input_json, t.output_json AS output_json
"""


def _build_task(row, *, with_children: bool) -> Task:
    m = row._mapping
    return Task(
        id=strawberry.ID(str(m['id'])),
        parent_id=strawberry.ID(str(m['parent_id'])) if m['parent_id'] is not None else None,
        task_name=m['task_name'],
        status=m['status'],
        completion_pct=m['completion_pct'] or 0,
        exit_code=m['exit_code'],
        error_message=m['error_message'],
        created_at=m['created_at'],
        started_at=m['started_at'],
        completed_at=m['completed_at'],
        run_after=m['run_after'],
        input=m['input_json'],
        output=m['output_json'],
        children_loaded=[] if with_children else None,
    )


def _hydrate_task_children(tasks: List[Task]) -> None:
    """One batched SELECT for the sub-tasks of every task on the page."""
    parent_pks = [int(t.id) for t in tasks]
    params = {f"p_{i}": pk for i, pk in enumerate(parent_pks)}
    placeholders = ",".join(f":p_{i}" for i in range(len(parent_pks)))
    rows = db.session.execute(text(f"""
    SELECT {_TASK_COLS} FROM tasks t
    WHERE t.parent_id IN ({placeholders})
    ORDER BY t.id
    """), params).all()
    by_parent: Dict[int, List[Task]] = {}
    for r in rows:
        by_parent.setdefault(int(r.parent_id), []).append(
            _build_task(r, with_children=False))
    for t in tasks:
        t.children_loaded = by_parent.get(int(t.id), [])


def resolve_tasks(*, status: Optional[str], task_name: Optional[str],
                   include_children: bool, limit: int,
                   ctx: GraphQLContext, info) -> List[Task]:
    """Top-level tasks newest first, mirroring what /api/tasks lists."""
    if not ctx.can_admin:
        return []
    limit = max(1, min(limit, 500))
    sel = Selection.from_info(info)

    params: dict = {"limit": limit}
    where = [] if include_children else ["t.parent_id IS NULL"]
    if status:
        params["status"] = status
        where.append("t.status = :status")
    if task_name:
        params["task_name"] = task_name
        where.append("t.task_name = :task_name")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    rows = db.session.execute(text(f"""
    SELECT {_TASK_COLS} FROM tasks t{where_sql}
    ORDER BY t.created_at DESC, t.id DESC
    LIMIT :limit
    """), params).all()

    want_children = sel.has("children")
    tasks = [_build_task(r, with_children=want_children) for r in rows]
    if want_children and tasks:
        _hydrate_task_children(tasks)
    return tasks


def resolve_task(task_id: str, ctx: GraphQLContext, info) -> Optional[Task]:
    if not ctx.can_admin:
        return None
    sel = Selection.from_info(info)
    row = db.session.execute(text(f"SELECT {_TASK_COLS} FROM tasks t WHERE t.id = :id"),
                             {"id": task_id}).first()
    if not row:
        return None
    want_children = sel.has("children")
    task = _build_task(row, with_children=want_children)
    if want_children:
        _hydrate_task_children([task])
    return task


def resolve_stats(*, ctx: GraphQLContext, info) -> LibraryStats:
    """Library-wide aggregates. Every branch is selection-gated, so a dashboard tile
    asking for one number runs one aggregate rather than all of them."""
    if not ctx.can_shop:
        return LibraryStats()
    sel = Selection.from_info(info)
    stats = LibraryStats()

    # Files: one scan covers every scalar file aggregate.
    if any(sel.has(f) for f in
           ("totalFiles", "totalSize", "identifiedFiles", "unidentifiedFiles")):
        if ctx.can_admin:
            r = db.session.execute(text("""
            SELECT COUNT(*), COALESCE(SUM(size), 0), COALESCE(SUM(identified), 0)
            FROM files""")).first()
            stats.total_files, stats.total_size, stats.identified_files = (
                int(r[0]), int(r[1]), int(r[2]))
            stats.unidentified_files = stats.total_files - stats.identified_files

    if any(sel.has(f) for f in ("totalTitles", "ownedTitles")):
        stats.total_titles = int(db.session.execute(text(
            "SELECT COUNT(*) FROM titledb.titles")).scalar() or 0)
        stats.owned_titles = int(db.session.execute(text(
            "SELECT COUNT(*) FROM main.titles WHERE have_base = 1")).scalar() or 0)

    if any(sel.has(f) for f in ("totalApps", "ownedApps")):
        r = db.session.execute(text(
            "SELECT COUNT(*), COALESCE(SUM(owned), 0) FROM apps")).first()
        stats.total_apps, stats.owned_apps = int(r[0]), int(r[1])

    if ctx.can_admin and sel.has("filesByExtension"):
        stats.files_by_extension = _count_by(
            "SELECT COALESCE(extension, ''), COUNT(*), COALESCE(SUM(size), 0) "
            "FROM files GROUP BY extension ORDER BY COUNT(*) DESC")

    if sel.has("appsByType"):
        stats.apps_by_type = _count_by(
            "SELECT app_type, COUNT(*), 0 FROM apps GROUP BY app_type ORDER BY app_type")

    if ctx.can_admin and sel.has("filesByLibrary"):
        stats.files_by_library = _count_by("""
        SELECT l.path, COUNT(f.id), COALESCE(SUM(f.size), 0)
        FROM libraries l LEFT JOIN files f ON f.library_id = l.id
        GROUP BY l.id, l.path ORDER BY l.id""")

    return stats


def _count_by(sql: str) -> List[CountByKey]:
    return [CountByKey(key=str(r[0]), count=int(r[1]), size=int(r[2]))
            for r in db.session.execute(text(sql)).all()]


def resolve_app(app_pk: str, ctx: GraphQLContext, info) -> Optional[App]:
    """Single app by primary key. Delegates to the list resolver so every nested field
    hydrates exactly as it does under `apps`."""
    if not ctx.can_shop:
        return None
    conn = resolve_apps(
        owned=None, app_type=None, filter=None, up_to_date=None, complete=None,
        search=None, order_by="id", group_by_app_id=False,
        page=1, page_size=1, ctx=ctx, info=info, only_pk=app_pk,
    )
    return conn.items[0] if conn.items else None


def resolve_file(file_pk: str, ctx: GraphQLContext, info) -> Optional[File]:
    """Single file by primary key, hydrated as under `files`."""
    if not ctx.can_admin:
        return None
    conn = resolve_files(filter=None, page=1, page_size=1, ctx=ctx, info=info,
                         only_pk=file_pk)
    return conn.items[0] if conn.items else None
