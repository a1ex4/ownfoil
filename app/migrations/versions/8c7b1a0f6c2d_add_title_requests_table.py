"""

Revision ID: 8c7b1a0f6c2d
Revises: e3a1b7a2f1c0

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '8c7b1a0f6c2d'
down_revision = 'e3a1b7a2f1c0'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'title_requests',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('status', sa.String(), nullable=False, server_default='open'),
        sa.Column('title_id', sa.String(), nullable=False),
        sa.Column('title_name', sa.String(), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_title_requests_created_at', 'title_requests', ['created_at'])
    op.create_index('ix_title_requests_status', 'title_requests', ['status'])
    op.create_index('ix_title_requests_title_id', 'title_requests', ['title_id'])
    op.create_index('ix_title_requests_user_id', 'title_requests', ['user_id'])


def downgrade():
    op.drop_index('ix_title_requests_user_id', table_name='title_requests')
    op.drop_index('ix_title_requests_title_id', table_name='title_requests')
    op.drop_index('ix_title_requests_status', table_name='title_requests')
    op.drop_index('ix_title_requests_created_at', table_name='title_requests')
    op.drop_table('title_requests')
