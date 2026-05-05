"""Add worker_id column to tasks

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
    op.add_column('tasks', sa.Column('worker_id', sa.Integer(), nullable=True))


def downgrade():
    op.drop_column('tasks', 'worker_id')
