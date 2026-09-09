"""Add extract_version to title_overrides

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9

The version the extract override was read from, so the newest update of a title wins its
metadata instead of whichever file the pipeline happened to read last. Existing rows were
written without one and are as likely to hold an older update's record as the newest, so
the extraction flag is cleared: one pass re-reads every file and the highest version wins.
"""
from alembic import op
import sqlalchemy as sa


revision = 'c5d6e7f8a9b0'
down_revision = 'b4c5d6e7f8a9'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('title_overrides', sa.Column('extract_version', sa.Integer(), nullable=True))
    op.execute('UPDATE files SET metadata_extracted = 0')


def downgrade():
    op.drop_column('title_overrides', 'extract_version')
