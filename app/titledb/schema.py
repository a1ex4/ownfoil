"""SQLAlchemy schema for ``config/titles.db``.

Single source of truth for creation (``metadata.create_all``), for the merge SQL that
resolves the metadata sources against each other, and for the ``title_overrides`` table
ownfoil.db keeps the durable copy in. Imports nothing but sqlalchemy and the stdlib.
"""
import hashlib

import sqlalchemy as sa

metadata = sa.MetaData()

# Metadata sources, highest priority first: a field is taken from the first source that
# has a value for it. 'titledb' is the downloaded eShop dump, 'custom' is user-authored,
# 'extract' is read out of the files themselves. Everything that merges sources is
# generated from this tuple, so a new source is an entry here plus a producer.
SOURCE_PRIORITY = ('custom', 'extract', 'titledb')
SOURCE_TITLEDB = 'titledb'
SOURCE_CUSTOM = 'custom'
SOURCE_EXTRACT = 'extract'

# Sources whose rows are user/library data rather than a copy of the download. These live
# durably in ownfoil.db and are projected into titles.db on every rebuild.
OVERRIDE_SOURCES = tuple(s for s in SOURCE_PRIORITY if s != SOURCE_TITLEDB)


def _col(name, json_key, type_=sa.Text, json=False, **kw):
    """A column whose value comes from the titledb JSON, tagged with its source key."""
    return sa.Column(name, type_, info={'json_key': json_key, 'json': json}, **kw)


def metadata_columns():
    """Fresh Column objects for the title metadata fields.

    A factory, not a list: Columns cannot be shared between tables, and these same fields
    make up ``titles``, ``title_overrides`` and the ownfoil.db override table.
    """
    return [
        _col('name', 'name'),
        _col('banner_url', 'bannerUrl'),
        _col('icon_url', 'iconUrl'),
        _col('front_box_art', 'frontBoxArt'),
        _col('description', 'description'),
        _col('intro', 'intro'),
        _col('developer', 'developer'),
        _col('publisher', 'publisher'),
        _col('release_date', 'releaseDate'),
        _col('category', 'category', json=True),
        _col('is_demo', 'isDemo'),
        _col('nsu_id', 'nsuId'),
        _col('number_of_players', 'numberOfPlayers'),
        _col('parent_id', 'parentId'),
        _col('rank', 'rank'),
        _col('rating', 'rating'),
        _col('rating_content', 'ratingContent', json=True),
        _col('region', 'region'),
        _col('regions', 'regions', json=True),
        _col('languages', 'languages', json=True),
        _col('language', 'language'),
        _col('rights_id', 'rightsId'),
        _col('screenshots', 'screenshots', json=True),
        _col('size', 'size'),
        _col('version', 'version'),
        _col('nca_key', 'key'),
        _col('ids', 'ids', json=True),
    ]


def column_map(table):
    """[(json_key, column, 's'|'j'), ...] for the columns fed from the titledb JSON."""
    return [(c.info['json_key'], c.name, 'j' if c.info['json'] else 's')
            for c in table.c if 'json_key' in c.info]


# One row per title id, holding the values already merged across the sources: the read path
# joins this directly, with no dedup predicate. `source` is the highest-priority source that
# contributed a field (what the GraphQL Title.source exposes), `sources` lists them all.
titles = sa.Table(
    'titles', metadata,
    _col('id', 'id', primary_key=True),
    sa.Column('source', sa.Text, nullable=False, server_default=sa.text(f"'{SOURCE_TITLEDB}'")),
    sa.Column('sources', sa.Text),
    *metadata_columns(),
)

# The raw per-source rows, kept only for ids that actually have an override, plus a
# 'titledb' snapshot of the pristine row for those same ids - that snapshot is what lets
# removing an override restore the original values without re-importing the JSON.
title_overrides = sa.Table(
    'title_overrides', metadata,
    sa.Column('id', sa.Text, primary_key=True),
    sa.Column('source', sa.Text, primary_key=True),
    *metadata_columns(),
)

cnmts = sa.Table(
    'cnmts', metadata,
    sa.Column('app_id', sa.Text, primary_key=True),
    sa.Column('cnmt_version', sa.Text, primary_key=True),
    _col('title_id', 'titleId'),
    # Integer, not Text: these are numbers in the JSON and are read back as numbers -
    # identify_appId compares titleType against 128/129/130, and TEXT affinity would
    # store '129', which matches nothing. cnmt_version stays Text: it is the JSON key,
    # and get_cnmt_latest orders it with CAST(... AS INTEGER).
    _col('title_type', 'titleType', sa.Integer),
    _col('version', 'version', sa.Integer),
    _col('other_application_id', 'otherApplicationId'),
    _col('required_application_version', 'requiredApplicationVersion', sa.Integer),
    _col('required_system_version', 'requiredSystemVersion', sa.Integer),
    _col('content_entries', 'contentEntries', json=True),
    _col('meta_entries', 'metaEntries', json=True),
    sa.Index('idx_cnmts_app_id', 'app_id'),
    sa.Index('idx_cnmts_dlc_lookup', 'other_application_id', 'title_type'),
)

versions = sa.Table(
    'versions', metadata,
    sa.Column('title_id', sa.Text, primary_key=True),
    sa.Column('version', sa.Integer, primary_key=True),
    sa.Column('release_date', sa.Text),
)

meta = sa.Table(
    'meta', metadata,
    sa.Column('key', sa.Text, primary_key=True),
    sa.Column('value', sa.Text),
)


def fingerprint():
    """Hash of the schema DDL, stored in meta so a changed schema forces a rebuild.

    titles.db is fully derivable, so it is versioned by what it should look like rather
    than by a migration chain: no match, recreate it and let the next update re-import.
    """
    from sqlalchemy.dialects import sqlite
    from sqlalchemy.schema import CreateIndex, CreateTable
    dialect = sqlite.dialect()
    ddl = [str(CreateTable(t).compile(dialect=dialect)) for t in metadata.tables.values()]
    ddl += [str(CreateIndex(i).compile(dialect=dialect))
            for t in metadata.tables.values() for i in t.indexes]
    return hashlib.sha256('\n'.join(sorted(ddl)).encode()).hexdigest()[:16]
