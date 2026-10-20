"""MVP planning, deadline and personal notification entities.

Revision ID: 0006_mvp_entities
Revises: 0005_outbox_retry
"""

from alembic import op
import sqlalchemy as sa


revision = "0006_mvp_entities"
down_revision = "0005_outbox_retry"
branch_labels = None
depends_on = None


def uid(name, *args, **kwargs):
    return sa.Column(name, sa.Uuid(), *args, **kwargs)


def stamp(name, **kwargs):
    return sa.Column(name, sa.DateTime(timezone=True), **kwargs)


def upgrade() -> None:
    op.create_table("project_teams",
                    uid("id", primary_key=True),
                    uid("project_id", sa.ForeignKey("projects.id"), nullable=False),
                    sa.Column("name", sa.String(160), nullable=False),
                    stamp("created_at", nullable=False),
                    sa.Column("version", sa.Integer(), nullable=False),
                    sa.UniqueConstraint("project_id", "name", name="uq_project_team_name"))
    op.create_index("ix_project_teams_project_id", "project_teams", ["project_id"])
    op.create_table("project_team_members",
                    uid("team_id", sa.ForeignKey("project_teams.id"), primary_key=True),
                    uid("user_id", sa.ForeignKey("users.id"), primary_key=True),
                    stamp("joined_at", nullable=False))
    op.create_table("sprints",
                    uid("id", primary_key=True),
                    uid("project_id", sa.ForeignKey("projects.id"), nullable=False),
                    sa.Column("name", sa.String(160), nullable=False),
                    sa.Column("starts_on", sa.Date(), nullable=False),
                    sa.Column("ends_on", sa.Date(), nullable=False),
                    stamp("created_at", nullable=False),
                    sa.Column("version", sa.Integer(), nullable=False),
                    sa.CheckConstraint("ends_on >= starts_on", name="ck_sprint_range"))
    op.create_index("ix_sprints_project_id", "sprints", ["project_id"])
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(uid("sprint_id"))
        batch.create_foreign_key("fk_tasks_sprint_id", "sprints", ["sprint_id"], ["id"])
    op.create_index("ix_tasks_sprint_id", "tasks", ["sprint_id"])
    op.create_table("task_watchers",
                    uid("task_id", sa.ForeignKey("tasks.id"), primary_key=True),
                    uid("user_id", sa.ForeignKey("users.id"), primary_key=True),
                    stamp("created_at", nullable=False))
    op.create_table("task_attachments",
                    uid("id", primary_key=True),
                    uid("task_id", sa.ForeignKey("tasks.id"), nullable=False),
                    uid("uploader_user_id", sa.ForeignKey("users.id"), nullable=False),
                    sa.Column("object_key", sa.String(200), nullable=False, unique=True),
                    sa.Column("filename", sa.String(200), nullable=False),
                    sa.Column("content_type", sa.String(80), nullable=False),
                    sa.Column("size_bytes", sa.Integer(), nullable=False),
                    sa.Column("sha256", sa.String(64), nullable=False),
                    stamp("created_at", nullable=False), stamp("deleted_at"))
    op.create_index("ix_task_attachments_task_id", "task_attachments", ["task_id"])
    op.create_table("task_dependencies",
                    uid("task_id", sa.ForeignKey("tasks.id"), primary_key=True),
                    uid("depends_on_task_id", sa.ForeignKey("tasks.id"), primary_key=True),
                    stamp("created_at", nullable=False),
                    sa.CheckConstraint("task_id <> depends_on_task_id", name="ck_dependency_not_self"))
    op.create_table("deadline_requests",
                    uid("id", primary_key=True),
                    uid("task_id", sa.ForeignKey("tasks.id"), nullable=False),
                    uid("requested_by", sa.ForeignKey("users.id"), nullable=False),
                    sa.Column("proposed_due_date", sa.Date(), nullable=False),
                    sa.Column("reason", sa.Text(), nullable=False),
                    sa.Column("progress_note", sa.Text(), nullable=False),
                    sa.Column("status", sa.String(16), nullable=False),
                    uid("decided_by", sa.ForeignKey("users.id")),
                    stamp("decided_at"), sa.Column("decision_reason", sa.Text()),
                    stamp("created_at", nullable=False),
                    sa.Column("version", sa.Integer(), nullable=False),
                    sa.CheckConstraint("status IN ('PENDING','APPROVED','REJECTED')",
                                       name="ck_deadline_request_status"))
    op.create_index("ix_deadline_requests_task_id", "deadline_requests", ["task_id"])
    op.create_index("uq_deadline_request_pending", "deadline_requests", ["task_id"],
                    unique=True, postgresql_where=sa.text("status = 'PENDING'"),
                    sqlite_where=sa.text("status = 'PENDING'"))
    op.create_table("deadline_changes",
                    uid("id", primary_key=True),
                    uid("request_id", sa.ForeignKey("deadline_requests.id"), nullable=False,
                        unique=True),
                    uid("task_id", sa.ForeignKey("tasks.id"), nullable=False),
                    sa.Column("previous_due_date", sa.Date(), nullable=False),
                    sa.Column("new_due_date", sa.Date(), nullable=False),
                    uid("approved_by", sa.ForeignKey("users.id"), nullable=False),
                    stamp("approved_at", nullable=False))
    op.create_index("ix_deadline_changes_task_id", "deadline_changes", ["task_id"])
    op.create_table("personal_reminders",
                    uid("id", primary_key=True),
                    uid("owner_user_id", sa.ForeignKey("users.id"), nullable=False),
                    uid("task_id", sa.ForeignKey("tasks.id")),
                    stamp("remind_at", nullable=False),
                    sa.Column("text", sa.Text(), nullable=False),
                    sa.Column("version", sa.Integer(), nullable=False),
                    stamp("cancelled_at"), stamp("created_at", nullable=False))
    op.create_index("ix_personal_reminders_owner_user_id", "personal_reminders", ["owner_user_id"])
    op.add_column("notifications", stamp("read_at"))


def downgrade() -> None:
    op.drop_column("notifications", "read_at")
    op.drop_index("ix_personal_reminders_owner_user_id", table_name="personal_reminders")
    op.drop_table("personal_reminders")
    op.drop_index("ix_deadline_changes_task_id", table_name="deadline_changes")
    op.drop_table("deadline_changes")
    op.drop_index("uq_deadline_request_pending", table_name="deadline_requests")
    op.drop_index("ix_deadline_requests_task_id", table_name="deadline_requests")
    op.drop_table("deadline_requests")
    op.drop_table("task_dependencies")
    op.drop_index("ix_task_attachments_task_id", table_name="task_attachments")
    op.drop_table("task_attachments")
    op.drop_table("task_watchers")
    op.drop_index("ix_tasks_sprint_id", table_name="tasks")
    with op.batch_alter_table("tasks") as batch:
        batch.drop_constraint("fk_tasks_sprint_id", type_="foreignkey")
        batch.drop_column("sprint_id")
    op.drop_index("ix_sprints_project_id", table_name="sprints")
    op.drop_table("sprints")
    op.drop_table("project_team_members")
    op.drop_index("ix_project_teams_project_id", table_name="project_teams")
    op.drop_table("project_teams")
