"""add title request views table

Revision ID: 4d4d6d0e59e0
Revises: 8c7b1a0f6c2d
Create Date: 2026-01-30

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '4d4d6d0e59e0'
down_revision = '8c7b1a0f6c2d'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'title_request_views',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id', ondelete='CASCADE'), nullable=False),
        sa.Column('request_id', sa.Integer(), sa.ForeignKey('title_requests.id', ondelete='CASCADE'), nullable=False),
        sa.Column('viewed_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('user_id', 'request_id', name='uq_title_request_views_user_request'),
    )
    op.create_index('ix_title_request_views_user_id', 'title_request_views', ['user_id'])
    op.create_index('ix_title_request_views_request_id', 'title_request_views', ['request_id'])
    op.create_index('ix_title_request_views_viewed_at', 'title_request_views', ['viewed_at'])


def downgrade():
    op.drop_index('ix_title_request_views_viewed_at', table_name='title_request_views')
    op.drop_index('ix_title_request_views_request_id', table_name='title_request_views')
    op.drop_index('ix_title_request_views_user_id', table_name='title_request_views')
    op.drop_table('title_request_views')
