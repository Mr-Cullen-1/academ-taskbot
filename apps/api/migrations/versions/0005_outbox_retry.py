"""Track bounded outbox retries without changing existing events.

Revision ID: 0005_outbox_retry
Revises: 0004_profile_avatar
"""

from alembic import op
import sqlalchemy as sa


revision = "0005_outbox_retry"
down_revision = "0004_profile_avatar"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("outbox_events", sa.Column("attempts", sa.Integer(),
                                              nullable=False, server_default="0"))
    op.add_column("outbox_events", sa.Column("last_error_code", sa.String(80)))


def downgrade() -> None:
    op.drop_column("outbox_events", "last_error_code")
    op.drop_column("outbox_events", "attempts")
