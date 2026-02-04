"""Add access events table

Revision ID: 2c9d2a6e3b41
Revises: 78c33e9bffce

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '2c9d2a6e3b41'
down_revision = '78c33e9bffce'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'access_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('at', sa.DateTime(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('user', sa.String(), nullable=True),
        sa.Column('remote_addr', sa.String(), nullable=True),
        sa.Column('user_agent', sa.String(), nullable=True),
        sa.Column('title_id', sa.String(), nullable=True),
        sa.Column('file_id', sa.Integer(), nullable=True),
        sa.Column('filename', sa.String(), nullable=True),
        sa.Column('bytes_sent', sa.Integer(), nullable=True),
        sa.Column('ok', sa.Boolean(), nullable=True),
        sa.Column('status_code', sa.Integer(), nullable=True),
        sa.Column('duration_ms', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_access_events_at', 'access_events', ['at'])
    op.create_index('ix_access_events_kind', 'access_events', ['kind'])


def downgrade():
    op.drop_index('ix_access_events_kind', table_name='access_events')
    op.drop_index('ix_access_events_at', table_name='access_events')
    op.drop_table('access_events')
