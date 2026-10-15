"""Phase 3 tasks, comments and immutable events.

Revision ID: 0002_phase3_tasks
Revises: 0001_phase2_access
"""

from alembic import op
import sqlalchemy as sa


revision = "0002_phase3_tasks"
down_revision = "0001_phase2_access"
branch_labels = None
depends_on = None


def uid(name, *args, **kwargs):
    return sa.Column(name, sa.Uuid(), *args, **kwargs)


def timestamp(name, **kwargs):
    return sa.Column(name, sa.DateTime(timezone=True), **kwargs)


def upgrade() -> None:
    op.create_table("task_key_sequence",
                    sa.Column("id", sa.Integer(), primary_key=True),
                    sa.Column("next_number", sa.Integer(), nullable=False))
    op.bulk_insert(sa.table("task_key_sequence", sa.column("id", sa.Integer()),
                            sa.column("next_number", sa.Integer())),
                   [{"id": 1, "next_number": 1001}])
    op.create_table(
        "tasks", uid("id", primary_key=True),
        sa.Column("task_key", sa.String(32), nullable=False, unique=True),
        uid("project_id", sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        uid("created_by", sa.ForeignKey("users.id"), nullable=False),
        uid("assignee_id", sa.ForeignKey("users.id")),
        uid("qa_reviewer_id", sa.ForeignKey("users.id")),
        sa.Column("qa_review_status", sa.String(12)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("priority", sa.String(12), nullable=False),
        sa.Column("type", sa.String(24), nullable=False),
        sa.Column("original_due_date", sa.Date(), nullable=False),
        sa.Column("current_due_date", sa.Date(), nullable=False),
        timestamp("created_at", nullable=False), timestamp("updated_at", nullable=False),
        timestamp("submitted_at"), timestamp("completed_at"),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint("status IN ('OPEN','ACCEPTED','REJECTED','IN_PROGRESS','BLOCKED','REVIEW','REWORK','DONE','CANCELLED')", name="ck_task_status"),
        sa.CheckConstraint("qa_review_status IS NULL OR qa_review_status IN ('PENDING','REWORK','DONE')", name="ck_task_qa_status"),
        sa.CheckConstraint("priority IN ('URGENT','HIGH','MEDIUM','LOW')", name="ck_task_priority"),
        sa.CheckConstraint("type IN ('DEVELOPMENT','BUG_FIX','IMPROVEMENT','QA_CHECK','RESEARCH')", name="ck_task_type"),
    )
    for field in ("project_id", "created_by", "assignee_id", "qa_reviewer_id"):
        op.create_index(f"ix_tasks_{field}", "tasks", [field])
    op.create_index("ix_tasks_project_status_due", "tasks", ["project_id", "status", "current_due_date"])
    op.create_table(
        "task_events", uid("id", primary_key=True),
        uid("task_id", sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        uid("actor_user_id", sa.ForeignKey("users.id"), nullable=False),
        sa.Column("type", sa.String(40), nullable=False),
        sa.Column("before_data", sa.JSON(), nullable=False),
        sa.Column("after_data", sa.JSON(), nullable=False),
        sa.Column("approval_mode", sa.String(20)),
        uid("original_author_user_id", sa.ForeignKey("users.id")),
        sa.Column("author_unavailable_confirmed", sa.Boolean()),
        sa.Column("intervention_reason", sa.Text()),
        sa.Column("request_id", sa.String(120)),
        timestamp("occurred_at", nullable=False),
        sa.UniqueConstraint("task_id", "sequence", name="uq_task_event_sequence"),
    )
    op.create_index("ix_task_events_task_id", "task_events", ["task_id"])
    op.create_table(
        "comments", uid("id", primary_key=True),
        uid("task_id", sa.ForeignKey("tasks.id"), nullable=False),
        uid("author_user_id", sa.ForeignKey("users.id"), nullable=False),
        sa.Column("body", sa.Text(), nullable=False), timestamp("created_at", nullable=False),
    )
    op.create_index("ix_comments_task_id", "comments", ["task_id"])
    op.create_table(
        "outbox_events", uid("id", primary_key=True),
        sa.Column("aggregate_type", sa.String(40), nullable=False),
        sa.Column("aggregate_id", sa.String(64), nullable=False),
        uid("source_event_id", sa.ForeignKey("task_events.id"), nullable=False),
        sa.Column("kind", sa.String(60), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("dedupe_key", sa.String(120), nullable=False, unique=True),
        sa.Column("state", sa.String(20), nullable=False),
        timestamp("available_at", nullable=False), timestamp("created_at", nullable=False),
        timestamp("processed_at"),
    )
    op.create_index("ix_outbox_events_state_available_at", "outbox_events", ["state", "available_at"])


def downgrade() -> None:
    op.drop_table("outbox_events")
    op.drop_table("comments")
    op.drop_table("task_events")
    op.drop_table("tasks")
    op.drop_table("task_key_sequence")
