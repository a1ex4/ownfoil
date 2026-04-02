"""Add ignored_events table

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'c3d4e5f6a7b8'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('ignored_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('src_path', sa.String(), nullable=False),
        sa.Column('dest_path', sa.String(), nullable=False, server_default=''),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade():
    op.drop_table('ignored_events')
