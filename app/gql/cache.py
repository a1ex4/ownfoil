"""ETag computation for the GraphQL endpoint.

The world hash captures everything that should bust a browser cache:
  - apps row count, owned count, max id (covers schema-relevant Apps mutations)
  - files row count and max id
  - titledb imported_at (set by titledb.store on every rebuild)

Combined with the query text, variables, and role, this yields a stable ETag
that lets clients short-circuit unchanged refetches via If-None-Match.
"""
import hashlib
import json

from db import db
from sqlalchemy import text


def _titledb_attached() -> bool:
    row = db.session.execute(text(
        "SELECT 1 FROM pragma_database_list WHERE name = 'titledb' LIMIT 1"
    )).first()
    return row is not None


def world_hash() -> str:
    """Return a short hex digest summarising library + titledb state."""
    row = db.session.execute(text("""
        SELECT
          (SELECT COUNT(*) FROM apps)                          AS apps_n,
          (SELECT COUNT(*) FROM apps WHERE owned = 1)          AS owned_n,
          (SELECT COALESCE(MAX(id), 0) FROM apps)              AS max_app_id,
          (SELECT COUNT(*) FROM files)                         AS files_n,
          (SELECT COALESCE(MAX(id), 0) FROM files)             AS max_file_id
    """)).first()
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
