"""Add tasks table

Revision ID: a1b2c3d4e5f6
Revises: 78c33e9bffce

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = '78c33e9bffce'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('tasks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('task_name', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False, server_default='pending'),
        sa.Column('completion_pct', sa.Integer(), server_default='0'),
        sa.Column('input_json', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('input_hash', sa.String(length=64), nullable=False),
        sa.Column('output_json', sa.Text(), nullable=True),
        sa.Column('exit_code', sa.Integer(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_tasks_task_name', 'tasks', ['task_name'])
    op.create_index('ix_tasks_status_created', 'tasks', ['status', 'created_at'])


def downgrade():
    op.drop_index('ix_tasks_status_created', table_name='tasks')
    op.drop_index('ix_tasks_task_name', table_name='tasks')
    op.drop_table('tasks')
