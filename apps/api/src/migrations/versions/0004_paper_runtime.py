"""Paper trading runtime, OMS, risk engine and ledger

Revision ID: 0004_paper_runtime
Revises: 0003_auth_ownership
Create Date: 2026-09-13

"""
import os
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from src.database import UTCDateTime

# revision identifiers, used by Alembic.
revision: str = "0004_paper_runtime"
down_revision: Union[str, None] = "0003_auth_ownership"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    # 0. Add unique constraint on strategies(id, owner_id) for composite foreign key references
    with op.batch_alter_table("strategies") as batch_op:
        batch_op.create_unique_constraint("uq_strategies_id_owner", ["owner_id", "id"])

    # 1. paper_accounts
    op.create_table(
        "paper_accounts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("currency", sa.String(10), nullable=False, server_default="INR"),
        sa.Column("total_cash_units", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reserved_cash_units", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("total_cash_units >= 0", name="ck_paper_accounts_total_cash_nonneg"),
        sa.CheckConstraint("reserved_cash_units >= 0 AND reserved_cash_units <= total_cash_units", name="ck_paper_accounts_reserved_cash_bound"),
        sa.CheckConstraint("version > 0", name="ck_paper_accounts_version_pos"),
        sa.UniqueConstraint("owner_id", "id", name="uq_paper_accounts_owner_resource"),
    )
    op.create_index("ix_paper_accounts_owner_id", "paper_accounts", ["owner_id"])

    # 2. account_ledger_entries
    op.create_table(
        "account_ledger_entries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("paper_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("sequence_number", sa.BigInteger(), nullable=False),
        sa.Column("entry_type", sa.String(30), nullable=False),
        sa.Column("settled_cash_delta_units", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reserved_cash_delta_units", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("settled_cash_after_units", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reserved_cash_after_units", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("amount_units", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("balance_after_units", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("order_id", sa.String(36), nullable=True),
        sa.Column("fill_id", sa.String(36), nullable=True),
        sa.Column("reason_code", sa.String(50), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("sequence_number > 0", name="ck_account_ledger_seq_pos"),
        sa.CheckConstraint("settled_cash_after_units >= 0", name="ck_account_ledger_settled_after_nonneg"),
        sa.CheckConstraint("reserved_cash_after_units >= 0", name="ck_account_ledger_reserved_after_nonneg"),
        sa.UniqueConstraint("account_id", "sequence_number", name="uq_account_ledger_account_seq"),
        sa.UniqueConstraint("account_id", "idempotency_key", name="uq_account_ledger_account_idempotency"),
        sa.ForeignKeyConstraint(["account_id", "owner_id"], ["paper_accounts.id", "paper_accounts.owner_id"], name="fk_account_ledger_account_owner"),
    )
    op.create_index("ix_account_ledger_entries_account_id", "account_ledger_entries", ["account_id"])
    op.create_index("ix_account_ledger_entries_owner_id", "account_ledger_entries", ["owner_id"])

    # 3. strategy_action_policies
    op.create_table(
        "strategy_action_policies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("strategy_id", sa.String(36), sa.ForeignKey("strategies.id"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_action_policies_version_pos"),
        sa.UniqueConstraint("strategy_id", "version", name="uq_action_policies_strategy_version"),
        sa.UniqueConstraint("owner_id", "id", name="uq_action_policies_owner_resource"),
        sa.ForeignKeyConstraint(["strategy_id", "owner_id"], ["strategies.id", "strategies.owner_id"], onupdate="RESTRICT", ondelete="RESTRICT", name="fk_action_policies_strategy_owner"),
    )
    op.create_index("ix_strategy_action_policies_owner_id", "strategy_action_policies", ["owner_id"])
    op.create_index("ix_strategy_action_policies_strategy_id", "strategy_action_policies", ["strategy_id"])

    # 4. risk_policies
    op.create_table(
        "risk_policies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_risk_policies_version_pos"),
        sa.UniqueConstraint("owner_id", "name", "version", name="uq_risk_policies_owner_name_version"),
        sa.UniqueConstraint("owner_id", "id", name="uq_risk_policies_owner_resource"),
    )
    op.create_index("ix_risk_policies_owner_id", "risk_policies", ["owner_id"])

    # 5. strategy_runtimes
    op.create_table(
        "strategy_runtimes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("strategy_id", sa.String(36), sa.ForeignKey("strategies.id"), nullable=False),
        sa.Column("action_policy_id", sa.String(36), sa.ForeignKey("strategy_action_policies.id"), nullable=False),
        sa.Column("risk_policy_id", sa.String(36), sa.ForeignKey("risk_policies.id"), nullable=False),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("paper_accounts.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="DRAFT"),
        sa.Column("trading_mode", sa.String(10), nullable=False, server_default="PAPER"),
        sa.Column("dataset_id", sa.String(255), nullable=False),
        sa.Column("timeframe", sa.String(50), nullable=False),
        sa.Column("strategy_snapshot", sa.JSON(), nullable=True),
        sa.Column("action_policy_snapshot", sa.JSON(), nullable=True),
        sa.Column("risk_policy_snapshot", sa.JSON(), nullable=True),
        sa.Column("instrument_spec_snapshot", sa.JSON(), nullable=True),
        sa.Column("fee_model_snapshot", sa.JSON(), nullable=True),
        sa.Column("slippage_model_snapshot", sa.JSON(), nullable=True),
        sa.Column("dataset_checksum", sa.String(64), nullable=True),
        sa.Column("manifest_version", sa.String(50), nullable=False, server_default="1.0.0"),
        sa.Column("engine_version", sa.String(50), nullable=False, server_default="1.0.0"),
        sa.Column("runtime_schema_version", sa.String(50), nullable=False, server_default="1.0.0"),
        sa.Column("last_processed_candle_timestamp", UTCDateTime(), nullable=True),
        sa.Column("consecutive_errors", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("status IN ('DRAFT', 'READY', 'RUNNING', 'PAUSED', 'HALTED', 'STOPPED', 'COMPLETED', 'ERROR')", name="ck_strategy_runtimes_status"),
        sa.CheckConstraint("trading_mode = 'PAPER'", name="ck_strategy_runtimes_trading_mode"),
        sa.CheckConstraint("version > 0", name="ck_strategy_runtimes_version_pos"),
        sa.CheckConstraint("consecutive_errors >= 0", name="ck_strategy_runtimes_errors_nonneg"),
        sa.UniqueConstraint("owner_id", "id", name="uq_strategy_runtimes_owner_resource"),
        sa.ForeignKeyConstraint(["account_id", "owner_id"], ["paper_accounts.id", "paper_accounts.owner_id"], onupdate="RESTRICT", ondelete="RESTRICT", name="fk_strategy_runtimes_account_owner"),
        sa.ForeignKeyConstraint(["strategy_id", "owner_id"], ["strategies.id", "strategies.owner_id"], onupdate="RESTRICT", ondelete="RESTRICT", name="fk_strategy_runtimes_strategy_owner"),
        sa.ForeignKeyConstraint(["action_policy_id", "owner_id"], ["strategy_action_policies.id", "strategy_action_policies.owner_id"], onupdate="RESTRICT", ondelete="RESTRICT", name="fk_strategy_runtimes_action_policy_owner"),
        sa.ForeignKeyConstraint(["risk_policy_id", "owner_id"], ["risk_policies.id", "risk_policies.owner_id"], onupdate="RESTRICT", ondelete="RESTRICT", name="fk_strategy_runtimes_risk_policy_owner"),
    )
    op.create_index("ix_strategy_runtimes_owner_id", "strategy_runtimes", ["owner_id"])
    op.create_index("ix_strategy_runtimes_strategy_id", "strategy_runtimes", ["strategy_id"])
    op.create_index("ix_strategy_runtimes_action_policy_id", "strategy_runtimes", ["action_policy_id"])
    op.create_index("ix_strategy_runtimes_risk_policy_id", "strategy_runtimes", ["risk_policy_id"])
    op.create_index("ix_strategy_runtimes_account_id", "strategy_runtimes", ["account_id"])

    # 6. order_intents
    op.create_table(
        "order_intents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("runtime_id", sa.String(36), sa.ForeignKey("strategy_runtimes.id"), nullable=False),
        sa.Column("action_mapping_id", sa.String(50), nullable=False),
        sa.Column("requested_instrument_id", sa.String(255), nullable=False),
        sa.Column("resolved_instrument_id", sa.String(255), nullable=False),
        sa.Column("intent_type", sa.String(20), nullable=False),
        sa.Column("reduce_only", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("quantity_units", sa.BigInteger(), nullable=False),
        sa.Column("order_type", sa.String(10), nullable=False),
        sa.Column("limit_price_units", sa.BigInteger(), nullable=True),
        sa.Column("time_in_force", sa.String(10), nullable=False),
        sa.Column("source_candle_timestamp", UTCDateTime(), nullable=False),
        sa.Column("source_evaluation_fingerprint", sa.String(64), nullable=False),
        sa.Column("trigger_event_key", sa.String(64), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("intent_type IN ('ENTRY', 'EXIT', 'REDUCE', 'REVERSE')", name="ck_order_intents_type"),
        sa.CheckConstraint("side IN ('BUY', 'SELL')", name="ck_order_intents_side"),
        sa.CheckConstraint("quantity_units > 0", name="ck_order_intents_qty_pos"),
        sa.CheckConstraint("order_type IN ('MARKET', 'LIMIT')", name="ck_order_intents_order_type"),
        sa.CheckConstraint("time_in_force IN ('DAY', 'GTC')", name="ck_order_intents_tif"),
        sa.CheckConstraint("order_type != 'LIMIT' OR (limit_price_units IS NOT NULL AND limit_price_units > 0)", name="ck_order_intents_limit_price_pos"),
        sa.CheckConstraint("order_type != 'MARKET' OR limit_price_units IS NULL", name="ck_order_intents_market_no_price"),
        sa.UniqueConstraint("runtime_id", "trigger_event_key", name="uq_order_intents_trigger_event"),
        sa.UniqueConstraint("owner_id", "id", name="uq_order_intents_owner_resource"),
        sa.ForeignKeyConstraint(["runtime_id", "owner_id"], ["strategy_runtimes.id", "strategy_runtimes.owner_id"], onupdate="RESTRICT", ondelete="RESTRICT", name="fk_order_intents_runtime_owner"),
    )
    op.create_index("ix_order_intents_owner_id", "order_intents", ["owner_id"])
    op.create_index("ix_order_intents_runtime_id", "order_intents", ["runtime_id"])

    # 7. orders
    op.create_table(
        "orders",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("runtime_id", sa.String(36), sa.ForeignKey("strategy_runtimes.id"), nullable=False),
        sa.Column("intent_id", sa.String(36), sa.ForeignKey("order_intents.id"), nullable=False),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("paper_accounts.id"), nullable=False),
        sa.Column("order_sequence_number", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.String(255), nullable=False),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("order_type", sa.String(10), nullable=False),
        sa.Column("quantity_units", sa.BigInteger(), nullable=False),
        sa.Column("limit_price_units", sa.BigInteger(), nullable=True),
        sa.Column("filled_quantity_units", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="CREATED"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("side IN ('BUY', 'SELL')", name="ck_orders_side"),
        sa.CheckConstraint("order_type IN ('MARKET', 'LIMIT')", name="ck_orders_order_type"),
        sa.CheckConstraint("quantity_units > 0", name="ck_orders_qty_pos"),
        sa.CheckConstraint("order_sequence_number > 0", name="ck_orders_seq_pos"),
        sa.CheckConstraint("filled_quantity_units >= 0 AND filled_quantity_units <= quantity_units", name="ck_orders_filled_bounds"),
        sa.CheckConstraint("status IN ('CREATED', 'ACCEPTED', 'PARTIALLY_FILLED', 'FILLED', 'CANCEL_PENDING', 'CANCELLED', 'REJECTED', 'EXPIRED', 'RISK_REJECTED', 'ERROR')", name="ck_orders_status"),
        sa.CheckConstraint("order_type != 'LIMIT' OR (limit_price_units IS NOT NULL AND limit_price_units > 0)", name="ck_orders_limit_requires_price"),
        sa.CheckConstraint("order_type != 'MARKET' OR limit_price_units IS NULL", name="ck_orders_market_forbids_price"),
        sa.CheckConstraint("version > 0", name="ck_orders_version_pos"),
        sa.UniqueConstraint("intent_id", name="uq_orders_intent_id"),
        sa.UniqueConstraint("runtime_id", "order_sequence_number", name="uq_orders_runtime_seq"),
        sa.UniqueConstraint("owner_id", "id", name="uq_orders_owner_resource"),
        sa.ForeignKeyConstraint(["runtime_id", "owner_id"], ["strategy_runtimes.id", "strategy_runtimes.owner_id"], onupdate="RESTRICT", ondelete="RESTRICT", name="fk_orders_runtime_owner"),
        sa.ForeignKeyConstraint(["account_id", "owner_id"], ["paper_accounts.id", "paper_accounts.owner_id"], onupdate="RESTRICT", ondelete="RESTRICT", name="fk_orders_account_owner"),
        sa.ForeignKeyConstraint(["intent_id", "owner_id"], ["order_intents.id", "order_intents.owner_id"], onupdate="RESTRICT", ondelete="RESTRICT", name="fk_orders_intent_owner"),
    )
    op.create_index("ix_orders_owner_id", "orders", ["owner_id"])
    op.create_index("ix_orders_runtime_id", "orders", ["runtime_id"])
    op.create_index("ix_orders_account_id", "orders", ["account_id"])

    # 8. order_events
    op.create_table(
        "order_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("order_id", sa.String(36), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("previous_status", sa.String(20), nullable=False),
        sa.Column("new_status", sa.String(20), nullable=False),
        sa.Column("actor", sa.String(50), nullable=False),
        sa.Column("reason_code", sa.String(50), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("sequence_number > 0", name="ck_order_events_seq_pos"),
        sa.UniqueConstraint("order_id", "sequence_number", name="uq_order_events_order_seq"),
        sa.ForeignKeyConstraint(["owner_id", "order_id"], ["orders.owner_id", "orders.id"], onupdate="RESTRICT", ondelete="RESTRICT", name="fk_order_events_order_owner"),
    )
    op.create_index("ix_order_events_owner_id", "order_events", ["owner_id"])
    op.create_index("ix_order_events_order_id", "order_events", ["order_id"])

    # 9. fills
    op.create_table(
        "fills",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("order_id", sa.String(36), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("paper_accounts.id"), nullable=False),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("instrument_id", sa.String(255), nullable=False),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("quantity_units", sa.BigInteger(), nullable=False),
        sa.Column("price_units", sa.BigInteger(), nullable=False),
        sa.Column("fee_units", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("candle_timestamp", UTCDateTime(), nullable=False),
        sa.Column("fill_idempotency_key", sa.String(64), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("quantity_units > 0", name="ck_fills_qty_pos"),
        sa.CheckConstraint("price_units > 0", name="ck_fills_price_pos"),
        sa.CheckConstraint("fee_units >= 0", name="ck_fills_fee_nonneg"),
        sa.UniqueConstraint("fill_idempotency_key", name="uq_fills_idempotency_key"),
        sa.UniqueConstraint("owner_id", "id", name="uq_fills_owner_resource"),
        sa.ForeignKeyConstraint(["order_id", "owner_id"], ["orders.id", "orders.owner_id"], onupdate="RESTRICT", ondelete="RESTRICT", name="fk_fills_order_owner"),
        sa.ForeignKeyConstraint(["account_id", "owner_id"], ["paper_accounts.id", "paper_accounts.owner_id"], onupdate="RESTRICT", ondelete="RESTRICT", name="fk_fills_account_owner"),
    )
    op.create_index("ix_fills_order_id", "fills", ["order_id"])
    op.create_index("ix_fills_account_id", "fills", ["account_id"])
    op.create_index("ix_fills_owner_id", "fills", ["owner_id"])

    # 10. paper_positions
    op.create_table(
        "paper_positions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("paper_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("instrument_id", sa.String(255), nullable=False),
        sa.Column("net_quantity_units", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("average_entry_price_units", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cost_basis_units", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("gross_realized_pnl_units", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("total_fees_units", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("net_realized_pnl_units", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_mark_price_units", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("unrealized_pnl_units", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("cost_basis_units >= 0", name="ck_paper_positions_cost_basis_nonneg"),
        sa.CheckConstraint("total_fees_units >= 0", name="ck_paper_positions_total_fees_nonneg"),
        sa.UniqueConstraint("account_id", "instrument_id", name="uq_paper_positions_account_instrument"),
        sa.ForeignKeyConstraint(["account_id", "owner_id"], ["paper_accounts.id", "paper_accounts.owner_id"], onupdate="RESTRICT", ondelete="RESTRICT", name="fk_paper_positions_account_owner"),
    )
    op.create_index("ix_paper_positions_owner_id", "paper_positions", ["owner_id"])
    op.create_index("ix_paper_positions_account_id", "paper_positions", ["account_id"])

    # 11. kill_switches
    op.create_table(
        "kill_switches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("target_key", sa.String(50), nullable=False),
        sa.Column("scope", sa.String(20), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("engaged_by", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("engaged_at", UTCDateTime(), nullable=True),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.CheckConstraint("scope IN ('GLOBAL', 'USER')", name="ck_kill_switches_scope"),
        sa.CheckConstraint("scope != 'GLOBAL' OR user_id IS NULL", name="ck_kill_switches_global_no_user"),
        sa.CheckConstraint("scope != 'USER' OR user_id IS NOT NULL", name="ck_kill_switches_user_requires_user"),
        sa.UniqueConstraint("target_key", name="uq_kill_switches_target_key"),
        sa.UniqueConstraint("scope", "user_id", name="uq_kill_switches_scope_user"),
    )
    op.create_index("ix_kill_switches_target_key", "kill_switches", ["target_key"])
    op.create_index("ix_kill_switches_user_id", "kill_switches", ["user_id"])

    # 12. api_idempotency_records
    op.create_table(
        "api_idempotency_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column("response_body", sa.JSON(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.UniqueConstraint("owner_id", "key", name="uq_api_idempotency_owner_key"),
    )
    op.create_index("ix_api_idempotency_records_owner_id", "api_idempotency_records", ["owner_id"])

    # 13. runtime_events
    op.create_table(
        "runtime_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("runtime_id", sa.String(36), sa.ForeignKey("strategy_runtimes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("previous_status", sa.String(20), nullable=False),
        sa.Column("new_status", sa.String(20), nullable=False),
        sa.Column("actor", sa.String(50), nullable=False),
        sa.Column("reason_code", sa.String(50), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("sequence_number > 0", name="ck_runtime_events_seq_pos"),
        sa.UniqueConstraint("runtime_id", "sequence_number", name="uq_runtime_events_runtime_seq"),
    )
    op.create_index("ix_runtime_events_runtime_id", "runtime_events", ["runtime_id"])

    # 14. action_decisions
    op.create_table(
        "action_decisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("runtime_id", sa.String(36), nullable=False),
        sa.Column("candle_timestamp", UTCDateTime(), nullable=False),
        sa.Column("action_mapping_id", sa.String(50), nullable=False),
        sa.Column("decision", sa.String(20), nullable=False),
        sa.Column("reason_code", sa.String(50), nullable=False),
        sa.Column("intent_id", sa.String(36), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["owner_id", "runtime_id"], ["strategy_runtimes.owner_id", "strategy_runtimes.id"], onupdate="RESTRICT", ondelete="RESTRICT", name="fk_action_decisions_runtime_owner"),
    )
    op.create_index("ix_action_decisions_owner_id", "action_decisions", ["owner_id"])
    op.create_index("ix_action_decisions_runtime_id", "action_decisions", ["runtime_id"])

    # 15. risk_decisions
    op.create_table(
        "risk_decisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("intent_id", sa.String(36), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("reason_code", sa.String(50), nullable=False),
        sa.Column("message", sa.String(500), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["owner_id", "intent_id"], ["order_intents.owner_id", "order_intents.id"], onupdate="RESTRICT", ondelete="RESTRICT", name="fk_risk_decisions_intent_owner"),
    )
    op.create_index("ix_risk_decisions_intent_id", "risk_decisions", ["intent_id"])

def downgrade() -> None:
    # Check all 15 business/configuration/audit tables to protect historical trading data
    bind = op.get_bind()
    tables_to_check = [
        "risk_decisions",
        "action_decisions",
        "runtime_events",
        "api_idempotency_records",
        "kill_switches",
        "paper_positions",
        "fills",
        "order_events",
        "orders",
        "order_intents",
        "strategy_runtimes",
        "risk_policies",
        "strategy_action_policies",
        "account_ledger_entries",
        "paper_accounts",
    ]
    for table_name in tables_to_check:
        try:
            row_count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table_name}")).scalar() or 0
            if row_count > 0:
                raise RuntimeError(
                    f"Downgrade refused: Non-empty paper trading table '{table_name}' detected ({row_count} rows). "
                    "Downgrading would permanently destroy historical data. Manual database backup and recovery required before schema changes."
                )
        except Exception as e:
            if "no such table" not in str(e).lower() and "does not exist" not in str(e).lower() and "Downgrade refused" in str(e):
                raise

    op.drop_table("risk_decisions")
    op.drop_table("action_decisions")
    op.drop_table("runtime_events")
    op.drop_table("api_idempotency_records")
    op.drop_table("kill_switches")
    op.drop_table("paper_positions")
    op.drop_table("fills")
    op.drop_table("order_events")
    op.drop_table("orders")
    op.drop_table("order_intents")
    op.drop_table("strategy_runtimes")
    op.drop_table("risk_policies")
    op.drop_table("strategy_action_policies")
    op.drop_table("account_ledger_entries")
    op.drop_table("paper_accounts")

    with op.batch_alter_table("strategies") as batch_op:
        batch_op.drop_constraint("uq_strategies_id_owner", type_="unique")
