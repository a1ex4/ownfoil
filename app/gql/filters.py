"""Filter input types and SQL clause builders.

Implicit AND across all populated fields. v1 omits OR/NOT combinators.

Booleans are bare `Boolean`, not operator objects: equality is the only predicate a
bool has, so a wrapper would only ever spell `{eq: ...}`. Strings and ints keep theirs.
"""
from enum import Enum

import strawberry
from typing import List, Optional

from containers.verification import (
    STATUS_ANY, STATUS_CORRUPT, STATUS_REPACK, STATUS_RULES, STATUS_SIGNATURE_FAILED,
    STATUS_SIGNATURE_OK, STATUS_UNVERIFIED, STATUS_VALID, status_of,
)

from .docs import desc, described
from .scalars import BigInt


@described(strawberry.input)
class StringFilter:
    """Text predicates. Populated operators AND together."""
    eq: Optional[str] = desc("Exact match, case-sensitive.", default=None)
    contains: Optional[str] = desc("Case-insensitive substring match.", default=None)
    in_: Optional[List[str]] = desc(
        "Matches any value in the list. An empty list is no constraint.",
        name="in", default=None)


@described(strawberry.input)
class StringListFilter:
    """Membership in a JSON-encoded list column (`Title.category` and friends).

    The column holds `["Adventure","Puzzle"]`, while the field it backs is a decoded
    `[String!]`. A StringFilter would filter the encoding rather than the elements -
    `eq: "Adventure"` could never match - so list columns get their own operators.
    """
    has: Optional[str] = desc("The list contains this exact element.", default=None)
    has_any: Optional[List[str]] = desc(
        "The list contains at least one of these elements.", default=None)
    has_all: Optional[List[str]] = desc(
        "The list contains every one of these elements.", default=None)


@described(strawberry.input)
class IntFilter:
    """Numeric predicates. Populated operators AND together, so `gte` with `lte` is
    a range."""
    eq: Optional[int] = desc("Exactly this value.", default=None)
    gte: Optional[int] = desc("At least this value.", default=None)
    lte: Optional[int] = desc("At most this value.", default=None)
    in_: Optional[List[int]] = desc(
        "Matches any value in the list. An empty list is no constraint.",
        name="in", default=None)


@described(strawberry.input)
class BigIntFilter:
    """64-bit-capable variant of IntFilter, for byte sizes that overflow `Int`."""
    eq: Optional[BigInt] = desc("Exactly this many bytes.", default=None)
    gte: Optional[BigInt] = desc("At least this many bytes.", default=None)
    lte: Optional[BigInt] = desc("At most this many bytes.", default=None)
    in_: Optional[List[BigInt]] = desc(
        "Matches any value in the list. An empty list is no constraint.",
        name="in", default=None)


@described(strawberry.input)
class TitleFilter:
    """Predicates on a title. Every populated field ANDs with the others."""
    title_id: Optional[StringFilter] = desc(
        "The 16-hex-digit title id. Uppercase - the stored ids are, and the match is "
        "case-sensitive.", default=None)
    name: Optional[StringFilter] = desc(
        "The game's name. `contains` is the useful operator here; `search` on the "
        "query searches name and ids together.", default=None)
    publisher: Optional[StringFilter] = desc("Publisher of record.", default=None)
    developer: Optional[StringFilter] = desc("Studio that made the game.",
                                             default=None)
    category: Optional[StringListFilter] = desc(
        "Genre tags. A list filter, because the column holds a JSON array and the "
        "field is a `[String!]` - element membership, not string matching.",
        default=None)
    region: Optional[StringFilter] = desc(
        "Primary region of the catalogue entry. Filters the scalar `region`, not the "
        "`regions` list, which is not filterable.", default=None)
    language: Optional[StringFilter] = desc(
        "Primary language of the catalogue entry. Filters the scalar `language`, not "
        "the `languages` list.", default=None)
    release_date: Optional[StringFilter] = desc(
        "Release date as titledb spells it - matched as text, so `contains: \"2017\"` "
        "is the practical way to ask for a year.", default=None)
    parent_id: Optional[StringFilter] = desc(
        "Title id this entry belongs under, for regional variants.", default=None)
    nsu_id: Optional[StringFilter] = desc("Nintendo eShop identifier.", default=None)
    source: Optional[StringFilter] = desc(
        "Which metadata source won: `titledb` or `custom`.", default=None)
    have_base: Optional[bool] = desc(
        "Whether the base game is in the library. A title the library has never seen "
        "counts as false, not unknown.", default=None)
    up_to_date: Optional[bool] = desc(
        "Whether no newer update is known than the highest one owned. Unowned titles "
        "count as false.", default=None)
    complete: Optional[bool] = desc(
        "Whether every known DLC is owned. Unowned titles count as false.",
        default=None)


