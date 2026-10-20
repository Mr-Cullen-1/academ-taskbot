"""Deny direct Data API access to Taskbot tables.

Revision ID: 0007_restrict_data_api
Revises: 0006_mvp_entities

Taskbot authorizes users in FastAPI and connects to PostgreSQL as its trusted
server role. No anon/authenticated RLS policies are intended for these tables.
Run the upgrade from 0005 through 0007 as one Alembic command so PostgreSQL
commits the entity creation and access hardening together.
"""

from alembic import op


revision = "0007_restrict_data_api"
down_revision = "0006_mvp_entities"
branch_labels = None
depends_on = None


# Frozen migration inventory: do not import live ORM metadata here.
TASKBOT_TABLES = (
    "api_idempotency",
    "audit_logs",
    "comments",
    "deadline_changes",
    "deadline_requests",
    "login_challenges",
    "login_proofs",
    "notification_attempts",
    "notifications",
    "outbox_events",
    "personal_reminders",
    "project_members",
    "project_team_members",
    "project_teams",
    "projects",
    "registration_drafts",
    "sessions",
    "sprints",
    "task_attachments",
    "task_dependencies",
    "task_events",
    "task_key_sequence",
    "task_watchers",
    "tasks",
    "telegram_updates",
    "user_avatars",
    "user_roles",
    "users",
    "weekly_reports",
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in TASKBOT_TABLES:
        op.execute(f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'REVOKE ALL PRIVILEGES ON TABLE public."{table}" FROM anon, authenticated')
    # The migrations run as postgres; this affects only its future public tables.
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public "
               "REVOKE ALL PRIVILEGES ON TABLES FROM anon, authenticated")


def downgrade() -> None:
    raise RuntimeError(
        "Taskbot Data API restrictions are intentionally not reversible: "
        "re-granting anon/authenticated access would expose private data"
    )
