"""SQLite-backed store for titledb metadata.

Builds and queries ``config/titles.db`` from the downloaded JSON files. The file is fully
derivable and disposable: it is versioned by a schema fingerprint rather than migrations,
and recreated from scratch whenever that fingerprint no longer matches. Metadata coming
from anywhere else (user-authored, extracted from the files) lives durably in ownfoil.db
and is projected in here, merged field by field following schema.SOURCE_PRIORITY.
"""
import contextlib
import datetime
import json
import logging
import os
import sqlite3
import time

from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool

from constants import DB_FILE, TITLES_DB_FILE
from titledb import schema
from titledb.schema import (OVERRIDE_SOURCES, SOURCE_CUSTOM, SOURCE_PRIORITY,
                            SOURCE_TITLEDB)

logger = logging.getLogger('main')

# (json_key, column_name, json_type), json_type: 's'=scalar, 'j'=list/object (json-encoded)
_TITLES_COLUMNS = schema.column_map(schema.titles)
_CNMTS_COLUMNS = schema.column_map(schema.cnmts)
# The metadata columns, without the id: what an override row and the merge deal in.
_META_COLUMNS = [c.name for c in schema.title_overrides.c if c.name not in ('id', 'source')]
_OVERRIDE_COLUMNS = [c for c in _TITLES_COLUMNS if c[1] != 'id']


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def _engine(path):
    """Engine for a titles.db file. NullPool: nothing may keep the file open afterwards,
    _replace_titles_db can only dispose the main pool."""
    return create_engine(URL.create('sqlite', database=path), poolclass=NullPool)


def create_titledb(path):
    """Create an empty titles.db from the schema metadata, fingerprinted."""
    engine = _engine(path)
    try:
        with engine.begin() as connection:
            schema.metadata.create_all(connection)
    finally:
        engine.dispose()
    with contextlib.closing(sqlite3.connect(path)) as conn:
        _set_meta(conn, 'schema_version', schema.fingerprint())
        conn.commit()


def init_titledb():
    """Create or recreate titles.db, the lifecycle ownfoil.db gets in init_db.

    titles.db is ATTACHed as `titledb` on every main connection and joined by the GraphQL
    resolvers from the first page load, which happens well before the initial import task
    has built it. Without the file the schema is simply absent and those queries fail.
    """
    try:
        if os.path.isfile(TITLES_DB_FILE):
            with contextlib.closing(sqlite3.connect(TITLES_DB_FILE)) as conn:
                version = _get_meta(conn, 'schema_version')
            if version == schema.fingerprint():
                logger.info(f'titles.db schema is up to date ({version})')
                return
            logger.info(f'titles.db schema changed ({version} -> {schema.fingerprint()}), rebuilding it.')
            os.remove(TITLES_DB_FILE)
        create_titledb(TITLES_DB_FILE)
        logger.info('Created empty titles.db, pending the next titledb import.')
    except Exception as e:
        # Half-written file, unreadable schema, failed create: titles.db is fully derivable,
        # so start over rather than fail startup. The next update_titledb re-imports it.
        logger.error(f'titles.db is unusable ({e}), recreating it empty.')
        with contextlib.suppress(OSError):
            os.remove(TITLES_DB_FILE)
        create_titledb(TITLES_DB_FILE)


def _get_meta(conn, key):
    row = conn.execute('SELECT value FROM meta WHERE key = ?', (key,)).fetchone()
    return row[0] if row else None


def _set_meta(conn, key, value):
    conn.execute('INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)', (key, value))


def _encode_row(record, columns):
    out = []
    for json_key, _col, kind in columns:
        v = record.get(json_key)
        if kind == 'j' and v is not None:
            v = json.dumps(v, separators=(',', ':'))
        out.append(v)
    return out


# ---------------------------------------------------------------------------
# Merge: resolve the sources against each other, field by field
# ---------------------------------------------------------------------------

def _alias(source):
    return f'o_{source}'


