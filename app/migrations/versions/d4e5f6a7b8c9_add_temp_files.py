"""Add temp_files table

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'd4e5f6a7b8c9'
down_revision = 'c3d4e5f6a7b8'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'temp_files',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('filepath', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('filepath'),
    )


def downgrade():
    op.drop_table('temp_files')
