"""Add hash_modified to files

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4

nstools decides CORRECT / MODIFIED / CORRUPT per content and then reports the last two as
one failed verdict, so `hash_valid = 0` covered both a repack whose contents are intact and
a genuinely damaged file. This column keeps the distinction. Nullable, and existing rows
stay NULL: a verdict recorded before it existed cannot say which of the two it was, and the
verify stage re-checks those rows rather than guessing.
"""
from alembic import op
import sqlalchemy as sa


revision = 'd0e1f2a3b4c5'
down_revision = 'c9d0e1f2a3b4'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('files', sa.Column('hash_modified', sa.Boolean(), nullable=True))


def downgrade():
    op.drop_column('files', 'hash_modified')
