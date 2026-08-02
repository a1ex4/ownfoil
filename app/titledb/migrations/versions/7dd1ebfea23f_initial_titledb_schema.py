"""Initial titledb schema

Revision ID: 7dd1ebfea23f
Revises:

"""
from alembic import op
import sqlalchemy as sa

revision = '7dd1ebfea23f'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'titles',
        sa.Column('id', sa.Text(), nullable=False),
        sa.Column('source', sa.Text(), nullable=False),
        sa.Column('is_overridden', sa.Integer(), server_default=sa.text('0'), nullable=False),
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
    op.create_index('idx_titles_id', 'titles', ['id'])

    op.create_table(
        'cnmts',
        sa.Column('app_id', sa.Text(), nullable=False),
        sa.Column('cnmt_version', sa.Text(), nullable=False),
        sa.Column('title_id', sa.Text(), nullable=True),
        sa.Column('title_type', sa.Integer(), nullable=True),
        sa.Column('version', sa.Integer(), nullable=True),
        sa.Column('other_application_id', sa.Text(), nullable=True),
        sa.Column('required_application_version', sa.Integer(), nullable=True),
        sa.Column('required_system_version', sa.Integer(), nullable=True),
        sa.Column('content_entries', sa.Text(), nullable=True),
        sa.Column('meta_entries', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('app_id', 'cnmt_version'),
    )
    op.create_index('idx_cnmts_app_id', 'cnmts', ['app_id'])
    op.create_index('idx_cnmts_dlc_lookup', 'cnmts', ['other_application_id', 'title_type'])

    op.create_table(
        'versions',
        sa.Column('title_id', sa.Text(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('release_date', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('title_id', 'version'),
    )

    op.create_table(
        'meta',
        sa.Column('key', sa.Text(), nullable=False),
        sa.Column('value', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('key'),
    )


def downgrade():
    op.drop_table('meta')
    op.drop_table('versions')
    op.drop_index('idx_cnmts_dlc_lookup', table_name='cnmts')
    op.drop_index('idx_cnmts_app_id', table_name='cnmts')
    op.drop_table('cnmts')
    op.drop_index('idx_titles_id', table_name='titles')
    op.drop_table('titles')