def _merge_sql(single_id):
    """Rebuild the merged `titles` rows from `title_overrides`, by source priority.

    One LEFT JOIN per source and a COALESCE per column, both generated from
    SOURCE_PRIORITY: a field falls through to the next source when the higher-priority
    one has nothing for it, which is the whole point - an `extract` row carrying only a
    name must not blank out the artwork only titledb has.
    """
    aliases = [_alias(s) for s in SOURCE_PRIORITY]
    cols = ', '.join(f'COALESCE({", ".join(f"{a}.{c}" for a in aliases)}) AS {c}'
                     for c in _META_COLUMNS)
    # Highest-priority source that has a row, and the full list, both in priority order.
    source = 'COALESCE(' + ', '.join(
        f"CASE WHEN {_alias(s)}.id IS NOT NULL THEN '{s}' END" for s in SOURCE_PRIORITY) + ')'
    # Each present source contributes ',<source>'; substr drops the leading separator.
    sources = 'substr(' + ' || '.join(
        f"CASE WHEN {_alias(s)}.id IS NOT NULL THEN ',{s}' ELSE '' END"
        for s in SOURCE_PRIORITY) + ', 2)'
    joins = '\n'.join(
        f"LEFT JOIN title_overrides {_alias(s)} ON {_alias(s)}.id = ids.id "
        f"AND {_alias(s)}.source = '{s}'" for s in SOURCE_PRIORITY)
    return f'''
        INSERT OR REPLACE INTO titles (id, source, sources, {", ".join(_META_COLUMNS)})
        SELECT ids.id, {source} AS source, {sources} AS sources, {cols}
        FROM (SELECT DISTINCT id FROM title_overrides{" WHERE id = :id" if single_id else ""}) ids
        {joins}
    '''


def _snapshot_sql(single_id):
    """Copy the pristine titledb row of every overridden id into title_overrides.

    Taken before the overrides are merged in, so the merge always has the untouched
    titledb values to fall back on - and so dropping an override restores them.
    """
    cols = ', '.join(_META_COLUMNS)
    where = 'WHERE t.id = :id' if single_id else 'WHERE t.id IN (SELECT id FROM title_overrides)'
    # OR IGNORE, never REPLACE: once an id is overridden its `titles` row holds merged
    # values, and re-snapshotting those would enshrine them as the titledb baseline.
    return f'''
        INSERT OR IGNORE INTO title_overrides (id, source, {cols})
        SELECT t.id, '{SOURCE_TITLEDB}', {cols} FROM titles t {where}
    '''


def _apply_overrides(conn):
    """Snapshot the titledb rows of overridden ids, then merge every source into titles."""
    conn.execute(_snapshot_sql(single_id=False))
    conn.execute(_merge_sql(single_id=False))


def _recompute_title(conn, title_id):
    """Re-merge a single id after its override rows changed."""
    has_override = conn.execute(
        'SELECT 1 FROM title_overrides WHERE id = ? AND source != ? LIMIT 1',
        (title_id, SOURCE_TITLEDB),
    ).fetchone()
    if not has_override:
        # Last override gone: restore the titledb snapshot, or drop the row entirely when
        # the id only ever existed because of the override.
        conn.execute(_merge_sql(single_id=True), {'id': title_id})
        conn.execute('DELETE FROM title_overrides WHERE id = ?', (title_id,))
        conn.execute(
            "DELETE FROM titles WHERE id = ? AND source != ?", (title_id, SOURCE_TITLEDB))
        return
    conn.execute(_snapshot_sql(single_id=True), {'id': title_id})
    conn.execute(_merge_sql(single_id=True), {'id': title_id})


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def import_from_json(region_file, locale):
    """(Re)build ``titles.db`` from the downloaded JSON files.

    Builds into a ``titles.db.new`` file and atomically renames it, so any in-flight
    reader connections keep seeing the old DB until they close. The durable overrides in
    ownfoil.db are projected in and merged before the swap.
    """
    titledb_dir = os.path.dirname(region_file)
    cnmts_file = os.path.join(titledb_dir, 'cnmts.json')
    versions_file = os.path.join(titledb_dir, 'versions.json')

    for path in (region_file, cnmts_file, versions_file):
        if not os.path.isfile(path):
            logger.warning(f'Cannot build titles.db, missing file: {path}')
            return

    new_path = TITLES_DB_FILE + '.new'
    if os.path.exists(new_path):
        os.remove(new_path)

    logger.info('Building titles.db from titledb JSON files ...')
    create_titledb(new_path)
    conn = sqlite3.connect(new_path)
    try:
        conn.execute('PRAGMA journal_mode=OFF')
        conn.execute('PRAGMA synchronous=OFF')

        _import_titles(conn, region_file)
        _import_cnmts(conn, cnmts_file)
        _import_versions(conn, versions_file)
        _import_overrides(conn)
        _apply_overrides(conn)

        _set_meta(conn, 'imported_locale', locale)
        _set_meta(conn, 'imported_at', _now())
        conn.commit()
    finally:
        conn.close()

    _replace_titles_db(new_path)
    logger.info('titles.db build complete.')


