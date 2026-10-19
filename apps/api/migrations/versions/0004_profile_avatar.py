"""Private user avatars; the API serves images only to their owner.

Revision ID: 0004_profile_avatar
Revises: 0003_phase4_notifications
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_profile_avatar"
down_revision = "0003_phase4_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_avatars",
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("image_data", sa.LargeBinary(), nullable=False),
        sa.Column("media_type", sa.String(32), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    # Supabase's public schema can be exposed through its Data API. With no
    # policies, Data API roles cannot read private image bytes.
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE user_avatars ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_table("user_avatars")
