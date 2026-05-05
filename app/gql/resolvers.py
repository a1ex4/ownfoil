"""Query resolvers for the GraphQL API."""
from typing import Dict, List, Optional

import strawberry
from sqlalchemy import text

from db import db

from .context import GraphQLContext
from .filters import (
    AppFilter, FileFilter, TitleFilter,
    APP_FIELDS, FILE_FIELDS, TITLE_FIELDS, build_clauses,
)
from .types import (
    App, AppConnection, File, FileConnection, Ownership,
    Title, TitleConnection, Version, decode_json_list,
)


# ------------- column lists -------------

_TITLE_FIELDS = """
td.name          AS name,
td.banner_url    AS banner_url,
td.icon_url      AS icon_url,
td.front_box_art AS front_box_art,
td.description   AS description,
td.intro         AS intro,
td.developer     AS developer,
td.publisher     AS publisher,
td.release_date  AS release_date,
td.category      AS category,
td.is_demo       AS is_demo,
td.nsu_id        AS nsu_id,
td.number_of_players AS number_of_players,
td.parent_id     AS parent_id,
td.rank          AS rank,
td.rating        AS rating,
td.rating_content AS rating_content,
td.region        AS region,
td.regions       AS regions,
td.languages     AS languages,
td.language      AS language,
td.rights_id     AS rights_id,
td.screenshots   AS screenshots,
td.size          AS size,
td.version       AS version,
td.nca_key       AS nca_key,
td.ids           AS ids,
ot.have_base     AS have_base,
ot.up_to_date    AS up_to_date,
ot.complete      AS complete,
ot.id            AS ownership_pk
"""


def _title_cols(driver: str) -> str:
    """SELECT clause: drive title_id and source from `ot` for owned-driven queries
    so unrecognized titles (no titledb match) still surface."""
    if driver == "owned":
        return f"UPPER(ot.title_id) AS title_id, COALESCE(td.source, 'unrecognized') AS source, {_TITLE_FIELDS}"
    return f"UPPER(td.id) AS title_id, td.source AS source, {_TITLE_FIELDS}"

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

# Subquery returning a single row per titledb id, custom-source preferred.
_TITLEDB_DEDUPED = """(
    SELECT t.* FROM titledb.titles t
    WHERE t.source = 'custom'
       OR NOT EXISTS (
            SELECT 1 FROM titledb.titles c
            WHERE c.id = t.id AND c.source = 'custom'
       )
)"""


# ------------- builders -------------

def _build_title(row, *, with_apps: bool, with_files: bool) -> Title:
    has_ownership = row.ownership_pk is not None
    return Title(
        title_id=strawberry.ID((row.title_id or "").upper()),
        source=row.source or "upstream",
        name=row.name,
        banner_url=row.banner_url,
        icon_url=row.icon_url,
        front_box_art=row.front_box_art,
        description=row.description,
        intro=row.intro,
        developer=row.developer,
        publisher=row.publisher,
        release_date=row.release_date,
        category=decode_json_list(row.category),
        is_demo=row.is_demo,
        nsu_id=row.nsu_id,
        number_of_players=row.number_of_players,
        parent_id=row.parent_id,
        rank=row.rank,
        rating=row.rating,
        rating_content=decode_json_list(row.rating_content),
        region=row.region,
        regions=decode_json_list(row.regions),
        languages=decode_json_list(row.languages),
        language=row.language,
        rights_id=row.rights_id,
        screenshots=decode_json_list(row.screenshots),
        size=row.size,
        version=row.version,
        nca_key=row.nca_key,
        ids=decode_json_list(row.ids),
        ownership=Ownership(
            have_base=bool(row.have_base),
            up_to_date=bool(row.up_to_date),
            complete=bool(row.complete),
        ) if has_ownership else None,
        apps_loaded=[] if with_apps else None,
        available_versions=[],
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

def _load_apps_for_titles(title_ids_uc: List[str], app_filter: Optional[AppFilter],
                           with_files: bool) -> Dict[str, List[App]]:
    """Return apps keyed by uppercase title_id."""
    if not title_ids_uc:
        return {}
    params = {f"t_{i}": tid for i, tid in enumerate(title_ids_uc)}
    placeholders = ",".join(f":t_{i}" for i in range(len(title_ids_uc)))
    extra = build_clauses(app_filter, APP_FIELDS, params)
    extra_sql = (" AND " + " AND ".join(extra)) if extra else ""
    sql = f"""
    SELECT a.id AS id, a.app_id AS app_id, a.app_version AS app_version,
           a.app_type AS app_type, a.owned AS owned, ot.title_id AS tuc
    FROM apps a JOIN main.titles ot ON ot.id = a.title_id
    WHERE ot.title_id IN ({placeholders}){extra_sql}
    ORDER BY a.app_id, CAST(a.app_version AS INTEGER)
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
            files_loaded=[] if with_files else None,
        )
        out.setdefault(r.tuc, []).append(a)
        all_apps.append(a)
        if with_files:
            apps_by_pk[int(r.id)] = a
            app_pks.append(int(r.id))

    if with_files and app_pks:
        _hydrate_app_files(app_pks, apps_by_pk)
    if all_apps:
        _hydrate_apps_titledb(all_apps)
    return out


def _hydrate_app_files(app_pks: List[int], apps_by_pk: Dict[int, App]) -> None:
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
        f.apps_loaded = []
        app.files_loaded.append(f)
        files_by_pk[int(r.id)] = f
    if files_by_pk:
        _hydrate_file_apps(list(files_by_pk.keys()), files_by_pk)


def _hydrate_file_apps(file_pks: List[int], files_by_pk: Dict[int, "File"]) -> None:
    """Populate .apps_loaded on each file (m2m back-direction across app_files)."""
    placeholders = ",".join(f":f_{i}" for i in range(len(file_pks)))
    params = {f"f_{i}": pk for i, pk in enumerate(file_pks)}
    sql = f"""
    SELECT af.file_id AS pk,
           a.id AS id, a.app_id AS app_id, a.app_version AS app_version,
           a.app_type AS app_type, a.owned AS owned, ot.title_id AS title_id
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
            files_loaded=None,  # don't recursively load files for back-linked apps
        )
        f.apps_loaded.append(a)
        backlinked.append(a)
    if backlinked:
        _hydrate_apps_titledb(backlinked)


