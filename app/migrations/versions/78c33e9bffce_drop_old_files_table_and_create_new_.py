"""Drop old Files table and create new schema

Revision ID: 78c33e9bffce
Revises: 

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

# revision identifiers, used by Alembic.
revision = '78c33e9bffce'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # Drop the old Files table
    op.drop_table('files')
    
    # Create Libraries table
    op.create_table('libraries',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('path', sa.String(), nullable=False),
        sa.Column('last_scan', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('path')
    )
    
    # Create Titles table
    op.create_table('titles',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('title_id', sa.String(), nullable=True),
        sa.Column('have_base', sa.Boolean(), nullable=True),
        sa.Column('up_to_date', sa.Boolean(), nullable=True),
        sa.Column('complete', sa.Boolean(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('title_id')
    )
    
    # Create new Files table with updated schema
    op.create_table('files',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('library_id', sa.Integer(), nullable=False),
        sa.Column('filepath', sa.String(), nullable=False),
        sa.Column('folder', sa.String(), nullable=True),
        sa.Column('filename', sa.String(), nullable=False),
        sa.Column('extension', sa.String(), nullable=True),
        sa.Column('size', sa.Integer(), nullable=True),
        sa.Column('compressed', sa.Boolean(), nullable=True),
        sa.Column('multicontent', sa.Boolean(), nullable=True),
        sa.Column('nb_content', sa.Integer(), nullable=True),
        sa.Column('download_count', sa.Integer(), nullable=True),
        sa.Column('identified', sa.Boolean(), nullable=True),
        sa.Column('identification_type', sa.String(), nullable=True),
        sa.Column('identification_error', sa.String(), nullable=True),
        sa.Column('identification_attempts', sa.Integer(), nullable=True),
        sa.Column('last_attempt', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['library_id'], ['libraries.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('filepath')
    )
    
    # Create Apps table
    op.create_table('apps',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('title_id', sa.Integer(), nullable=False),
        sa.Column('app_id', sa.String(), nullable=True),
        sa.Column('app_version', sa.String(), nullable=True),
        sa.Column('app_type', sa.String(), nullable=True),
        sa.Column('owned', sa.Boolean(), nullable=True),
        sa.ForeignKeyConstraint(['title_id'], ['titles.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('app_id', 'app_version', name='uq_apps_app_version')
    )
    
    # Create app_files association table (many-to-many relationship)
    op.create_table('app_files',
        sa.Column('app_id', sa.Integer(), nullable=False),
        sa.Column('file_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['app_id'], ['apps.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['file_id'], ['files.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('app_id', 'file_id')
    )
    
    # Set default values for boolean columns
    with op.batch_alter_table('files') as batch_op:
        batch_op.alter_column('compressed', server_default='0')
        batch_op.alter_column('multicontent', server_default='0')
        batch_op.alter_column('nb_content', server_default='0')
        batch_op.alter_column('download_count', server_default='0')
        batch_op.alter_column('identified', server_default='0')
        batch_op.alter_column('identification_attempts', server_default='0')
    
    with op.batch_alter_table('titles') as batch_op:
        batch_op.alter_column('have_base', server_default='0')
        batch_op.alter_column('up_to_date', server_default='0')
        batch_op.alter_column('complete', server_default='0')
    
    with op.batch_alter_table('apps') as batch_op:
        batch_op.alter_column('owned', server_default='0')


def downgrade():
    raise NotImplementedError("Downgrade is intentionally not supported for this migration.")
