"""Phase 4 delivery records and weekly report snapshots.

Revision ID: 0003_phase4_notifications
Revises: 0002_phase3_tasks
"""

from alembic import op
import sqlalchemy as sa


revision = "0003_phase4_notifications"
down_revision = "0002_phase3_tasks"
branch_labels = None
depends_on = None


def uid(name, *args, **kwargs):
    return sa.Column(name, sa.Uuid(), *args, **kwargs)


def stamp(name, **kwargs):
    return sa.Column(name, sa.DateTime(timezone=True), **kwargs)


def upgrade() -> None:
    op.create_table(
        "notifications",
        uid("id", primary_key=True),
        uid("recipient_user_id", sa.ForeignKey("users.id"), nullable=False),
        uid("subject_user_id", sa.ForeignKey("users.id")),
        uid("task_id", sa.ForeignKey("tasks.id")),
        uid("source_event_id", sa.ForeignKey("task_events.id")),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("dedupe_key", sa.String(180), nullable=False, unique=True),
        sa.Column("body", sa.Text(), nullable=False),
        stamp("scheduled_at", nullable=False),
        sa.Column("due_date_snapshot", sa.Date()),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        stamp("lease_until"),
        sa.Column("last_error_code", sa.String(80)),
        stamp("created_at", nullable=False),
        stamp("sent_at"),
        sa.CheckConstraint("state IN ('PENDING','SENDING','RETRY','SENT','SIMULATED','SKIPPED','FAILED')", name="ck_notification_state"),
    )
    op.create_index("ix_notifications_recipient_user_id", "notifications", ["recipient_user_id"])
    op.create_index("ix_notifications_task_id", "notifications", ["task_id"])
    op.create_index("ix_notifications_state_scheduled", "notifications", ["state", "scheduled_at"])
    op.create_table(
        "notification_attempts",
        uid("id", primary_key=True),
        uid("notification_id", sa.ForeignKey("notifications.id"), nullable=False),
        stamp("attempted_at", nullable=False),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(80)),
    )
    op.create_index("ix_notification_attempts_notification_id", "notification_attempts", ["notification_id"])
    op.create_table(
        "weekly_reports",
        uid("id", primary_key=True),
        uid("recipient_user_id", sa.ForeignKey("users.id"), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        stamp("generated_at", nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("recipient_user_id", "week_start", name="uq_weekly_report_recipient_week"),
    )
    op.create_index("ix_weekly_reports_recipient_user_id", "weekly_reports", ["recipient_user_id"])


def downgrade() -> None:
    op.drop_table("weekly_reports")
    op.drop_table("notification_attempts")
    op.drop_table("notifications")
