"""Add added_at column to files

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2

"Recently added" was not expressible: mtime is the filesystem's modification time,
which a re-organize or a copy rewrites, so it does not track when ownfoil first saw
the file. Existing rows are backfilled from mtime as the closest available proxy.
"""
from alembic import op
import sqlalchemy as sa


revision = 'b8c9d0e1f2a3'
down_revision = 'a7b8c9d0e1f2'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('files', sa.Column('added_at', sa.DateTime(), nullable=True))
    op.execute(
        "UPDATE files SET added_at = datetime(mtime, 'unixepoch') "
        "WHERE added_at IS NULL AND mtime IS NOT NULL"
    )


def downgrade():
    op.drop_column('files', 'added_at')
