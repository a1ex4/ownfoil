"""Add file metadata extraction columns

Revision ID: a3b4c5d6e7f8
Revises: c9d0e1f2a3b4

Records what the per-file extract stage produces: the flag gating it, and the display
version its Control NCA declares - the only place a human version string exists, since
titledb only ever carries the numeric one.
"""
from alembic import op
import sqlalchemy as sa


revision = 'a3b4c5d6e7f8'
down_revision = 'c9d0e1f2a3b4'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('files', sa.Column('metadata_extracted', sa.Boolean(), nullable=True,
                                     server_default=sa.false()))
    op.add_column('apps', sa.Column('display_version', sa.String(), nullable=True))


def downgrade():
    op.drop_column('apps', 'display_version')
    op.drop_column('files', 'metadata_extracted')
