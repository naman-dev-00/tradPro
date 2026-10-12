"""Broker readiness, OAuth 2.0 connection, encrypted token storage, and read-only broker snapshots.

Revision ID: 0009_broker_readiness
Revises: 0008_provider_execution
Create Date: 2026-10-07

Rationale:
Phase 7A introduces secure broker connectivity via Upstox OAuth 2.0 authorization-code flow,
server-side token encryption at rest, atomic OAuth state validation for replay protection,
and durable read-only broker snapshots (funds, margins, holdings, positions, open orders)
isolated completely from internal paper accounting.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from src.database import UTCDateTime

# revision identifiers, used by Alembic.
revision: str = "0009_broker_readiness"
down_revision: Union[str, None] = "0008_provider_execution"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 0. Add composite unique constraint on user_sessions for owner-consistent session FK
    with op.batch_alter_table("user_sessions") as batch_op:
        batch_op.create_unique_constraint("uq_user_sessions_id_user", ["id", "user_id"])

    # 1. Update provider_connections table to support LIVE environment, token metadata, and concurrency versioning
    with op.batch_alter_table("provider_connections") as batch_op:
        batch_op.add_column(sa.Column("broker_user_id", sa.String(100), nullable=True))
        batch_op.add_column(sa.Column("account_name", sa.String(255), nullable=True))
        batch_op.add_column(sa.Column("broker_user_type", sa.String(50), nullable=True))
        batch_op.add_column(sa.Column("encrypted_access_token", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("token_acquired_at", UTCDateTime(), nullable=True))
        batch_op.add_column(sa.Column("token_expires_at", UTCDateTime(), nullable=True))
        batch_op.add_column(sa.Column("token_scope", sa.String(255), nullable=True))
        batch_op.add_column(sa.Column("last_successful_refresh_at", UTCDateTime(), nullable=True))
        batch_op.add_column(sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
        batch_op.add_column(sa.Column("credential_generation", sa.Integer(), nullable=False, server_default="1"))
        batch_op.add_column(sa.Column("authorization_fence", sa.String(64), nullable=True))

        batch_op.drop_constraint("ck_provider_connections_env_sandbox", type_="check")
        batch_op.drop_constraint("ck_provider_connections_status", type_="check")

        batch_op.create_check_constraint(
            "ck_provider_connections_env",
            "environment IN ('SANDBOX', 'LIVE')",
        )
        batch_op.create_check_constraint(
            "ck_provider_connections_status",
            "status IN ('CONFIGURED', 'DISABLED', 'ERROR', 'CONNECTED', 'DISCONNECTED', 'EXPIRED', 'NOT_CONFIGURED')",
        )
        batch_op.create_unique_constraint(
            "uq_provider_connections_id_owner",
            ["id", "owner_id"],
        )

    # Database-backed integrity: prevent concurrent cross-owner claims of the same broker UCC in CONNECTED status
    op.create_index(
        "uq_provider_connections_active_ucc",
        "provider_connections",
        ["environment", "broker_user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'CONNECTED' AND broker_user_id IS NOT NULL"),
        sqlite_where=sa.text("status = 'CONNECTED' AND broker_user_id IS NOT NULL"),
    )

    # 2. Create oauth_states table for single-use, owner/session-bound OAuth state replay protection
    op.create_table(
        "oauth_states",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("state_token", sa.String(64), nullable=False),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("environment", sa.String(50), nullable=False, server_default="LIVE"),
        sa.Column("redirect_uri", sa.String(512), nullable=False),
        sa.Column("client_ip", sa.String(64), nullable=True),
        sa.Column("authorization_fence", sa.String(64), nullable=True),
        sa.Column("credential_generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("consumed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("consumed_at", UTCDateTime(), nullable=True),
        sa.Column("expires_at", UTCDateTime(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("environment IN ('SANDBOX', 'LIVE')", name="ck_oauth_states_env"),
        sa.UniqueConstraint("state_token", name="uq_oauth_states_state_token"),
        sa.ForeignKeyConstraint(
            ["session_id", "owner_id"],
            ["user_sessions.id", "user_sessions.user_id"],
            name="fk_oauth_states_session_owner",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_oauth_states_owner_id", "oauth_states", ["owner_id"])
    op.create_index("ix_oauth_states_session_id", "oauth_states", ["session_id"])
    op.create_index("ix_oauth_states_expires_at", "oauth_states", ["expires_at"])

    # 3. Create broker_snapshots table for read-only snapshots isolated from internal paper accounting
    op.create_table(
        "broker_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), nullable=False),
        sa.Column("connection_id", sa.String(36), nullable=False),
        sa.Column("broker_user_id", sa.String(100), nullable=False),
        sa.Column("environment", sa.String(50), nullable=False),
        sa.Column("status", sa.String(50), nullable=False, server_default="ACTIVE"),
        sa.Column("acquired_at_utc", UTCDateTime(), nullable=False),
        sa.Column("funds_and_margins_json", sa.JSON(), nullable=True),
        sa.Column("holdings_json", sa.JSON(), nullable=True),
        sa.Column("positions_json", sa.JSON(), nullable=True),
        sa.Column("orders_json", sa.JSON(), nullable=True),
        sa.Column("raw_fingerprint", sa.String(64), nullable=False),
        sa.Column("connection_version_at_fetch", sa.Integer(), nullable=False),
        sa.Column("credential_generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("environment IN ('SANDBOX', 'LIVE')", name="ck_broker_snapshots_env"),
        sa.CheckConstraint("status IN ('ACTIVE', 'SUPERSEDED', 'ERROR')", name="ck_broker_snapshots_status"),
        sa.ForeignKeyConstraint(
            ["connection_id", "owner_id"],
            ["provider_connections.id", "provider_connections.owner_id"],
            name="fk_broker_snapshots_conn_owner",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_broker_snapshots_owner_id", "broker_snapshots", ["owner_id"])
    op.create_index("ix_broker_snapshots_connection_id", "broker_snapshots", ["connection_id"])
    op.create_index("ix_broker_snapshots_acquired_at_utc", "broker_snapshots", ["acquired_at_utc"])


def downgrade() -> None:
    conn = op.get_bind()

    # Preflight 1: Refuse downgrade if broker snapshots exist
    has_snapshots = conn.execute(sa.text("SELECT 1 FROM broker_snapshots LIMIT 1")).fetchone()
    if has_snapshots:
        raise RuntimeError("Cannot downgrade: broker_snapshots contains records.")

    # Preflight 2: Refuse downgrade if oauth states exist (including pending or consumed states)
    has_oauth = conn.execute(sa.text("SELECT 1 FROM oauth_states LIMIT 1")).fetchone()
    if has_oauth:
        raise RuntimeError("Cannot downgrade: oauth_states contains records.")

    # Preflight 3: Refuse downgrade if any provider_connections has LIVE environment, non-legacy status, or Phase 7A fields
    incompatible_conns = conn.execute(
        sa.text(
            "SELECT 1 FROM provider_connections WHERE "
            "environment != 'SANDBOX' OR "
            "status NOT IN ('CONFIGURED', 'DISABLED', 'ERROR') OR "
            "encrypted_access_token IS NOT NULL OR "
            "broker_user_id IS NOT NULL OR "
            "account_name IS NOT NULL OR "
            "broker_user_type IS NOT NULL OR "
            "token_acquired_at IS NOT NULL OR "
            "token_expires_at IS NOT NULL OR "
            "last_successful_refresh_at IS NOT NULL OR "
            "authorization_fence IS NOT NULL "
            "LIMIT 1"
        )
    ).fetchone()
    if incompatible_conns:
        raise RuntimeError(
            "Cannot downgrade: provider_connections contains LIVE records, active/disconnected/expired Phase 7A statuses, "
            "or populated Phase 7A broker credentials."
        )

    # All preflights passed: proceed with clean drops without risk of half-applied DDL
    op.drop_table("broker_snapshots")
    op.drop_table("oauth_states")

    op.drop_index("uq_provider_connections_active_ucc", table_name="provider_connections")

    with op.batch_alter_table("provider_connections") as batch_op:
        batch_op.drop_constraint("uq_provider_connections_id_owner", type_="unique")
        batch_op.drop_constraint("ck_provider_connections_env", type_="check")
        batch_op.drop_constraint("ck_provider_connections_status", type_="check")
        batch_op.create_check_constraint(
            "ck_provider_connections_env_sandbox",
            "environment = 'SANDBOX'",
        )
        batch_op.create_check_constraint(
            "ck_provider_connections_status",
            "status IN ('CONFIGURED', 'DISABLED', 'ERROR')",
        )
        batch_op.drop_column("authorization_fence")
        batch_op.drop_column("credential_generation")
        batch_op.drop_column("version")
        batch_op.drop_column("last_successful_refresh_at")
        batch_op.drop_column("token_scope")
        batch_op.drop_column("token_expires_at")
        batch_op.drop_column("token_acquired_at")
        batch_op.drop_column("encrypted_access_token")
        batch_op.drop_column("broker_user_type")
        batch_op.drop_column("account_name")
        batch_op.drop_column("broker_user_id")

    with op.batch_alter_table("user_sessions") as batch_op:
        batch_op.drop_constraint("uq_user_sessions_id_user", type_="unique")