def _hydrate_apps_titledb(apps: List[App]) -> None:
    """For each App, attach its own titledb entry keyed by app_id (uppercase).
    Most useful for DLC apps (their titledb row is their own metadata, not the
    parent title's). Single batch SELECT, distinct app_ids only."""
    distinct_ids = list({a.app_id for a in apps if a.app_id})
    if not distinct_ids:
        return
    params = {f"i_{i}": x for i, x in enumerate(distinct_ids)}
    placeholders = ",".join(f":i_{i}" for i in range(len(distinct_ids)))
    sql = f"""
    SELECT {_title_cols('titledb')}
    FROM {_TITLEDB_DEDUPED} td
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


def _load_versions_for_titles(title_ids_uc: List[str]) -> Dict[str, List[Version]]:
    """Return versions keyed by uppercase title_id. titledb.versions stores ids
    lowercase, so we lowercase the bindings and uppercase the result."""
    if not title_ids_uc:
        return {}
    params = {f"t_{i}": tid.lower() for i, tid in enumerate(title_ids_uc)}
    placeholders = ",".join(f":t_{i}" for i in range(len(title_ids_uc)))
    sql = f"""
    SELECT v.title_id AS tlc, v.version AS version, v.release_date AS release_date,
           COALESCE((
             SELECT 1 FROM apps a JOIN main.titles ot ON ot.id = a.title_id
             WHERE LOWER(ot.title_id) = v.title_id
               AND a.app_type = 'UPDATE'
               AND CAST(a.app_version AS INTEGER) = v.version
               AND a.owned = 1
             LIMIT 1
           ), 0) AS owned
    FROM titledb.versions v
    WHERE v.title_id IN ({placeholders})
    ORDER BY v.title_id, v.version
    """
    rows = db.session.execute(text(sql), params).all()
    out: Dict[str, List[Version]] = {tid: [] for tid in title_ids_uc}
    for r in rows:
        out.setdefault((r.tlc or "").upper(), []).append(Version(
            version=int(r.version),
            release_date=r.release_date,
            owned=bool(r.owned),
        ))
    return out


# ------------- top-level resolvers -------------

def resolve_title(title_id: str, ctx: GraphQLContext) -> Optional[Title]:
    if not ctx.can_shop:
        return None
    tid = title_id.upper()
    # Drive from main.titles when this title is owned (so unrecognized titles surface),
    # otherwise drive from titledb. Both stores keep title_id in uppercase.
    sql = f"""
    SELECT {_title_cols('owned')}
    FROM main.titles ot
    LEFT JOIN {_TITLEDB_DEDUPED} td ON td.id = ot.title_id
    WHERE ot.title_id = :tid
    UNION ALL
    SELECT {_title_cols('titledb')}
    FROM {_TITLEDB_DEDUPED} td
    LEFT JOIN main.titles ot ON ot.title_id = td.id
    WHERE td.id = :tid AND ot.id IS NULL
    LIMIT 1
    """
    row = db.session.execute(text(sql), {"tid": tid}).first()
    if not row:
        return None
    title = _build_title(row, with_apps=ctx.can_shop, with_files=ctx.can_admin)
    if ctx.can_shop:
        apps_map = _load_apps_for_titles([tid], None, with_files=ctx.can_admin)
        title.apps_loaded = apps_map.get(tid, [])
    versions_map = _load_versions_for_titles([tid])
    title.available_versions = versions_map.get(tid, [])
    return title


def resolve_titles(*, owned: Optional[bool], filter: Optional[TitleFilter],
                    page: int, page_size: int, ctx: GraphQLContext) -> TitleConnection:
    if not ctx.can_shop:
        return TitleConnection(total=0, items=[])
    page = max(1, page)
    page_size = max(1, min(page_size, 500))

    params: dict = {}
    where = build_clauses(filter, TITLE_FIELDS, params)

    if owned is True:
        # Driver = main.titles, LEFT JOIN titledb. Unrecognized titles surface
        # with null titledb fields rather than being dropped.
        from_sql = (
            f"FROM main.titles ot "
            f"LEFT JOIN {_TITLEDB_DEDUPED} td ON td.id = ot.title_id"
        )
        order_by = "ot.title_id"
        cols = _title_cols("owned")
    elif owned is False:
        from_sql = (
            f"FROM {_TITLEDB_DEDUPED} td "
            f"LEFT JOIN main.titles ot ON ot.title_id = td.id"
        )
        where.append("ot.id IS NULL")
        order_by = "td.id"
        cols = _title_cols("titledb")
    else:
        from_sql = (
            f"FROM {_TITLEDB_DEDUPED} td "
            f"LEFT JOIN main.titles ot ON ot.title_id = td.id"
        )
        order_by = "td.id"
        cols = _title_cols("titledb")

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    count_sql = f"SELECT COUNT(*) {from_sql}{where_sql}"
    total = db.session.execute(text(count_sql), params).scalar() or 0

    page_sql = f"""
    SELECT {cols}
    {from_sql}
    {where_sql}
    ORDER BY {order_by}
    LIMIT :limit OFFSET :offset
    """
    page_params = dict(params, limit=page_size, offset=(page - 1) * page_size)
    rows = db.session.execute(text(page_sql), page_params).all()

    titles = [_build_title(r, with_apps=ctx.can_shop, with_files=ctx.can_admin)
              for r in rows]
    title_ids_uc = [(r.title_id or "").upper() for r in rows]

    if ctx.can_shop and title_ids_uc:
        apps_map = _load_apps_for_titles(title_ids_uc, None, with_files=ctx.can_admin)
        for t, tid in zip(titles, title_ids_uc):
            t.apps_loaded = apps_map.get(tid, [])

    if title_ids_uc:
        versions_map = _load_versions_for_titles(title_ids_uc)
        for t, tid in zip(titles, title_ids_uc):
            t.available_versions = versions_map.get(tid, [])

    return TitleConnection(total=int(total), items=titles)


def resolve_apps(*, owned: Optional[bool], filter: Optional[AppFilter],
                  page: int, page_size: int, ctx: GraphQLContext) -> AppConnection:
    if not ctx.can_shop:
        return AppConnection(total=0, items=[])
    page = max(1, page)
    page_size = max(1, min(page_size, 1000))

    params: dict = {}
    where = build_clauses(filter, APP_FIELDS, params)
    if owned is not None:
        params["owned_arg"] = 1 if owned else 0
        where.append("a.owned = :owned_arg")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    count_sql = f"""
    SELECT COUNT(*) FROM apps a JOIN main.titles ot ON ot.id = a.title_id{where_sql}
    """
    total = db.session.execute(text(count_sql), params).scalar() or 0

    page_sql = f"""
    SELECT a.id AS id, a.app_id AS app_id, a.app_version AS app_version,
           a.app_type AS app_type, a.owned AS owned, ot.title_id AS title_id
    FROM apps a JOIN main.titles ot ON ot.id = a.title_id{where_sql}
    ORDER BY a.id
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
            app_version=r.app_version,
            app_type=r.app_type,
            owned=bool(r.owned),
            files_loaded=[] if ctx.can_admin else None,
        )
        items.append(a)
        if ctx.can_admin:
            apps_by_pk[int(r.id)] = a

    if ctx.can_admin and apps_by_pk:
        _hydrate_app_files(list(apps_by_pk.keys()), apps_by_pk)
    if items:
        _hydrate_apps_titledb(items)

    return AppConnection(total=int(total), items=items)


def resolve_files(*, filter: Optional[FileFilter], page: int, page_size: int,
                   ctx: GraphQLContext) -> FileConnection:
    if not ctx.can_admin:
        return FileConnection(total=0, items=[])
    page = max(1, page)
    page_size = max(1, min(page_size, 1000))

    params: dict = {}
    where = build_clauses(filter, FILE_FIELDS, params)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    count_sql = f"SELECT COUNT(*) FROM files f{where_sql}"
    total = db.session.execute(text(count_sql), params).scalar() or 0

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
        f.apps_loaded = []
        items.append(f)
        files_by_pk[int(r.id)] = f
    if files_by_pk:
        _hydrate_file_apps(list(files_by_pk.keys()), files_by_pk)
    return FileConnection(total=int(total), items=items)
