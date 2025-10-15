from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "b52221b8c1e8"
down_revision = '78c33e9bffce'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        'app_overrides',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('title_id', sa.String(), nullable=True),
        sa.Column('file_basename', sa.String(), nullable=True),
        sa.Column('app_id', sa.String(), nullable=True),
        sa.Column('app_version', sa.String(), nullable=True),
        sa.Column('name', sa.String(length=512), nullable=True),
        sa.Column('release_date', sa.Date(), nullable=True),
        sa.Column('region', sa.String(length=32), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('content_type', sa.String(length=64), nullable=True),
        sa.Column('version', sa.String(length=64), nullable=True),
        sa.Column('icon_path', sa.String(length=1024), nullable=True),
        sa.Column('banner_path', sa.String(length=1024), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_app_overrides_title_id', 'app_overrides', ['title_id'])
    op.create_index('ix_app_overrides_file_basename', 'app_overrides', ['file_basename'])
    op.create_index('ix_app_overrides_app_id', 'app_overrides', ['app_id'])
    op.create_index('ix_app_overrides_app_version', 'app_overrides', ['app_version'])
    op.create_unique_constraint(
        'uq_user_overrides_target',
        'app_overrides',
        ['title_id', 'file_basename', 'app_id', 'app_version']
    )

def downgrade():
    op.drop_constraint('uq_user_overrides_target', 'app_overrides', type_='unique')
    op.drop_index('ix_app_overrides_app_version', table_name='app_overrides')
    op.drop_index('ix_app_overrides_app_id', table_name='app_overrides')
    op.drop_index('ix_app_overrides_file_basename', table_name='app_overrides')
    op.drop_index('ix_app_overrides_title_id', table_name='app_overrides')
    op.drop_table('app_overrides')
