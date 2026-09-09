"""Add download_token to files

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0

/api/get_game/<id> addresses a file by its auto-increment primary key, which a client
can enumerate. The token is the unguessable name /api/download/<token> uses instead.
Existing rows are backfilled one at a time: a token derived from anything already in
the row (the path, the id) would be as guessable as the id it replaces.
"""
import secrets

import sqlalchemy as sa
from alembic import op


revision = 'd6e7f8a9b0c1'
down_revision = 'c5d6e7f8a9b0'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('files', sa.Column('download_token', sa.String(), nullable=True))
    conn = op.get_bind()
    for (file_id,) in conn.execute(sa.text("SELECT id FROM files")).all():
        conn.execute(sa.text("UPDATE files SET download_token = :t WHERE id = :i"),
                     {"t": secrets.token_urlsafe(16), "i": file_id})
    op.create_index('ix_files_download_token', 'files', ['download_token'], unique=True)


def downgrade():
    op.drop_index('ix_files_download_token', table_name='files')
    with op.batch_alter_table('files') as batch_op:
        batch_op.drop_column('download_token')
