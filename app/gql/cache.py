"""ETag computation for the GraphQL endpoint.

The world hash summarises every piece of state the schema can expose, so a client
holding a 304 can trust that nothing it could have read has changed. Combined with
the query text, variables and role, it yields a stable ETag that lets clients
short-circuit unchanged refetches via If-None-Match.

It is built from aggregates rather than from a revision counter that writers bump:
SQLite stores no row count, so COUNT(*) already walks the whole B-tree, and folding
the mutable columns into that same scan costs almost nothing. The pay-off is that no
writer has to remember to invalidate anything - a flag flipped anywhere changes a
SUM here.
"""
import hashlib
import json

from db import db
from sqlalchemy import text


# One derived table per source table, cross-joined so the summary is a single round
# trip and a single scan each. Every mutable column the schema exposes has to appear
# in one of these, or a change to it will serve a stale 304.
_WORLD_SQL = """
SELECT * FROM
  (SELECT COUNT(*), COALESCE(SUM(have_base), 0), COALESCE(SUM(up_to_date), 0),
          COALESCE(SUM(complete), 0)
     FROM main.titles),
  (SELECT COUNT(*), COALESCE(SUM(owned), 0), COALESCE(MAX(id), 0)
     FROM apps),
  (SELECT COUNT(*), COALESCE(MAX(id), 0), COALESCE(SUM(organized), 0),
          COALESCE(SUM(identified), 0), COALESCE(SUM(identification_attempts), 0),
          COALESCE(SUM(download_count), 0),
          COUNT(signature_valid), COALESCE(SUM(signature_valid), 0),
          COUNT(hash_valid), COALESCE(SUM(hash_valid), 0)
     FROM files),
  (SELECT COUNT(*), COALESCE(MAX(id), 0), COALESCE(SUM(completion_pct), 0)
     FROM tasks)
"""


def _titledb_attached() -> bool:
    row = db.session.execute(text(
        "SELECT 1 FROM pragma_database_list WHERE name = 'titledb' LIMIT 1"
    )).first()
    return row is not None


def world_hash() -> str:
    """Return a short hex digest summarising library + titledb state."""
    row = db.session.execute(text(_WORLD_SQL)).first()
    parts = [str(x) for x in (row or [])]
    if _titledb_attached():
        meta = db.session.execute(text(
            "SELECT value FROM titledb.meta WHERE key = 'imported_at'"
        )).first()
        parts.append(meta[0] if meta else "")
    else:
        parts.append("")
    h = hashlib.md5("|".join(parts).encode()).hexdigest()
    return h[:16]


def etag_for(query: str, variables, operation_name, role: str, world: str) -> str:
    """Build the ETag value (already wrapped in quotes per RFC 7232)."""
    payload = json.dumps({
        "q": query or "",
        "v": variables or {},
        "o": operation_name or "",
        "r": role,
        "w": world,
    }, sort_keys=True, separators=(",", ":"))
    return '"' + hashlib.md5(payload.encode()).hexdigest() + '"'
