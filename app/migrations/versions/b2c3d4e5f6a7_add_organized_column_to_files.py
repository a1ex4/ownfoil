"""Add organized column to files

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
    op.add_column('files', sa.Column('organized', sa.Boolean(), server_default='0', nullable=False))


def downgrade():
    op.drop_column('files', 'organized')
