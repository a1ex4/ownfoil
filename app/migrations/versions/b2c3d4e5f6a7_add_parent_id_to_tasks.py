"""Add parent_id to tasks table

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'b2c3d4e5f6a7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('tasks', sa.Column('parent_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_tasks_parent_id', 'tasks', 'tasks', ['parent_id'], ['id'])
    op.create_index('ix_tasks_parent_id', 'tasks', ['parent_id'])


def downgrade():
    op.drop_index('ix_tasks_parent_id', table_name='tasks')
    op.drop_constraint('fk_tasks_parent_id', 'tasks', type_='foreignkey')
    op.drop_column('tasks', 'parent_id')
