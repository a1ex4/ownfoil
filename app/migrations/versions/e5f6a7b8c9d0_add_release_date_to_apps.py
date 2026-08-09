"""Add release_date column to apps

Revision ID: e5f6a7b8c9d0
Revises: f6a7b8c9d0e1

Lets the GraphQL App type carry version metadata directly, eliminating the
separate availableVersions field and its per-page join against
titledb.versions.
"""
from alembic import op
import sqlalchemy as sa


revision = 'e5f6a7b8c9d0'
down_revision = 'f6a7b8c9d0e1'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('apps', sa.Column('release_date', sa.String(), nullable=True))


def downgrade():
    op.drop_column('apps', 'release_date')