def _replace_titles_db(new_path):
    """Move the freshly built DB into place, atomically for readers.

    Windows refuses to replace a file that other open handles still hold, and every pooled
    main connection keeps titles.db ATTACHed, so drop those handles and retry there. That
    retry needs a Flask app context, which callers have via the worker's task loop.
    """
    for attempt in range(3):
        try:
            os.replace(new_path, TITLES_DB_FILE)
            return
        except PermissionError:
            if attempt == 2:
                raise
            from db import db
            db.engine.dispose()  # closes idle pooled connections; in-flight ones on return
            time.sleep(1)


def _import_titles(conn, path):
    cols = ['"id"', 'source', 'sources'] + [f'"{c}"' for _, c, _ in _TITLES_COLUMNS if c != 'id']
    placeholders = ','.join('?' * len(cols))
    sql = f'INSERT OR IGNORE INTO titles ({",".join(cols)}) VALUES ({placeholders})'

    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    batch = []
    count = 0
    id_col_index = next(i for i, (_, c, _) in enumerate(_TITLES_COLUMNS) if c == 'id')
    for _key, record in data.items():
        if not isinstance(record, dict):
            continue
        row = _encode_row(record, _TITLES_COLUMNS)
        if row[id_col_index] is None:
            continue
        # Column order: id, source, sources, then the remaining titles columns
        # (in _TITLES_COLUMNS order minus id)
        rest = [v for i, v in enumerate(row) if i != id_col_index]
        batch.append([row[id_col_index], SOURCE_TITLEDB, SOURCE_TITLEDB] + rest)
        if len(batch) >= 5000:
            conn.executemany(sql, batch)
            count += len(batch)
            batch.clear()
    if batch:
        conn.executemany(sql, batch)
        count += len(batch)
    logger.info(f'  titles: {count} rows')


def _import_cnmts(conn, path):
    cols = ['app_id', 'cnmt_version'] + [f'"{c}"' for _, c, _ in _CNMTS_COLUMNS]
    placeholders = ','.join('?' * len(cols))
    sql = f'INSERT OR IGNORE INTO cnmts ({",".join(cols)}) VALUES ({placeholders})'

    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    batch = []
    count = 0
    for app_id, versions in data.items():
        if not isinstance(versions, dict):
            continue
        for cnmt_version, record in versions.items():
            if not isinstance(record, dict):
                continue
            row = _encode_row(record, _CNMTS_COLUMNS)
            batch.append([app_id, cnmt_version] + row)
            if len(batch) >= 5000:
                conn.executemany(sql, batch)
                count += len(batch)
                batch.clear()
    if batch:
        conn.executemany(sql, batch)
        count += len(batch)
    logger.info(f'  cnmts: {count} rows')


def _import_versions(conn, path):
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    batch = []
    for title_id, versions in data.items():
        if not isinstance(versions, dict):
            continue
        for version, release_date in versions.items():
            try:
                version_int = int(version)
            except (TypeError, ValueError):
                continue
            batch.append((title_id.upper(), version_int, release_date))
    conn.executemany(
        'INSERT OR IGNORE INTO versions(title_id, version, release_date) VALUES (?, ?, ?)',
        batch,
    )
    logger.info(f'  versions: {len(batch)} rows')


