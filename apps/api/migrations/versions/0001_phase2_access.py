"""Phase 2 users, sessions, registration, roles, projects and audit.

Revision ID: 0001_phase2_access
Revises:
"""

from alembic import op
import sqlalchemy as sa


revision = "0001_phase2_access"
down_revision = None
branch_labels = None
depends_on = None


def uid(name, *args, **kwargs):
    return sa.Column(name, sa.Uuid(), *args, **kwargs)


def timestamp(name, **kwargs):
    return sa.Column(name, sa.DateTime(timezone=True), **kwargs)


def upgrade() -> None:
    op.create_table(
        "users", uid("id", primary_key=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("phone", sa.String(32), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        timestamp("created_at", nullable=False), timestamp("updated_at", nullable=False),
        sa.CheckConstraint("status IN ('PENDING','ACTIVE','REJECTED','BLOCKED')", name="ck_user_status"),
    )
    op.create_table(
        "user_roles", uid("id", primary_key=True),
        uid("user_id", sa.ForeignKey("users.id"), nullable=False),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("super_admin_slot", sa.Integer(), unique=True),
        uid("granted_by", sa.ForeignKey("users.id")), timestamp("granted_at", nullable=False),
        sa.UniqueConstraint("user_id", "code", name="uq_user_role"),
        sa.CheckConstraint("code IN ('SUPER_ADMIN','PM','TEAM_LEAD','SUBJECT_DEVELOPER','QA')", name="ck_role_code"),
        sa.CheckConstraint("(code = 'SUPER_ADMIN' AND super_admin_slot = 1) OR (code <> 'SUPER_ADMIN' AND super_admin_slot IS NULL)", name="ck_super_admin_slot"),
    )
    op.create_index("ix_user_roles_user_id", "user_roles", ["user_id"])
    op.create_table(
        "registration_drafts", sa.Column("telegram_user_id", sa.BigInteger(), primary_key=True),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("phone", sa.String(32)), sa.Column("stage", sa.String(16), nullable=False),
        timestamp("updated_at", nullable=False),
    )
    op.create_table(
        "telegram_updates", sa.Column("update_id", sa.BigInteger(), primary_key=True),
        timestamp("processed_at", nullable=False), sa.Column("response_code", sa.String(40), nullable=False),
    )
    op.create_table(
        "sessions", uid("id", primary_key=True), uid("user_id", sa.ForeignKey("users.id"), nullable=False),
        sa.Column("credential_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("csrf_digest", sa.String(64), nullable=False),
        timestamp("issued_at", nullable=False), timestamp("expires_at", nullable=False),
        timestamp("revoked_at"),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_table("login_proofs", sa.Column("digest", sa.String(64), primary_key=True), timestamp("used_at", nullable=False))
    op.create_table("login_challenges", sa.Column("digest", sa.String(64), primary_key=True), timestamp("expires_at", nullable=False))
    op.create_table(
        "projects", uid("id", primary_key=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        uid("owner_user_id", sa.ForeignKey("users.id"), nullable=False),
        timestamp("created_at", nullable=False), sa.Column("version", sa.Integer(), nullable=False),
    )
    op.create_table(
        "project_members", uid("project_id", sa.ForeignKey("projects.id"), primary_key=True),
        uid("user_id", sa.ForeignKey("users.id"), primary_key=True), timestamp("joined_at", nullable=False),
    )
    op.create_table(
        "audit_logs", uid("id", primary_key=True),
        uid("actor_user_id", sa.ForeignKey("users.id")),
        sa.Column("action", sa.String(60), nullable=False),
        sa.Column("target_type", sa.String(40), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False), timestamp("created_at", nullable=False),
    )
    op.create_table(
        "api_idempotency", uid("id", primary_key=True),
        uid("actor_user_id", sa.ForeignKey("users.id"), nullable=False),
        sa.Column("key", sa.String(120), nullable=False),
        sa.Column("operation", sa.String(120), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("response_body", sa.JSON(), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.UniqueConstraint("actor_user_id", "key", name="uq_actor_idempotency_key"),
    )


def downgrade() -> None:
    for table in ("api_idempotency", "audit_logs", "project_members", "projects",
                  "login_challenges", "login_proofs", "sessions", "telegram_updates",
                  "registration_drafts", "user_roles", "users"):
        op.drop_table(table)
