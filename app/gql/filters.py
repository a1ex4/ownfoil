"""Filter input types and SQL clause builders.

Implicit AND across all populated fields. v1 omits OR/NOT combinators.
"""
import strawberry
from typing import List, Optional

from .scalars import BigInt


@strawberry.input
class StringFilter:
    eq: Optional[str] = None
    contains: Optional[str] = None  # case-insensitive substring
    in_: Optional[List[str]] = strawberry.field(name="in", default=None)


@strawberry.input
class BoolFilter:
    eq: Optional[bool] = None


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
    category: Optional[StringFilter] = None
    region: Optional[StringFilter] = None
    language: Optional[StringFilter] = None
    release_date: Optional[StringFilter] = None
    parent_id: Optional[StringFilter] = None
    nsu_id: Optional[StringFilter] = None
    source: Optional[StringFilter] = None
    have_base: Optional[BoolFilter] = None
    up_to_date: Optional[BoolFilter] = None
    complete: Optional[BoolFilter] = None


@strawberry.input
class AppFilter:
    title_id: Optional[StringFilter] = None
    app_id: Optional[StringFilter] = None
    app_version: Optional[StringFilter] = None
    app_type: Optional[StringFilter] = None
    owned: Optional[BoolFilter] = None


@strawberry.input
class FileFilter:
    filepath: Optional[StringFilter] = None
    filename: Optional[StringFilter] = None
    folder: Optional[StringFilter] = None
    extension: Optional[StringFilter] = None
    identification_type: Optional[StringFilter] = None
    identified: Optional[BoolFilter] = None
    organized: Optional[BoolFilter] = None
    multicontent: Optional[BoolFilter] = None
    compressed: Optional[BoolFilter] = None
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


def bool_clauses(column_sql: str, f: Optional[BoolFilter], params: dict, key: str) -> List[str]:
    if f is None or f.eq is None:
        return []
    params[f"{key}_eq"] = 1 if f.eq else 0
    return [f"{column_sql} = :{key}_eq"]


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
    ("category",     "td.category",      "string"),
    ("region",       "td.region",        "string"),
    ("language",     "td.language",      "string"),
    ("release_date", "td.release_date",  "string"),
    ("parent_id",    "td.parent_id",     "string"),
    ("nsu_id",       "td.nsu_id",        "string"),
    ("source",       "td.source",        "string"),
    ("have_base",    "ot.have_base",     "bool"),
    ("up_to_date",   "ot.up_to_date",    "bool"),
    ("complete",     "ot.complete",      "bool"),
]

APP_FIELDS = [
    ("title_id",    "ot.title_id",   "string"),
    ("app_id",      "a.app_id",      "string"),
    ("app_version", "a.app_version", "string"),
    ("app_type",    "a.app_type",    "string"),
    ("owned",       "a.owned",       "bool"),
]

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


def match_bool(value, f: Optional[BoolFilter]) -> bool:
    if f is None:
        return True
    if f.eq is not None and bool(value) != f.eq:
        return False
    return True


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
        and match_string(app.app_version, f.app_version)
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