def _import_overrides(conn):
    """Copy the durable overrides out of ownfoil.db into the DB being built."""
    if not os.path.isfile(DB_FILE):
        return
    cols = ', '.join(['id', 'source'] + _META_COLUMNS)
    conn.execute('ATTACH ? AS ownfoil', (DB_FILE,))
    try:
        count = conn.execute(
            f'INSERT OR REPLACE INTO title_overrides ({cols}) '
            f'SELECT {cols} FROM ownfoil.title_overrides'
        ).rowcount
    finally:
        conn.commit()  # DETACH is refused while the INSERT's transaction is still open
        conn.execute('DETACH ownfoil')
    if count:
        logger.info(f'  overrides: {count} rows')


# ---------------------------------------------------------------------------
# Overrides: durable in ownfoil.db, projected here
# ---------------------------------------------------------------------------

def _project_override(title_id, source, record):
    """Mirror one override row into titles.db and re-merge that title."""
    if not os.path.isfile(TITLES_DB_FILE):
        return
    with contextlib.closing(sqlite3.connect(TITLES_DB_FILE)) as conn:
        if record is None:
            conn.execute('DELETE FROM title_overrides WHERE id = ? AND source = ?',
                         (title_id, source))
        else:
            cols = ['id', 'source'] + _META_COLUMNS
            placeholders = ','.join('?' * len(cols))
            values = [title_id, source] + _encode_row(record, _OVERRIDE_COLUMNS)
            conn.execute(
                f'INSERT OR REPLACE INTO title_overrides ({",".join(cols)}) '
                f'VALUES ({placeholders})', values)
        _recompute_title(conn, title_id)
        _set_meta(conn, 'imported_at', _now())  # invalidates the cached GraphQL responses
        conn.commit()


def set_override(title_id, record, source=SOURCE_CUSTOM):
    """Persist a metadata override and apply it to titles.db. Returns (ok, error)."""
    if not title_id:
        return False, 'id is required'
    if source not in OVERRIDE_SOURCES:
        return False, f'Unknown metadata source: {source}'
    from db import upsert_title_override
    upsert_title_override(title_id, source, _override_values(record))
    _project_override(title_id, source, record)
    return True, None


def delete_override(title_id, source=SOURCE_CUSTOM):
    """Drop an override, restoring the values of the next source down. Returns (ok, error)."""
    from db import delete_title_override
    if not delete_title_override(title_id, source):
        return False, f'No {source} entry for {title_id}'
    _project_override(title_id, source, None)
    return True, None


def list_overrides(source=SOURCE_CUSTOM):
    """{title_id: record} of the durable overrides for a source."""
    from db import list_title_overrides
    return {row['id']: _decode_record(row) for row in list_title_overrides(source)}


def _override_values(record):
    """The record as override columns, JSON-encoding the list/object fields."""
    return dict(zip(_META_COLUMNS, _encode_row(record, _OVERRIDE_COLUMNS)))


def _decode_record(row):
    """An override row back to its JSON shape, id included, dropping unset fields."""
    out = {'id': row['id']}
    for json_key, col, kind in _TITLES_COLUMNS:
        if col == 'id' or row[col] is None:
            continue
        v = row[col]
        if kind == 'j':
            with contextlib.suppress(Exception):
                v = json.loads(v)
        out[json_key] = v
    return out


def _now():
    return datetime.datetime.utcnow().isoformat(timespec='seconds') + 'Z'


# ---------------------------------------------------------------------------
# Query layer
# ---------------------------------------------------------------------------

def get_imported_locale():
    """Return the locale string (e.g. 'US.en') stored in titles.db, or None."""
    conn = _connect_ro()
    if conn is None:
        return None
    try:
        return _get_meta(conn, 'imported_locale')
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _connect_ro():
    """Read-only connection. Returns None if the DB doesn't exist yet."""
    if not os.path.isfile(TITLES_DB_FILE):
        return None
    uri = f'file:{TITLES_DB_FILE}?mode=ro'
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _decode_row(row, columns):
    """Turn a sqlite row back into the JSON-shaped dict used by callers."""
    if row is None:
        return None
    out = {}
    for json_key, col, kind in columns:
        v = row[col]
        if kind == 'j' and v is not None:
            try:
                v = json.loads(v)
            except Exception:
                pass
        out[json_key] = v
    return out


