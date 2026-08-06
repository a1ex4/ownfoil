"""Filter input types and SQL clause builders.

Implicit AND across all populated fields. v1 omits OR/NOT combinators.

Booleans are bare `Boolean`, not operator objects: equality is the only predicate a
bool has, so a wrapper would only ever spell `{eq: ...}`. Strings and ints keep theirs.
"""
from enum import Enum

import strawberry
from typing import List, Optional

from .scalars import BigInt


@strawberry.input
class StringFilter:
    eq: Optional[str] = None
    contains: Optional[str] = None  # case-insensitive substring
    in_: Optional[List[str]] = strawberry.field(name="in", default=None)


@strawberry.input
class StringListFilter:
    """Membership in a JSON-encoded list column (`Title.category` and friends).

    The column holds `["Adventure","Puzzle"]`, while the field it backs is a decoded
    `[String!]`. A StringFilter would filter the encoding rather than the elements -
    `eq: "Adventure"` could never match - so list columns get their own operators.
    """
    has: Optional[str] = None
    has_any: Optional[List[str]] = None
    has_all: Optional[List[str]] = None


@strawberry.input
class IntFilter:
    eq: Optional[int] = None
    gte: Optional[int] = None
    lte: Optional[int] = None
    in_: Optional[List[int]] = strawberry.field(name="in", default=None)


@strawberry.input
class BigIntFilter:
    """64-bit-capable variant of IntFilter; used for byte sizes."""
    eq: Optional[BigInt] = None
    gte: Optional[BigInt] = None
    lte: Optional[BigInt] = None
    in_: Optional[List[BigInt]] = strawberry.field(name="in", default=None)


@strawberry.input
class TitleFilter:
    title_id: Optional[StringFilter] = None
    name: Optional[StringFilter] = None
    publisher: Optional[StringFilter] = None
    developer: Optional[StringFilter] = None
    category: Optional[StringListFilter] = None
    region: Optional[StringFilter] = None
    language: Optional[StringFilter] = None
    release_date: Optional[StringFilter] = None
    parent_id: Optional[StringFilter] = None
    nsu_id: Optional[StringFilter] = None
    source: Optional[StringFilter] = None
    have_base: Optional[bool] = None
    up_to_date: Optional[bool] = None
    complete: Optional[bool] = None


@strawberry.input
class AppFilter:
    title_id: Optional[StringFilter] = None
    app_id: Optional[StringFilter] = None
    app_version: Optional[IntFilter] = None
    app_type: Optional[StringFilter] = None
    owned: Optional[bool] = None


@strawberry.input
class FileFilter:
    filepath: Optional[StringFilter] = None
    filename: Optional[StringFilter] = None
    folder: Optional[StringFilter] = None
    extension: Optional[StringFilter] = None
    identification_type: Optional[StringFilter] = None
    identified: Optional[bool] = None
    organized: Optional[bool] = None
    multicontent: Optional[bool] = None
    compressed: Optional[bool] = None
    library_id: Optional[IntFilter] = None
    size: Optional[BigIntFilter] = None
    download_count: Optional[IntFilter] = None
    nb_content: Optional[IntFilter] = None


def string_clauses(column_sql: str, f: Optional[StringFilter], params: dict, key: str) -> List[str]:
    """Translate a StringFilter into SQL fragments. Mutates `params` with bound values."""
    if f is None:
        return []
    out: List[str] = []
    if f.eq is not None:
        params[f"{key}_eq"] = f.eq
        out.append(f"{column_sql} = :{key}_eq")
    if f.contains is not None:
        params[f"{key}_ct"] = f"%{f.contains}%"
        out.append(f"{column_sql} LIKE :{key}_ct COLLATE NOCASE")
    if f.in_:
        keys = []
        for i, v in enumerate(f.in_):
            pk = f"{key}_in_{i}"
            params[pk] = v
            keys.append(f":{pk}")
        out.append(f"{column_sql} IN ({','.join(keys)})")
    return out


def bool_clauses(column_sql: str, value: Optional[bool], params: dict, key: str) -> List[str]:
    # `is None`, never truthiness: False is a predicate, not an absent filter.
    if value is None:
        return []
    params[f"{key}_eq"] = 1 if value else 0
    return [f"{column_sql} = :{key}_eq"]


def _json_array(column_sql: str) -> str:
    """A guaranteed-parseable JSON array expression for `json_each`.

    These columns default to an empty string rather than `[]`, and `json_each` raises
    on malformed JSON instead of yielding nothing - an AND with `json_valid` would not
    save us, since SQLite is free to evaluate the table-valued function first.
    """
    return f"CASE WHEN json_valid({column_sql}) THEN {column_sql} ELSE '[]' END"


def _has_element(column_sql: str, param: str) -> str:
    return (f"EXISTS (SELECT 1 FROM json_each({_json_array(column_sql)}) "
            f"WHERE json_each.value = :{param})")


