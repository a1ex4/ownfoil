"""SQLite-backed store for titledb JSON data.

Replaces the in-memory TitleDB loaded from JSON files with a disposable
SQLite database (``config/titles.db``) built from the downloaded JSON files.
Custom admin-added entries are persisted separately in
``config/custom_titles.json`` and merged into the DB on every rebuild.
"""
import contextlib
import json
import logging
import os
import sqlite3

from constants import TITLEDB_DIR, TITLES_DB_FILE, CUSTOM_TITLES_FILE
import titledb

logger = logging.getLogger('main')

SOURCE_UPSTREAM = 'upstream'
SOURCE_CUSTOM = 'custom'

# Columns for the titles table. (json_key, column_name, json_type)
#   json_type: 's'=scalar (stored as-is), 'j'=list/object (json-encoded)
_TITLES_COLUMNS = [
    ('id',                'id',                's'),
    ('name',              'name',              's'),
    ('bannerUrl',         'banner_url',        's'),
    ('iconUrl',           'icon_url',          's'),
    ('frontBoxArt',       'front_box_art',     's'),
    ('description',       'description',       's'),
    ('intro',             'intro',             's'),
    ('developer',         'developer',         's'),
    ('publisher',         'publisher',         's'),
    ('releaseDate',       'release_date',      's'),
    ('category',          'category',          'j'),
    ('isDemo',            'is_demo',           's'),
    ('nsuId',             'nsu_id',            's'),
    ('numberOfPlayers',   'number_of_players', 's'),
    ('parentId',          'parent_id',         's'),
    ('rank',              'rank',              's'),
    ('rating',            'rating',            's'),
    ('ratingContent',     'rating_content',    'j'),
    ('region',            'region',            's'),
    ('regions',           'regions',           'j'),
    ('languages',         'languages',         'j'),
    ('language',          'language',          's'),
    ('rightsId',          'rights_id',         's'),
    ('screenshots',       'screenshots',       'j'),
    ('size',              'size',              's'),
    ('version',           'version',           's'),
    ('key',               'nca_key',           's'),
    ('ids',               'ids',               'j'),
]

_CNMTS_COLUMNS = [
    # (json_key, column_name, json_type)
    ('titleId',                    'title_id',                     's'),
    ('titleType',                  'title_type',                   's'),
    ('version',                    'version',                      's'),
    ('otherApplicationId',         'other_application_id',         's'),
    ('requiredApplicationVersion', 'required_application_version', 's'),
    ('requiredSystemVersion',      'required_system_version',      's'),
    ('contentEntries',             'content_entries',              'j'),
    ('metaEntries',                'meta_entries',                 'j'),
]


def _titles_schema():
    cols = ',\n    '.join(f'"{c}"' for _, c, _ in _TITLES_COLUMNS if c != 'id')
    return f'''
    CREATE TABLE titles (
        "id" TEXT NOT NULL,
        source TEXT NOT NULL,
        {cols},
        PRIMARY KEY ("id", source)
    );
    CREATE INDEX idx_titles_id ON titles("id");
    '''


def _cnmts_schema():
    cols = ',\n    '.join(f'"{c}"' for _, c, _ in _CNMTS_COLUMNS)
    return f'''
    CREATE TABLE cnmts (
        app_id TEXT NOT NULL,
        cnmt_version TEXT NOT NULL,
        {cols},
        PRIMARY KEY (app_id, cnmt_version)
    );
    CREATE INDEX idx_cnmts_app_id     ON cnmts(app_id);
    CREATE INDEX idx_cnmts_dlc_lookup ON cnmts(other_application_id, title_type);
    '''


_SCHEMA = _titles_schema() + _cnmts_schema() + '''
CREATE TABLE versions (
    title_id     TEXT NOT NULL,
    version      INTEGER NOT NULL,
    release_date TEXT,
    PRIMARY KEY (title_id, version)
);

CREATE TABLE versions_txt (
    app_id  TEXT PRIMARY KEY,
    version TEXT NOT NULL
);

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
'''