def get_title_record(title_id):
    """Full merged title record. Returns None if missing."""
    conn = _connect_ro()
    if conn is None:
        return None
    try:
        row = conn.execute('SELECT * FROM titles WHERE "id" = ?', (title_id,)).fetchone()
        return _decode_row(row, _TITLES_COLUMNS)
    finally:
        conn.close()


def get_game_info(title_id):
    """Compatibility shim returning the subset used by identify/organize code."""
    rec = get_title_record(title_id)
    if rec is None:
        return {
            'name': 'Unrecognized',
            'bannerUrl': '//placehold.it/400x200',
            'iconUrl': '',
            'id': str(title_id) + ' not found in titledb',
            'category': '',
        }
    return {
        'name': rec.get('name'),
        'bannerUrl': rec.get('bannerUrl'),
        'iconUrl': rec.get('iconUrl'),
        'id': rec.get('id'),
        'category': rec.get('category'),
    }


def get_cnmt_latest(app_id):
    """Return the cnmts record with the highest numeric cnmt_version for app_id."""
    conn = _connect_ro()
    if conn is None:
        return None
    try:
        row = conn.execute(
            'SELECT * FROM cnmts WHERE app_id = ? '
            'ORDER BY CAST(cnmt_version AS INTEGER) DESC LIMIT 1',
            (app_id.lower(),),
        ).fetchone()
        out = _decode_row(row, _CNMTS_COLUMNS)
        if out is None:
            return None
        out['app_id'] = row['app_id']
        out['cnmt_version'] = row['cnmt_version']
        return out
    finally:
        conn.close()


def get_all_existing_versions(title_id):
    conn = _connect_ro()
    if conn is None:
        return []
    try:
        row = conn.execute(
            'SELECT release_date FROM titles WHERE id = ?',
            (title_id.upper(),),
        ).fetchone()

        all_versions = []

        if row and row['release_date']:
            # Add the "version 0" entry from the titles table
            rd = str(row['release_date'])
            if rd:
                rd = f"{rd[:4]}-{rd[4:6]}-{rd[6:8]}"
            else:
                rd = None
            all_versions.append({
                'version': 0,
                'update_number': 0,
                'release_date': rd,
            })

        rows = conn.execute(
            'SELECT version, release_date FROM versions WHERE title_id = ?',
            (title_id.upper(),),
        ).fetchall()

        # Add other versions
        for r in rows:
            all_versions.append({
                'version': r['version'],
                'update_number': int(r['version']) // 65536,
                'release_date': r['release_date'],
            })

        return all_versions
    finally:
        conn.close()


def get_all_app_existing_versions(app_id):
    conn = _connect_ro()
    if conn is None:
        return None
    try:
        rows = conn.execute(
            'SELECT cnmt_version FROM cnmts WHERE app_id = ? ORDER BY cnmt_version',
            (app_id.lower(),),
        ).fetchall()
        if not rows:
            return None
        return [r['cnmt_version'] for r in rows]
    finally:
        conn.close()


def get_all_dlc_versions(title_id):
    """Return [(app_id_upper, cnmt_version, release_date), ...] for every DLC of the given title."""
    conn = _connect_ro()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            'SELECT upper(c.app_id) AS app_id, c.cnmt_version, td.release_date '
            'FROM cnmts c '
            'LEFT JOIN titles td ON td.id = upper(c.app_id) '
            'WHERE c.other_application_id = ? AND c.title_type = 130',
            (title_id.lower(),),
        ).fetchall()
        out = []
        for r in rows:
            rd = r['release_date']
            if rd:
                rd = str(rd)
                rd = f"{rd[:4]}-{rd[4:6]}-{rd[6:8]}"
            else:
                rd = None
            out.append((r['app_id'], r['cnmt_version'], rd))
        return out
    finally:
        conn.close()


def get_all_existing_dlc(title_id):
    conn = _connect_ro()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            'SELECT DISTINCT app_id FROM cnmts WHERE other_application_id = ? AND title_type = 130',
            (title_id.lower(),),
        ).fetchall()
        return [r['app_id'].upper() for r in rows]
    finally:
        conn.close()
