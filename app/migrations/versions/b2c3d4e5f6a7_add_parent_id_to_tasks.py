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
    with op.batch_alter_table('tasks') as batch_op:
        batch_op.add_column(sa.Column('parent_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_tasks_parent_id', 'tasks', ['parent_id'], ['id'])
        batch_op.create_index('ix_tasks_parent_id', ['parent_id'])


def downgrade():
    with op.batch_alter_table('tasks') as batch_op:
        batch_op.drop_index('ix_tasks_parent_id')
        batch_op.drop_constraint('fk_tasks_parent_id', type_='foreignkey')
        batch_op.drop_column('parent_id')