def _encode_row(record, columns):
    out = []
    for json_key, _col, kind in columns:
        v = record.get(json_key)
        if kind == 'j' and v is not None:
            v = json.dumps(v, separators=(',', ':'))
        out.append(v)
    return out


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def import_from_json(app_settings):
    """(Re)build ``titles.db`` from the downloaded JSON files in TITLEDB_DIR.

    Builds into a ``titles.db.new`` file and atomically renames it, so any
    in-flight reader connections keep seeing the old DB until they close.
    Custom entries are re-imported from ``custom_titles.json`` at the end.
    """
    region_file = os.path.join(TITLEDB_DIR, titledb.get_region_titles_file(app_settings))
    cnmts_file = os.path.join(TITLEDB_DIR, 'cnmts.json')
    versions_file = os.path.join(TITLEDB_DIR, 'versions.json')
    versions_txt_file = os.path.join(TITLEDB_DIR, 'versions.txt')

    for path in (region_file, cnmts_file, versions_file, versions_txt_file):
        if not os.path.isfile(path):
            logger.warning(f'Cannot build titles.db, missing file: {path}')
            return

    new_path = TITLES_DB_FILE + '.new'
    if os.path.exists(new_path):
        os.remove(new_path)

    logger.info('Building titles.db from titledb JSON files ...')
    conn = sqlite3.connect(new_path)
    try:
        conn.execute('PRAGMA journal_mode=OFF')
        conn.execute('PRAGMA synchronous=OFF')
        conn.executescript(_SCHEMA)

        _import_titles(conn, region_file)
        _import_cnmts(conn, cnmts_file)
        _import_versions(conn, versions_file)
        _import_versions_txt(conn, versions_txt_file)
        _import_customs(conn)

        conn.execute(
            'INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)',
            ('imported_locale', f"{app_settings['titles']['region']}.{app_settings['titles']['language']}"),
        )
        conn.commit()
    finally:
        conn.close()

    os.replace(new_path, TITLES_DB_FILE)
    logger.info('titles.db build complete.')


def _import_titles(conn, path):
    cols = ['"id"', 'source'] + [f'"{c}"' for _, c, _ in _TITLES_COLUMNS if c != 'id']
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
        # Column order: id, source, then remaining titles columns (in _TITLES_COLUMNS order minus id)
        rest = [v for i, v in enumerate(row) if i != id_col_index]
        batch.append([row[id_col_index], SOURCE_UPSTREAM] + rest)
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
    count = 0
    for title_id, versions in data.items():
        if not isinstance(versions, dict):
            continue
        for version, release_date in versions.items():
            try:
                version_int = int(version)
            except (TypeError, ValueError):
                continue
            batch.append((title_id, version_int, release_date))
    conn.executemany(
        'INSERT OR IGNORE INTO versions(title_id, version, release_date) VALUES (?, ?, ?)',
        batch,
    )
    count = len(batch)
    logger.info(f'  versions: {count} rows')


def _import_versions_txt(conn, path):
    batch = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.rstrip('\n')
            if not line:
                continue
            parts = line.split('|')
            if len(parts) < 3:
                continue
            app_id = parts[0]
            version = parts[2] or '0'
            batch.append((app_id, version))
    conn.executemany(
        'INSERT OR REPLACE INTO versions_txt(app_id, version) VALUES (?, ?)',
        batch,
    )
    logger.info(f'  versions_txt: {len(batch)} rows')


def _import_customs(conn):
    records = _load_custom_titles()
    if not records:
        return
    for record in records.values():
        _upsert_custom_title(conn, record)
    logger.info(f'  custom titles: {len(records)} rows')


# ---------------------------------------------------------------------------
# Custom entries
# ---------------------------------------------------------------------------

def _load_custom_titles():
    if not os.path.isfile(CUSTOM_TITLES_FILE):
        return {}
    try:
        with open(CUSTOM_TITLES_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception as e:
        logger.error(f'Failed to load {CUSTOM_TITLES_FILE}: {e}')
        return {}


def _save_custom_titles(records):
    tmp = CUSTOM_TITLES_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CUSTOM_TITLES_FILE)


