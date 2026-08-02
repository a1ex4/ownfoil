"""Alembic environment for config/titles.db.

Standalone on purpose: unlike migrations/env.py this one never touches Flask, so it can run
both from the CLI and from titledb_store, which passes a live connection through
config.attributes rather than a URL (a '%' in OWNFOIL_CONFIG_DIR would break interpolation).
"""
import logging
import os
import sys

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import titledb_schema

config = context.config
target_metadata = titledb_schema.metadata

# Retrieve main logger. fileConfig() is deliberately not called: it would disable it.
logger = logging.getLogger('main')


def process_revision_directives(context, revision, directives):
    """Don't emit a revision file when autogenerate found no change."""
    if getattr(config.cmd_opts, 'autogenerate', False):
        script = directives[0]
        if script.upgrade_ops.is_empty():
            directives[:] = []
            logger.info('No changes in titledb schema detected.')


def run_migrations_offline():
    context.configure(
        url=config.get_main_option('sqlalchemy.url'),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connection = config.attributes.get('connection')
    if connection is not None:
        _run(connection)
        return
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix='sqlalchemy.',
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _run(connection)


def _run(connection):
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,  # SQLite has no real ALTER
        process_revision_directives=process_revision_directives,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
