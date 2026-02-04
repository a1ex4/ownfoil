"""Add user freeze fields

Revision ID: e3a1b7a2f1c0
Revises: 2c9d2a6e3b41

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e3a1b7a2f1c0'
down_revision = '2c9d2a6e3b41'
branch_labels = None
depends_on = None


def upgrade():
    # SQLite requires batch ops for ALTER TABLE.
    with op.batch_alter_table('user') as batch_op:
        batch_op.add_column(sa.Column('frozen', sa.Boolean(), nullable=True, server_default=sa.text('0')))
        batch_op.add_column(sa.Column('frozen_message', sa.String(), nullable=True))


def downgrade():
    with op.batch_alter_table('user') as batch_op:
        batch_op.drop_column('frozen_message')
        batch_op.drop_column('frozen')