def string_list_clauses(column_sql: str, f: Optional[StringListFilter],
                        params: dict, key: str) -> List[str]:
    """Translate a StringListFilter into element-membership SQL over a JSON array."""
    if f is None:
        return []
    out: List[str] = []
    if f.has is not None:
        params[f"{key}_has"] = f.has
        out.append(_has_element(column_sql, f"{key}_has"))
    if f.has_any:
        keys = []
        for i, v in enumerate(f.has_any):
            params[f"{key}_any_{i}"] = v
            keys.append(f":{key}_any_{i}")
        out.append(f"EXISTS (SELECT 1 FROM json_each({_json_array(column_sql)}) "
                   f"WHERE json_each.value IN ({','.join(keys)}))")
    if f.has_all:
        for i, v in enumerate(f.has_all):
            params[f"{key}_all_{i}"] = v
            out.append(_has_element(column_sql, f"{key}_all_{i}"))
    return out


def int_clauses(column_sql: str, f: Optional[IntFilter], params: dict, key: str) -> List[str]:
    if f is None:
        return []
    out: List[str] = []
    if f.eq is not None:
        params[f"{key}_eq"] = f.eq
        out.append(f"{column_sql} = :{key}_eq")
    if f.gte is not None:
        params[f"{key}_gte"] = f.gte
        out.append(f"{column_sql} >= :{key}_gte")
    if f.lte is not None:
        params[f"{key}_lte"] = f.lte
        out.append(f"{column_sql} <= :{key}_lte")
    if f.in_:
        keys = []
        for i, v in enumerate(f.in_):
            pk = f"{key}_in_{i}"
            params[pk] = v
            keys.append(f":{pk}")
        out.append(f"{column_sql} IN ({','.join(keys)})")
    return out


# (filter-attr, sql-column-expression, kind)
TITLE_FIELDS = [
    ("title_id",     "td.id",            "string"),
    ("name",         "td.name",          "string"),
    ("publisher",    "td.publisher",     "string"),
    ("developer",    "td.developer",     "string"),
    ("category",     "td.category",      "strlist"),
    ("region",       "td.region",        "string"),
    ("language",     "td.language",      "string"),
    ("release_date", "td.release_date",  "string"),
    ("parent_id",    "td.parent_id",     "string"),
    ("nsu_id",       "td.nsu_id",        "string"),
    ("source",       "td.source",        "string"),
    # main.titles is LEFT JOINed, so a catalogue-only title has no ownership row at
    # all. Compared bare, `NULL = 0` is NULL rather than true, so both polarities
    # matched nothing and the filter could not discriminate. A title the library has
    # never seen definitively has no base, is not up to date and is not complete, so
    # the absent row reads as false.
    ("have_base",    "COALESCE(ot.have_base, 0)",  "bool"),
    ("up_to_date",   "COALESCE(ot.up_to_date, 0)", "bool"),
    ("complete",     "COALESCE(ot.complete, 0)",   "bool"),
]

APP_FIELDS = [
    ("title_id",    "ot.title_id",   "string"),
    ("app_id",      "a.app_id",      "string"),
    # Stored as text but semantically an integer, so it is filtered and sorted as one:
    # lexicographically "9" sorts above "65536", which is never what a caller means.
    ("app_version", "CAST(a.app_version AS INTEGER)", "int"),
    ("app_type",    "a.app_type",    "string"),
    ("owned",       "a.owned",       "bool"),
]

# `resolve_apps` filters `owned` itself: grouped by app id it means "any version of
# this app is owned", which is a HAVING on MAX(a.owned) rather than a WHERE on one
# row. Leaving it here too let `filter: {owned:}` and the `owned:` shorthand answer
# the same question differently. Every other caller filters ungrouped rows, where the
# row-level column in APP_FIELDS is the right one.
APP_FIELDS_EXCEPT_OWNED = [f for f in APP_FIELDS if f[0] != "owned"]

FILE_FIELDS = [
    ("filepath",            "f.filepath",            "string"),
    ("filename",            "f.filename",            "string"),
    ("folder",              "f.folder",              "string"),
    ("extension",           "f.extension",           "string"),
    ("identification_type", "f.identification_type", "string"),
    ("identified",          "f.identified",          "bool"),
    ("organized",           "f.organized",           "bool"),
    ("multicontent",        "f.multicontent",        "bool"),
    ("compressed",          "f.compressed",          "bool"),
    ("library_id",          "f.library_id",          "int"),
    ("size",                "f.size",                "int"),
    ("download_count",      "f.download_count",      "int"),
    ("nb_content",          "f.nb_content",          "int"),
]


# ---- ordering ----
#
# The client picks a field from an enum, never a column name: the SQL fragment is
# looked up server-side, so nothing a caller sends is ever interpolated into ORDER BY.


@strawberry.enum
class OrderField(Enum):
    """What to sort by. Not every field applies to every query - see ORDER_FIELDS."""
    ID = "id"
    NAME = "name"
    SIZE = "size"
    RELEASE_DATE = "release_date"
    DOWNLOAD_COUNT = "download_count"
    ADDED_AT = "added_at"
    VERSION = "version"


@strawberry.enum
class OrderDirection(Enum):
    ASC = "ASC"
    DESC = "DESC"


