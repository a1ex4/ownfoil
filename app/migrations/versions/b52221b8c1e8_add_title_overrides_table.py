from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "b52221b8c1e8"
down_revision = "78c33e9bffce"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "app_overrides",
        sa.Column("id", sa.Integer(), primary_key=True),

        # One-to-one to apps.id; cascade on delete
        sa.Column("app_fk", sa.Integer(), sa.ForeignKey("apps.id", ondelete="CASCADE"), nullable=False, unique=True),

        # Overridable metadata
        sa.Column("name", sa.String(length=512), nullable=True),
        sa.Column("release_date", sa.Date(), nullable=True),
        sa.Column("region", sa.String(length=32), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("content_type", sa.String(length=64), nullable=True),
        sa.Column("version", sa.String(length=64), nullable=True),

        # Artwork
        sa.Column("icon_path", sa.String(length=1024), nullable=True),
        sa.Column("banner_path", sa.String(length=1024), nullable=True),

        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    # Helpful index for joins/filters
    op.create_index("ix_app_overrides_app_fk", "app_overrides", ["app_fk"])


def downgrade():
    op.drop_index("ix_app_overrides_app_fk", table_name="app_overrides")
    op.drop_table("app_overrides")
