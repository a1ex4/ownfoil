"""Drop front_box_art from title_overrides

Revision ID: a9b0c1d2e3f4
Revises: f8a9b0c1d2e3

Box art is no longer part of the title metadata: no source ever filled it.
"""
from alembic import op
import sqlalchemy as sa


revision = 'a9b0c1d2e3f4'
down_revision = 'f8a9b0c1d2e3'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('title_overrides') as batch_op:
        batch_op.drop_column('front_box_art')


def downgrade():
    with op.batch_alter_table('title_overrides') as batch_op:
        batch_op.add_column(sa.Column('front_box_art', sa.Text(), nullable=True))
