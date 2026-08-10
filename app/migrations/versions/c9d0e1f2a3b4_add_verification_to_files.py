"""Add verification columns to files

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3

Nothing recorded whether a file was intact. A truncated download, a bit-rotted disk and a
repacked NSP all looked like a healthy file until a Switch refused to install it. The two
verdicts are nullable rather than defaulted because "never attempted" is a third state,
distinct from "checked and bad" — every existing row starts there.
nstools decides CORRECT / MODIFIED / CORRUPT per content and then reports the last two as
one failed verdict, so `hash_valid = 0` covers both a repack whose contents are intact and
a genuinely damaged file. `hash_modified` keeps the distinction.
"""
from alembic import op
import sqlalchemy as sa


revision = 'c9d0e1f2a3b4'
down_revision = 'b8c9d0e1f2a3'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('files', sa.Column('signature_valid', sa.Boolean(), nullable=True))
    op.add_column('files', sa.Column('hash_valid', sa.Boolean(), nullable=True))
    op.add_column('files', sa.Column('hash_modified', sa.Boolean(), nullable=True))
    op.add_column('files', sa.Column('verification_error', sa.String(), nullable=True))
    op.add_column('files', sa.Column('verified_at', sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column('files', 'verified_at')
    op.drop_column('files', 'verification_error')
    op.drop_column('files', 'hash_modified')
    op.drop_column('files', 'hash_valid')
    op.drop_column('files', 'signature_valid')