def _upsert_custom_title(conn, record):
    rec = dict(record)
    rec.setdefault('id', record.get('id'))
    cols = ['"id"', 'source'] + [f'"{c}"' for _, c, _ in _TITLES_COLUMNS if c != 'id']
    placeholders = ','.join('?' * len(cols))
    sql = f'INSERT OR REPLACE INTO titles ({",".join(cols)}) VALUES ({placeholders})'
    row = _encode_row(rec, _TITLES_COLUMNS)
    id_col_index = next(i for i, (_, c, _) in enumerate(_TITLES_COLUMNS) if c == 'id')
    rest = [v for i, v in enumerate(row) if i != id_col_index]
    conn.execute(sql, [row[id_col_index], SOURCE_CUSTOM] + rest)


def list_custom_titles():
    return _load_custom_titles()


def add_custom_title(record):
    """Persist a new custom title and upsert it into titles.db. Returns (ok, error)."""
    title_id = record.get('id')
    if not title_id:
        return False, 'id is required'
    records = _load_custom_titles()
    if title_id in records:
        return False, f'Custom entry already exists for {title_id}'
    records[title_id] = record
    _save_custom_titles(records)

    if os.path.isfile(TITLES_DB_FILE):
        with contextlib.closing(sqlite3.connect(TITLES_DB_FILE)) as conn:
            _upsert_custom_title(conn, record)
            conn.commit()
    return True, None


def delete_custom_title(title_id):
    records = _load_custom_titles()
    if title_id not in records:
        return False, f'No custom entry for {title_id}'
    del records[title_id]
    _save_custom_titles(records)

    if os.path.isfile(TITLES_DB_FILE):
        with contextlib.closing(sqlite3.connect(TITLES_DB_FILE)) as conn:
            conn.execute('DELETE FROM titles WHERE "id" = ? AND source = ?', (title_id, SOURCE_CUSTOM))
            conn.commit()
    return True, None


# ---------------------------------------------------------------------------
# Query layer
# ---------------------------------------------------------------------------

def get_imported_locale():
    """Return the locale string (e.g. 'US.en') stored in titles.db, or None."""
    if not os.path.isfile(TITLES_DB_FILE):
        return None
    try:
        with contextlib.closing(sqlite3.connect(f'file:{TITLES_DB_FILE}?mode=ro', uri=True)) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'imported_locale'").fetchone()
            return row[0] if row else None
    except sqlite3.Error:
        return None


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
    """Full title record (custom preferred over upstream). Returns None if missing."""
    conn = _connect_ro()
    if conn is None:
        return None
    try:
        row = conn.execute(
            'SELECT * FROM titles WHERE "id" = ? '
            "ORDER BY CASE source WHEN 'custom' THEN 0 ELSE 1 END LIMIT 1",
            (title_id,),
        ).fetchone()
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
        rows = conn.execute(
            'SELECT version, release_date FROM versions WHERE title_id = ?',
            (title_id.lower(),),
        ).fetchall()
        return [
            {
                'version': r['version'],
                'update_number': int(r['version']) // 65536,
                'release_date': r['release_date'],
            }
            for r in rows
        ]
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


def get_app_id_version_from_versions_txt(app_id):
    conn = _connect_ro()
    if conn is None:
        return None
    try:
        row = conn.execute(
            'SELECT version FROM versions_txt WHERE app_id = ?', (app_id,)
        ).fetchone()
        return row['version'] if row else None
    finally:
        conn.close()


def get_all_dlc_versions(title_id):
    """Return [(app_id_upper, cnmt_version), ...] for every DLC of the given title."""
    conn = _connect_ro()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            'SELECT app_id, cnmt_version FROM cnmts '
            'WHERE other_application_id = ? AND title_type = 130',
            (title_id.lower(),),
        ).fetchall()
        return [(r['app_id'].upper(), r['cnmt_version']) for r in rows]
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