@described(strawberry.input)
class AppFilter:
    """Predicates on an app. Every populated field ANDs with the others."""
    title_id: Optional[StringFilter] = desc(
        "Id of the title the app belongs to, uppercase.", default=None)
    app_id: Optional[StringFilter] = desc("The app's own application id.",
                                          default=None)
    app_version: Optional[IntFilter] = desc(
        "The version, compared numerically - so `gte: 65536` means what it says, "
        "which it could not if versions were compared as text.", default=None)
    app_type: Optional[StringFilter] = desc(
        "BASE, UPDATE or DLC. The `appType` argument is the shorthand for this and "
        "takes a list.", default=None)
    owned: Optional[bool] = desc(
        "Whether a file carries the app. Identical to the `owned` argument, including "
        "under `groupByAppId: true`, where both mean 'any version of this app id'.",
        default=None)


@described(strawberry.enum)
class VerificationStatus(Enum):
    """One label for a file's two verification verdicts. Derived from `signatureValid`
    and `hashValid` rather than stored, so it can never disagree with them."""
    UNVERIFIED = strawberry.enum_value(
        STATUS_UNVERIFIED,
        description="Never checked. Either verification is off, the keys are missing, "
                    "or the file has not come round yet.")
    VALID = strawberry.enum_value(
        STATUS_VALID,
        description="Signed by Nintendo and every content hashed as claimed. The only "
                    "status that means the bytes are both authentic and intact.")
    REPACK = strawberry.enum_value(
        STATUS_REPACK,
        description="Re-signed, contents intact. A repacked file rather than a damaged "
                    "one - much of a normal library looks like this, and it is not on "
                    "its own a reason to re-download.")
    CORRUPT = strawberry.enum_value(
        STATUS_CORRUPT,
        description="At least one content did not hash to what it claims. These are the "
                    "files worth replacing, and the ones compression refuses to touch.")
    SIGNATURE_OK = strawberry.enum_value(
        STATUS_SIGNATURE_OK,
        description="Signatures checked out, contents never read. A pass as far as the "
                    "configured depth looked - raise it to `hash` to learn more.")
    SIGNATURE_FAILED = strawberry.enum_value(
        STATUS_SIGNATURE_FAILED,
        description="Signatures did not check out and the contents were never read, so "
                    "this is either an ordinary repack or a corrupt file - `hash` depth "
                    "is what tells the two apart.")


@described(strawberry.input)
class FileFilter:
    """Predicates on a file. Every populated field ANDs with the others."""
    filepath: Optional[StringFilter] = desc(
        "Absolute path on the server. Admin only, like the field itself.",
        default=None)
    filename: Optional[StringFilter] = desc("File name with extension.", default=None)
    folder: Optional[StringFilter] = desc(
        "Directory relative to the library root.", default=None)
    extension: Optional[StringFilter] = desc(
        "Lowercase extension without the dot, e.g. `nsp`.", default=None)
    identification_type: Optional[StringFilter] = desc(
        "How the file was identified - from its own metadata, or from its name.",
        default=None)
    identified: Optional[bool] = desc(
        "Whether ownfoil worked out what the file contains. `false` is the list of "
        "files needing attention.", default=None)
    organized: Optional[bool] = desc(
        "Whether the file already sits where the naming template says.", default=None)
    multicontent: Optional[bool] = desc(
        "Whether the file carries more than one app.", default=None)
    compressed: Optional[bool] = desc(
        "Whether the file is NSZ/XCZ rather than NSP/XCI.", default=None)
    signature_valid: Optional[bool] = desc(
        "Whether the NCA header signatures checked out. Neither `true` nor `false` "
        "matches a file that was never verified.", default=None)
    hash_valid: Optional[bool] = desc(
        "Whether the NCA contents hashed as claimed. `false` is the list of files worth "
        "re-downloading; neither value matches one never verified at `hash` depth.",
        default=None)
    verification_status: Optional[VerificationStatus] = desc(
        "The two verdicts as one label. Every status is a fixed combination of "
        "`signatureValid` and `hashValid`, so this is shorthand rather than an extra "
        "predicate - `CORRUPT` is the one worth an alert.", default=None)
    library_id: Optional[IntFilter] = desc(
        "Which library root the file sits under.", default=None)
    size: Optional[BigIntFilter] = desc(
        "Size in bytes. `gte` with `lte` gives a range - the way to find the files "
        "worth compressing.", default=None)
    download_count: Optional[IntFilter] = desc(
        "How many times shop clients have downloaded it.", default=None)
    nb_content: Optional[IntFilter] = desc(
        "How many apps the file carries. `gte: 2` is another way to say "
        "`multicontent: true`.", default=None)


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


