"""Add media

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8

Local copies of title artwork. The files under data/media are content-addressed and shared
between titles that use the same image, so nothing on disk names a title - this table is
the only record of which slot a stored file fills, which is why it lives in ownfoil.db
rather than in the derived titles.db.

One row per (title, kind, position): position is 0 for the slots a title has one of and the
array index for screenshots.
"""
from alembic import op
import sqlalchemy as sa


revision = 'b4c5d6e7f8a9'
down_revision = 'a3b4c5d6e7f8'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'media',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('title_id', sa.String(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('source_url', sa.String(), nullable=True),
        sa.Column('filename', sa.String(), nullable=False),
        sa.Column('width', sa.Integer(), nullable=True),
        sa.Column('height', sa.Integer(), nullable=True),
        sa.Column('client_width', sa.Integer(), nullable=True),
        sa.Column('client_height', sa.Integer(), nullable=True),
        sa.Column('downloaded_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('title_id', 'kind', 'position', name='uq_media_slot'),
    )
    op.create_index('ix_media_title_id', 'media', ['title_id'])


def downgrade():
    op.drop_index('ix_media_title_id', table_name='media')
    op.drop_table('media')
