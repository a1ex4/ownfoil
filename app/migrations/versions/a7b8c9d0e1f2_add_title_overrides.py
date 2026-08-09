"""Add title_overrides

Revision ID: a7b8c9d0e1f2
Revises: e5f6a7b8c9d0

Durable home of the title metadata that isn't downloaded: user-authored entries and, later,
metadata extracted from the files themselves. titles.db is derived and gets recreated
whenever its schema changes, so it cannot hold anything the app can't rebuild.

Rows are sparse: each source only sets the fields it knows about, and titledb.store merges
them field by field. The columns mirror app/titledb/schema.py, spelled out here so the
migration stays a fixed snapshot.
"""
from alembic import op
import sqlalchemy as sa


revision = 'a7b8c9d0e1f2'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'title_overrides',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('name', sa.Text(), nullable=True),
        sa.Column('banner_url', sa.Text(), nullable=True),
        sa.Column('icon_url', sa.Text(), nullable=True),
        sa.Column('front_box_art', sa.Text(), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('intro', sa.Text(), nullable=True),
        sa.Column('developer', sa.Text(), nullable=True),
        sa.Column('publisher', sa.Text(), nullable=True),
        sa.Column('release_date', sa.Text(), nullable=True),
        sa.Column('category', sa.Text(), nullable=True),
        sa.Column('is_demo', sa.Text(), nullable=True),
        sa.Column('nsu_id', sa.Text(), nullable=True),
        sa.Column('number_of_players', sa.Text(), nullable=True),
        sa.Column('parent_id', sa.Text(), nullable=True),
        sa.Column('rank', sa.Text(), nullable=True),
        sa.Column('rating', sa.Text(), nullable=True),
        sa.Column('rating_content', sa.Text(), nullable=True),
        sa.Column('region', sa.Text(), nullable=True),
        sa.Column('regions', sa.Text(), nullable=True),
        sa.Column('languages', sa.Text(), nullable=True),
        sa.Column('language', sa.Text(), nullable=True),
        sa.Column('rights_id', sa.Text(), nullable=True),
        sa.Column('screenshots', sa.Text(), nullable=True),
        sa.Column('size', sa.Text(), nullable=True),
        sa.Column('version', sa.Text(), nullable=True),
        sa.Column('nca_key', sa.Text(), nullable=True),
        sa.Column('ids', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id', 'source'),
    )


def downgrade():
    op.drop_table('title_overrides')