def _verdict_sql(column_sql: str, want) -> Optional[str]:
    """One verdict column tested against one row of STATUS_RULES."""
    if want is STATUS_ANY:
        return None
    if want is None:
        return f"{column_sql} IS NULL"
    return f"{column_sql} = {1 if want else 0}"


def verification_status_clauses(alias: str, status) -> List[str]:
    """Translate a VerificationStatus into a test on the two verdict columns.

    Reads the same table the projection reads, so the filter and the value it filters
    on cannot drift apart. Nothing is bound: the operands come from that table, never
    from anything a caller sent.
    """
    if status is None:
        return []
    for name, want_signature, want_hash in STATUS_RULES:
        if name != status.value:
            continue
        tests = [t for t in (_verdict_sql(f"{alias}.signature_valid", want_signature),
                             _verdict_sql(f"{alias}.hash_valid", want_hash)) if t]
        return [f"({' AND '.join(tests)})"]
    return []


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
    ("signature_valid",     "f.signature_valid",     "bool"),
    ("hash_valid",          "f.hash_valid",          "bool"),
    # Spans both verdict columns, so the entry carries the table alias rather than one
    # column and the clause builder picks the columns out of STATUS_RULES.
    ("verification_status", "f",                     "vstatus"),
    ("library_id",          "f.library_id",          "int"),
    ("size",                "f.size",                "int"),
    ("download_count",      "f.download_count",      "int"),
    ("nb_content",          "f.nb_content",          "int"),
]


# ---- ordering ----
#
# The client picks a field from an enum, never a column name: the SQL fragment is
# looked up server-side, so nothing a caller sends is ever interpolated into ORDER BY.


@described(strawberry.enum)
class OrderField(Enum):
    """What to sort by. Not every member applies to every query, and one that does
    not falls back to that query's default order rather than erroring - sorting
    titles by DOWNLOAD_COUNT is meaningless, not invalid."""
    ID = strawberry.enum_value("id", description=(
        "Primary key order. Every query's default, and the tie-break appended to "
        "every other ordering so paging stays stable."))
    NAME = strawberry.enum_value("name", description=(
        "Title name for `titles` and `apps`, file name for `files`. "
        "Case-insensitive, with unnamed rows last."))
    SIZE = strawberry.enum_value("size", description=(
        "Bytes for `files`; the catalogue's install size, cast to a number, for "
        "`titles`. Not applicable to `apps`."))
    RELEASE_DATE = strawberry.enum_value("release_date", description=(
        "Catalogue release date for `titles` and `apps`. On `files` it sorts by "
        "`mtime`, the closest thing a file has."))
    DOWNLOAD_COUNT = strawberry.enum_value("download_count", description=(
        "How often shop clients fetched the file. `files` only."))
    ADDED_AT = strawberry.enum_value("added_at", description=(
        "When ownfoil first saw the file - the 'recently added' view, paired with "
        "`direction: DESC`. `files` only."))
    VERSION = strawberry.enum_value("version", description=(
        "App version, compared numerically. `apps` only; under "
        "`groupByAppId: true` it sorts by the group's highest version."))


@described(strawberry.enum)
class OrderDirection(Enum):
    """Which way to sort. Null-placement is not reversed by DESC: rows with nothing
    to sort on stay last either way."""
    ASC = strawberry.enum_value("ASC", description="Smallest, earliest or A-Z first.")
    DESC = strawberry.enum_value("DESC", description="Largest, latest or Z-A first.")


@described(strawberry.input)
class OrderBy:
    """How to sort a page. The query's own key is always appended as a tie-break, so
    two rows that compare equal keep a stable order across pages."""
    field: OrderField = desc("What to sort by. Defaults to primary key order.",
                             default=OrderField.ID)
    direction: OrderDirection = desc("Which way. Defaults to ascending.",
                                     default=OrderDirection.ASC)


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
        elif kind == "vstatus":
            clauses += verification_status_clauses(col, f)
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


def match_tristate(value, expected: Optional[bool]) -> bool:
    """For a nullable column: null matches neither `true` nor `false`, the same way the
    SQL side's `col = 0/1` skips it. match_bool would fold it into `false`."""
    return expected is None or (value is not None and bool(value) == expected)


def match_verification_status(file_, expected) -> bool:
    """Derives the status the same way the field does, rather than testing the columns
    a second time - two spellings of one rule is how they come to disagree."""
    if expected is None:
        return True
    return status_of(file_.signature_valid, file_.hash_valid) == expected.value


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
        and match_tristate(file_.signature_valid,   f.signature_valid)
        and match_tristate(file_.hash_valid,        f.hash_valid)
        and match_verification_status(file_,        f.verification_status)
        and match_int(file_.library_id,             f.library_id)
        and match_int(file_.size,                   f.size)
        and match_int(file_.download_count,         f.download_count)
        and match_int(file_.nb_content,             f.nb_content)
    )