@strawberry.input
class OrderBy:
    field: OrderField = OrderField.ID
    direction: OrderDirection = OrderDirection.ASC


# Per-query whitelist: {OrderField value: SQL expression}. A field absent from a
# query's map falls back to that query's default, so asking a files query to sort by
# NAME degrades to its id order rather than erroring.
TITLE_ORDER = {
    "name": "td.name IS NULL, td.name COLLATE NOCASE",
    "release_date": "td.release_date IS NULL, td.release_date",
    "size": "td.size IS NULL, CAST(td.size AS INTEGER)",
}

APP_ORDER = {
    "name": "td.name IS NULL, td.name COLLATE NOCASE",
    "release_date": "a.release_date IS NULL, a.release_date",
    "version": "CAST(a.app_version AS INTEGER)",
}

# Grouped by app id, the item is the group's highest version, so that is what sorting
# by VERSION has to compare - a bare column would be the aggregate's row by accident
# rather than by intent.
APP_ORDER_GROUPED = {**APP_ORDER, "version": "MAX(CAST(a.app_version AS INTEGER))"}

FILE_ORDER = {
    "name": "f.filename COLLATE NOCASE",
    "size": "f.size IS NULL, f.size",
    "download_count": "f.download_count",
    "added_at": "f.added_at IS NULL, f.added_at",
    "release_date": "f.mtime IS NULL, f.mtime",
}


def order_sql(order_by: Optional[OrderBy], allowed: dict, default: str) -> str:
    """Build an ORDER BY body from a whitelisted field plus a direction.

    `default` is appended as a tie-break so paging stays stable: without a unique
    trailing key, rows that compare equal can swap between pages and a client sees
    one row twice and another never."""
    if order_by is None:
        return default
    expr = allowed.get(order_by.field.value)
    direction = order_by.direction.value
    if expr is None:
        return f"{default} {direction}"
    # NULL-placement flags must not take the direction, or DESC would flip them.
    parts = [p.strip() for p in expr.split(",")]
    ordered = ", ".join(p if p.endswith("IS NULL") else f"{p} {direction}" for p in parts)
    return f"{ordered}, {default}"


def build_clauses(filter_obj, fields, params: dict) -> List[str]:
    """Translate a filter input object into a list of SQL clauses (AND-combined by caller)."""
    if filter_obj is None:
        return []
    clauses: List[str] = []
    for attr, col, kind in fields:
        f = getattr(filter_obj, attr, None)
        if f is None:
            continue
        if kind == "string":
            clauses += string_clauses(col, f, params, attr)
        elif kind == "bool":
            clauses += bool_clauses(col, f, params, attr)
        elif kind == "int":
            clauses += int_clauses(col, f, params, attr)
        elif kind == "strlist":
            clauses += string_list_clauses(col, f, params, attr)
    return clauses


# In-memory filter matchers, used when filtering an already-hydrated list (e.g.
# nested Title.apps). Mirrors the SQL semantics in *_clauses above.

def match_string(value, f: Optional[StringFilter]) -> bool:
    if f is None:
        return True
    if value is None:
        return False
    if f.eq is not None and value != f.eq:
        return False
    if f.contains is not None and f.contains.lower() not in str(value).lower():
        return False
    if f.in_ is not None and value not in f.in_:
        return False
    return True


def match_bool(value, expected: Optional[bool]) -> bool:
    return expected is None or bool(value) == expected


def match_int(value, f) -> bool:
    """Matches against either IntFilter or BigIntFilter (same shape)."""
    if f is None:
        return True
    if value is None:
        return False
    if f.eq is not None and value != f.eq:
        return False
    if f.gte is not None and value < f.gte:
        return False
    if f.lte is not None and value > f.lte:
        return False
    if f.in_ is not None and value not in f.in_:
        return False
    return True


def match_app(app, owned: Optional[bool], f: Optional[AppFilter],
              app_type: Optional[List[str]] = None) -> bool:
    if owned is not None and bool(app.owned) != owned:
        return False
    if app_type and app.app_type not in app_type:
        return False
    if f is None:
        return True
    return (
        match_string(app.title_id,    f.title_id)
        and match_string(app.app_id,      f.app_id)
        and match_int(app.app_version,    f.app_version)
        and match_string(app.app_type,    f.app_type)
        and match_bool(app.owned,         f.owned)
    )


def match_file(file_, f: Optional[FileFilter]) -> bool:
    if f is None:
        return True
    return (
        match_string(file_.filepath,            f.filepath)
        and match_string(file_.filename,            f.filename)
        and match_string(file_.folder,              f.folder)
        and match_string(file_.extension,           f.extension)
        and match_string(file_.identification_type, f.identification_type)
        and match_bool(file_.identified,            f.identified)
        and match_bool(file_.organized,             f.organized)
        and match_bool(file_.multicontent,          f.multicontent)
        and match_bool(file_.compressed,            f.compressed)
        and match_int(file_.library_id,             f.library_id)
        and match_int(file_.size,                   f.size)
        and match_int(file_.download_count,         f.download_count)
        and match_int(file_.nb_content,             f.nb_content)
    )
