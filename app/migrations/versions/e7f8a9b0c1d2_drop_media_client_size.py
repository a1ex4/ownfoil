"""Drop media client size

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1

A rendition's size follows from the original's and the box it is fitted to (`media.fit`),
so a row holding one of them recorded nothing the original's size doesn't already say -
and would go stale the moment a box changed.
"""
from alembic import op
import sqlalchemy as sa


revision = 'e7f8a9b0c1d2'
down_revision = 'd6e7f8a9b0c1'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('media') as batch_op:
        batch_op.drop_column('client_width')
        batch_op.drop_column('client_height')


def downgrade():
    with op.batch_alter_table('media') as batch_op:
        batch_op.add_column(sa.Column('client_width', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('client_height', sa.Integer(), nullable=True))
