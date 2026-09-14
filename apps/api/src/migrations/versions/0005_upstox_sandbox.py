"""Upstox Sandbox integration, instrument mapping, submission outbox and reconciliation

Revision ID: 0005_upstox_sandbox
Revises: 0004_paper_runtime
Create Date: 2026-09-13

"""
import os
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from src.database import UTCDateTime

# revision identifiers, used by Alembic.
revision: str = "0005_upstox_sandbox"
down_revision: Union[str, None] = "0004_paper_runtime"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 0. Alter existing tables to support BROKER_SANDBOX mode, IOC time-in-force, new order statuses, and composite FK target
    with op.batch_alter_table("orders") as batch_op:
        # Update check constraint on orders.status
        batch_op.drop_constraint("ck_orders_status", type_="check")
        batch_op.create_check_constraint(
            "ck_orders_status",
            "status IN ('CREATED', 'ACCEPTED', 'PENDING_SUBMISSION', 'ACKNOWLEDGED', 'PARTIALLY_FILLED', 'FILLED', 'CANCEL_PENDING', 'CANCELLED', 'REJECTED', 'PROVIDER_REJECTED', 'RECONCILIATION_REQUIRED', 'EXPIRED', 'RISK_REJECTED', 'ERROR')"
        )

    with op.batch_alter_table("strategy_runtimes") as batch_op:
        batch_op.drop_constraint("ck_strategy_runtimes_trading_mode", type_="check")
        batch_op.create_check_constraint(
            "ck_strategy_runtimes_trading_mode",
            "trading_mode IN ('PAPER', 'BROKER_SANDBOX', 'BROKER_SANDBOX_RECORDED_FIXTURE')"
        )

    with op.batch_alter_table("order_intents") as batch_op:
        batch_op.drop_constraint("ck_order_intents_tif", type_="check")
        batch_op.create_check_constraint(
            "ck_order_intents_tif",
            "time_in_force IN ('DAY', 'GTC', 'IOC')"
        )

    # 1. provider_connections
    op.create_table(
        "provider_connections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("provider_name", sa.String(50), nullable=False, server_default="UPSTOX"),
        sa.Column("environment", sa.String(50), nullable=False, server_default="SANDBOX"),
        sa.Column("credential_reference", sa.String(100), nullable=False),
        sa.Column("credential_version", sa.String(50), nullable=False, server_default="v1"),
        sa.Column("status", sa.String(50), nullable=False, server_default="CONFIGURED"),
        sa.Column("last_successful_transmission_at", UTCDateTime(), nullable=True),
        sa.Column("sanitized_error_code", sa.String(100), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("environment = 'SANDBOX'", name="ck_provider_connections_env_sandbox"),
        sa.CheckConstraint("provider_name = 'UPSTOX'", name="ck_provider_connections_provider_upstox"),
        sa.CheckConstraint("status IN ('CONFIGURED', 'DISABLED', 'ERROR')", name="ck_provider_connections_status"),
        sa.UniqueConstraint("owner_id", "provider_name", "environment", name="uq_provider_conns_owner_prov_env"),
    )
    op.create_index("ix_provider_connections_owner_id", "provider_connections", ["owner_id"])

    # 2. provider_instrument_mappings
    op.create_table(
        "provider_instrument_mappings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("tradepro_instrument_id", sa.String(100), nullable=False),
        sa.Column("provider_instrument_token", sa.String(100), nullable=False),
        sa.Column("exchange", sa.String(20), nullable=False),
        sa.Column("segment", sa.String(20), nullable=False),
        sa.Column("symbol", sa.String(100), nullable=False),
        sa.Column("expiry_date", UTCDateTime(), nullable=True),
        sa.Column("strike_price_units", sa.BigInteger(), nullable=True),
        sa.Column("option_type", sa.String(10), nullable=True),
        sa.Column("lot_size_units", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("tick_size_units", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("freeze_quantity_units", sa.Integer(), nullable=False, server_default="1800"),
        sa.Column("verification_status", sa.String(20), nullable=False, server_default="UNVERIFIED"),
        sa.Column("verified_by", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("verified_at", UTCDateTime(), nullable=True),
        sa.Column("verification_audit_json", sa.JSON(), nullable=True),
        sa.Column("mapping_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("verification_status IN ('UNVERIFIED', 'VERIFIED', 'REJECTED', 'DISABLED')", name="ck_prov_inst_map_status"),
        sa.UniqueConstraint("owner_id", "tradepro_instrument_id", "mapping_version", name="uq_prov_inst_map_owner_inst_ver"),
    )
    op.create_index("ix_provider_instrument_mappings_owner_id", "provider_instrument_mappings", ["owner_id"])
    op.create_index("ix_provider_instrument_mappings_tradepro_instrument_id", "provider_instrument_mappings", ["tradepro_instrument_id"])

    # 3. submission_outbox
    op.create_table(
        "submission_outbox",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("order_id", sa.String(36), nullable=False),
        sa.Column("action_type", sa.String(20), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("status", sa.String(30), nullable=False, server_default="PENDING"),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("canonical_payload_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("claim_lease_until", UTCDateTime(), nullable=True),
        sa.Column("claimed_by", sa.String(100), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("next_attempt_at", UTCDateTime(), nullable=False),
        sa.Column("last_error_code", sa.String(100), nullable=True),
        sa.Column("last_error_message", sa.String(500), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("action_type IN ('PLACE', 'CANCEL')", name="ck_submission_outbox_action_type"),
        sa.CheckConstraint("(action_type = 'CANCEL' AND priority = 0) OR (action_type = 'PLACE' AND priority = 10)", name="ck_submission_outbox_priority_action"),
        sa.CheckConstraint("status IN ('PENDING', 'CLAIMED', 'DELIVERED', 'RETRY_SCHEDULED', 'RECONCILIATION_REQUIRED', 'DEAD_LETTER')", name="ck_submission_outbox_status"),
        sa.UniqueConstraint("owner_id", "idempotency_key", name="uq_submission_outbox_owner_idem"),
        sa.UniqueConstraint("owner_id", "id", name="uq_submission_outbox_owner_resource"),
        sa.ForeignKeyConstraint(["owner_id", "order_id"], ["orders.owner_id", "orders.id"], onupdate="RESTRICT", ondelete="RESTRICT", name="fk_submission_outbox_order_owner"),
    )
    op.create_index("ix_submission_outbox_owner_id", "submission_outbox", ["owner_id"])
    op.create_index("ix_submission_outbox_order_id", "submission_outbox", ["order_id"])
    op.create_index("ix_submission_outbox_priority", "submission_outbox", ["priority"])
    op.create_index("ix_submission_outbox_claim_lease_until", "submission_outbox", ["claim_lease_until"])
    op.create_index("ix_submission_outbox_next_attempt_at", "submission_outbox", ["next_attempt_at"])

    # 4. external_order_links
    op.create_table(
        "external_order_links",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("order_id", sa.String(36), nullable=False),
        sa.Column("provider_name", sa.String(50), nullable=False, server_default="UPSTOX"),
        sa.Column("provider_order_id", sa.String(100), nullable=False),
        sa.Column("submitted_at", UTCDateTime(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("provider_name = 'UPSTOX'", name="ck_external_order_links_provider"),
        sa.UniqueConstraint("owner_id", "provider_order_id", name="uq_ext_order_links_owner_prov_id"),
        sa.ForeignKeyConstraint(["owner_id", "order_id"], ["orders.owner_id", "orders.id"], onupdate="RESTRICT", ondelete="RESTRICT", name="fk_external_order_links_order_owner"),
    )
    op.create_index("ix_external_order_links_owner_id", "external_order_links", ["owner_id"])
    op.create_index("ix_external_order_links_order_id", "external_order_links", ["order_id"])
    op.create_index("ix_external_order_links_provider_order_id", "external_order_links", ["provider_order_id"])

    # 5. reconciliation_records
    op.create_table(
        "reconciliation_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id", onupdate="RESTRICT", ondelete="RESTRICT"), nullable=False),
        sa.Column("order_id", sa.String(36), nullable=False),
        sa.Column("outbox_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default='OPEN'),
        sa.Column("resolution_type", sa.String(30), nullable=True),
        sa.Column("resolved_by", sa.String(36), sa.ForeignKey("users.id", onupdate="RESTRICT", ondelete="RESTRICT"), nullable=True),
        sa.Column("provider_order_reference", sa.String(100), nullable=True),
        sa.Column("notes", sa.String(1000), nullable=True),
        sa.Column("resolved_at", UTCDateTime(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("status IN ('OPEN', 'RESOLVED')", name="ck_reconciliation_records_status"),
        sa.CheckConstraint("resolution_type IN ('PLACE_CONFIRMED', 'PLACE_REJECTED', 'CANCEL_CONFIRMED', 'CANCEL_NOT_CONFIRMED')", name="ck_reconciliation_records_resolution_type"),
        sa.CheckConstraint(
            "(status = 'OPEN' AND resolution_type IS NULL AND resolved_by IS NULL AND resolved_at IS NULL) OR "
            "(status = 'RESOLVED' AND resolution_type IS NOT NULL AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL)",
            name="ck_reconciliation_records_lifecycle"
        ),
        sa.UniqueConstraint("owner_id", "outbox_id", name="uq_reconciliation_records_owner_outbox"),
        sa.ForeignKeyConstraint(["owner_id", "order_id"], ["orders.owner_id", "orders.id"], onupdate="RESTRICT", ondelete="RESTRICT", name="fk_reconciliation_records_order_owner"),
        sa.ForeignKeyConstraint(["owner_id", "outbox_id"], ["submission_outbox.owner_id", "submission_outbox.id"], onupdate="RESTRICT", ondelete="RESTRICT", name="fk_reconciliation_records_outbox_owner"),
    )
    op.create_index("ix_reconciliation_records_owner_id", "reconciliation_records", ["owner_id"])
    op.create_index("ix_reconciliation_records_order_id", "reconciliation_records", ["order_id"])
    op.create_index("ix_reconciliation_records_outbox_id", "reconciliation_records", ["outbox_id"])

    # 6. worker_heartbeats
    op.create_table(
        "worker_heartbeats",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("worker_id", sa.String(100), nullable=False),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("provider_name", sa.String(50), nullable=False, server_default="UPSTOX"),
        sa.Column("status", sa.String(20), nullable=False, server_default="HEALTHY"),
        sa.Column("last_heartbeat_at", UTCDateTime(), nullable=False),
        sa.Column("batch_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("processed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("status IN ('HEALTHY', 'STOPPED', 'ERROR')", name="ck_worker_heartbeats_status"),
        sa.UniqueConstraint("worker_id", name="uq_worker_heartbeats_worker_id"),
    )
    op.create_index("ix_worker_heartbeats_worker_id", "worker_heartbeats", ["worker_id"])


def downgrade() -> None:
    # Downgrade guard refusing downgrade when any 6B table contains data
    bind = op.get_bind()
    tables_to_check = [
        "worker_heartbeats",
        "reconciliation_records",
        "external_order_links",
        "submission_outbox",
        "provider_instrument_mappings",
        "provider_connections",
    ]
    for table_name in tables_to_check:
        try:
            row_count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table_name}")).scalar() or 0
            if row_count > 0:
                raise RuntimeError(
                    f"Downgrade refused: Non-empty Upstox sandbox table '{table_name}' detected ({row_count} rows). "
                    "Downgrading would permanently destroy sandbox execution and audit records. Manual database backup and recovery required before schema changes."
                )
        except Exception as e:
            if "no such table" not in str(e).lower() and "does not exist" not in str(e).lower() and "Downgrade refused" in str(e):
                raise

    op.drop_table("worker_heartbeats")
    op.drop_table("reconciliation_records")
    op.drop_table("external_order_links")
    op.drop_table("submission_outbox")
    op.drop_table("provider_instrument_mappings")
    op.drop_table("provider_connections")

    with op.batch_alter_table("order_intents") as batch_op:
        batch_op.drop_constraint("ck_order_intents_tif", type_="check")
        batch_op.create_check_constraint(
            "ck_order_intents_tif",
            "time_in_force IN ('DAY', 'GTC')"
        )

    with op.batch_alter_table("strategy_runtimes") as batch_op:
        batch_op.drop_constraint("ck_strategy_runtimes_trading_mode", type_="check")
        batch_op.create_check_constraint(
            "ck_strategy_runtimes_trading_mode",
            "trading_mode = 'PAPER'"
        )

    with op.batch_alter_table("orders") as batch_op:
        batch_op.drop_constraint("ck_orders_status", type_="check")
        batch_op.create_check_constraint(
            "ck_orders_status",
            "status IN ('CREATED', 'ACCEPTED', 'PARTIALLY_FILLED', 'FILLED', 'CANCEL_PENDING', 'CANCELLED', 'REJECTED', 'EXPIRED', 'RISK_REJECTED')"
        )
