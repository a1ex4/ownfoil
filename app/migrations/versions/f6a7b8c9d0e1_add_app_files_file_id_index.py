"""Add index on app_files.file_id

Revision ID: f6a7b8c9d0e1
Revises: d4e5f6a7b8c9

The composite PK (app_id, file_id) only indexes left-to-right, so back-link
queries filtering by file_id alone (resolvers' _hydrate_file_apps, called by
the GraphQL files endpoint and any nested apps.files.apps selection) had to
full-scan app_files. This index turns that into a probe.
"""
from alembic import op


revision = 'f6a7b8c9d0e1'
down_revision = 'd4e5f6a7b8c9'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index('ix_app_files_file_id', 'app_files', ['file_id'])


def downgrade():
    op.drop_index('ix_app_files_file_id', table_name='app_files')
