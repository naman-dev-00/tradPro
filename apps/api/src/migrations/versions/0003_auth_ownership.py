"""Auth sessions and ownership

Revision ID: 0003_auth_ownership
Revises: 0002_inspection_history
Create Date: 2026-08-30

"""
from typing import Sequence, Union
from datetime import datetime, timezone
from alembic import op
import sqlalchemy as sa
from src.database import UTCDateTime

# revision identifiers, used by Alembic.
revision: str = "0003_auth_ownership"
down_revision: Union[str, None] = "0002_inspection_history"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LEGACY_PRINCIPAL_ID = "00000000-0000-0000-0000-000000000000"

def upgrade() -> None:
    # 1. Create 'users' table
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("username", sa.String(50), nullable=False),
        sa.Column("normalized_username", sa.String(50), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("normalized_email", sa.String(255), nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("role", sa.String(20), nullable=False, server_default="VIEWER"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("role IN ('VIEWER', 'EDITOR', 'ADMIN')", name="ck_users_role"),
        sa.CheckConstraint("length(username) >= 3 AND length(username) <= 50", name="ck_users_username_len"),
    )
    op.create_index("ix_users_normalized_username", "users", ["normalized_username"], unique=True)
    op.create_index("ix_users_normalized_email", "users", ["normalized_email"], unique=True)
    op.create_index("ix_users_is_active", "users", ["is_active"])

    # 2. Create 'user_sessions' table
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_hash", sa.String(64), nullable=False),
        sa.Column("csrf_hash", sa.String(64), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("last_accessed_at", UTCDateTime(), nullable=False),
        sa.Column("idle_expires_at", UTCDateTime(), nullable=False),
        sa.Column("absolute_expires_at", UTCDateTime(), nullable=False),
        sa.Column("is_revoked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("revoked_at", UTCDateTime(), nullable=True),
    )
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])
    op.create_index("ix_user_sessions_session_hash", "user_sessions", ["session_hash"], unique=True)
    op.create_index("ix_user_sessions_idle_expires_at", "user_sessions", ["idle_expires_at"])
    op.create_index("ix_user_sessions_absolute_expires_at", "user_sessions", ["absolute_expires_at"])
    op.create_index("ix_user_sessions_is_revoked", "user_sessions", ["is_revoked"])

    # 3. Add 'owner_id' columns (initially nullable for data backfill)
    with op.batch_alter_table("strategies") as batch_op:
        batch_op.add_column(sa.Column("owner_id", sa.String(36), nullable=True))

    with op.batch_alter_table("inspection_runs") as batch_op:
        batch_op.add_column(sa.Column("owner_id", sa.String(36), nullable=True))

    # 4. Insert inactive, non-login legacy system principal for pre-existing record isolation
    now_utc = datetime.now(timezone.utc)
    users_table = sa.table(
        "users",
        sa.column("id", sa.String),
        sa.column("username", sa.String),
        sa.column("normalized_username", sa.String),
        sa.column("email", sa.String),
        sa.column("normalized_email", sa.String),
        sa.column("hashed_password", sa.String),
        sa.column("role", sa.String),
        sa.column("is_active", sa.Boolean),
        sa.column("created_at", UTCDateTime),
        sa.column("updated_at", UTCDateTime),
    )
    op.bulk_insert(
        users_table,
        [
            {
                "id": LEGACY_PRINCIPAL_ID,
                "username": "system_legacy_owner",
                "normalized_username": "system_legacy_owner",
                "email": "system_legacy@tradepro.internal",
                "normalized_email": "system_legacy@tradepro.internal",
                "hashed_password": "!DISABLED_LEGACY_PRINCIPAL_NO_LOGIN",
                "role": "VIEWER",
                "is_active": False,
                "created_at": now_utc,
                "updated_at": now_utc,
            }
        ]
    )

    # 5. Backfill existing unowned strategies and inspection runs
    op.execute(
        sa.text(f"UPDATE strategies SET owner_id = '{LEGACY_PRINCIPAL_ID}' WHERE owner_id IS NULL")
    )
    op.execute(
        sa.text(f"UPDATE inspection_runs SET owner_id = '{LEGACY_PRINCIPAL_ID}' WHERE owner_id IS NULL")
    )

    # 6. Alter 'owner_id' to NOT NULL and configure foreign keys / unique constraints
    with op.batch_alter_table("strategies") as batch_op:
        batch_op.alter_column("owner_id", nullable=False, existing_type=sa.String(36))
        batch_op.create_foreign_key("fk_strategies_owner_id", "users", ["owner_id"], ["id"])
        batch_op.create_index("ix_strategies_owner_id", ["owner_id"])

    with op.batch_alter_table("inspection_runs") as batch_op:
        batch_op.alter_column("owner_id", nullable=False, existing_type=sa.String(36))
        batch_op.create_foreign_key("fk_inspection_runs_owner_id", "users", ["owner_id"], ["id"])
        batch_op.create_index("ix_inspection_runs_owner_id", ["owner_id"])
        # Replace global completed_fingerprint uniqueness with owner-scoped uniqueness
        batch_op.drop_constraint("uq_inspection_runs_completed_fingerprint", type_="unique")
        batch_op.create_unique_constraint(
            "uq_inspection_runs_owner_completed_fingerprint",
            ["owner_id", "completed_fingerprint"]
        )

def downgrade() -> None:
    # 1. Collision detection safety: Check for cross-owner duplicate non-null completed_fingerprints
    bind = op.get_bind()
    dupes = bind.execute(sa.text("""
        SELECT completed_fingerprint, COUNT(*)
        FROM inspection_runs
        WHERE completed_fingerprint IS NOT NULL
        GROUP BY completed_fingerprint
        HAVING COUNT(*) > 1
    """)).fetchall()

    if dupes:
        raise RuntimeError(
            f"Downgrade aborted: {len(dupes)} cross-owner duplicate completed_fingerprint(s) exist. "
            "Restoring global uniqueness constraint would lose data or fail constraint integrity."
        )

    # 2. Revert inspection_runs constraints and owner_id
    with op.batch_alter_table("inspection_runs") as batch_op:
        batch_op.drop_constraint("uq_inspection_runs_owner_completed_fingerprint", type_="unique")
        batch_op.create_unique_constraint("uq_inspection_runs_completed_fingerprint", ["completed_fingerprint"])
        batch_op.drop_constraint("fk_inspection_runs_owner_id", type_="foreignkey")
        batch_op.drop_index("ix_inspection_runs_owner_id")
        batch_op.drop_column("owner_id")

    # 3. Revert strategies owner_id
    with op.batch_alter_table("strategies") as batch_op:
        batch_op.drop_constraint("fk_strategies_owner_id", type_="foreignkey")
        batch_op.drop_index("ix_strategies_owner_id")
        batch_op.drop_column("owner_id")

    # 4. Drop tables
    op.drop_table("user_sessions")
    op.drop_table("users")
