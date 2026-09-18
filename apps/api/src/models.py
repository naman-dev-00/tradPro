import datetime
import uuid
from sqlalchemy import Column, String, DateTime, JSON, Boolean, CheckConstraint, UniqueConstraint, ForeignKey, ForeignKeyConstraint, text, BigInteger, Integer, Text, Index, event, inspect
from src.database import Base, UTCDateTime

LEGACY_PRINCIPAL_ID = "00000000-0000-0000-0000-000000000000"

class User(Base):
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username = Column(String(50), nullable=False)
    normalized_username = Column(String(50), nullable=False, unique=True, index=True)
    email = Column(String(255), nullable=False)
    normalized_email = Column(String(255), nullable=False, unique=True, index=True)
    hashed_password = Column(String(255), nullable=False)
    role = Column(String(20), nullable=False, default="VIEWER")
    is_active = Column(Boolean, nullable=False, default=True, server_default=text('true'), index=True)
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))
    updated_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc), onupdate=lambda: datetime.datetime.now(datetime.timezone.utc))

    __table_args__ = (
        CheckConstraint("role IN ('VIEWER', 'EDITOR', 'ADMIN')", name="ck_users_role"),
        CheckConstraint("length(username) >= 3 AND length(username) <= 50", name="ck_users_username_len"),
    )


class UserSession(Base):
    __tablename__ = "user_sessions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    session_hash = Column(String(64), nullable=False, unique=True, index=True)
    csrf_hash = Column(String(64), nullable=False)
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))
    last_accessed_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))
    idle_expires_at = Column(UTCDateTime, nullable=False, index=True)
    absolute_expires_at = Column(UTCDateTime, nullable=False, index=True)
    is_revoked = Column(Boolean, nullable=False, default=False, server_default=text('false'), index=True)
    revoked_at = Column(UTCDateTime, nullable=True)


class Strategy(Base):
    __tablename__ = "strategies"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(String(1024), nullable=True)
    timeframe = Column(String(50), nullable=False)
    candidate_selection_mode = Column(String(50), nullable=False, default="FIRST_ELIGIBLE")
    payload = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc), onupdate=lambda: datetime.datetime.now(datetime.timezone.utc))

    __table_args__ = (
        UniqueConstraint("id", "owner_id", name="uq_strategies_id_owner"),
    )

    @property
    def action(self):
        return self.payload.get("action")

    @property
    def global_conditions(self):
        return self.payload.get("global_conditions")

    @property
    def candidate_conditions(self):
        return self.payload.get("candidate_conditions")


class InspectionRun(Base):
    __tablename__ = "inspection_runs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    strategy_id = Column(String(36), index=True, nullable=True)
    strategy_version_snapshot = Column(String(100), nullable=True)
    strategy_definition_snapshot = Column(JSON(none_as_null=True), nullable=True)
    run_type = Column(String(50), index=True, nullable=False)
    reference_dataset_id = Column(String(255), nullable=True)
    subject_dataset_ids = Column(JSON, nullable=False)
    requested_start_timestamp = Column(UTCDateTime, nullable=True)
    requested_end_timestamp = Column(UTCDateTime, nullable=True)
    requested_evaluation_timestamp = Column(UTCDateTime, nullable=True)
    timeframe = Column(String(50), nullable=False)
    engine_version = Column(String(50), nullable=False, default="1.0.0")
    manifest_version = Column(String(50), nullable=False, default="1.0.0")
    created_at = Column(UTCDateTime, index=True, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))
    completed_at = Column(UTCDateTime, nullable=True)
    status = Column(String(50), index=True, nullable=False)
    failure_summary = Column(String(2048), nullable=True)
    result_summary = Column(String(2048), nullable=True)
    result_payload = Column(JSON(none_as_null=True), nullable=True)
    synthetic_data_confirmed = Column(Boolean, nullable=False, default=True, server_default=text('true'))
    request_fingerprint = Column(String(64), index=True, nullable=True)
    completed_fingerprint = Column(String(64), index=True, nullable=True)
    manifest_checksums_snapshot = Column(JSON(none_as_null=True), nullable=True)

    __table_args__ = (
        CheckConstraint("run_type IN ('SINGLE_SERIES', 'MULTI_SERIES', 'HISTORICAL_REPLAY')", name="ck_inspection_runs_run_type"),
        CheckConstraint("status IN ('COMPLETED', 'FAILED')", name="ck_inspection_runs_status"),
        CheckConstraint("synthetic_data_confirmed IS TRUE", name="ck_inspection_runs_synthetic_confirmed"),
        CheckConstraint(
            "status != 'COMPLETED' OR ("
            "completed_at IS NOT NULL AND "
            "strategy_definition_snapshot IS NOT NULL AND "
            "reference_dataset_id IS NOT NULL AND "
            "subject_dataset_ids IS NOT NULL AND "
            "requested_start_timestamp IS NOT NULL AND "
            "requested_end_timestamp IS NOT NULL AND "
            "result_payload IS NOT NULL AND "
            "manifest_checksums_snapshot IS NOT NULL AND "
            "failure_summary IS NULL"
            ")",
            name="ck_inspection_runs_completed_fields"
        ),
        CheckConstraint(
            "status != 'FAILED' OR ("
            "failure_summary IS NOT NULL AND "
            "result_payload IS NULL"
            ")",
            name="ck_inspection_runs_failed_fields"
        ),
        UniqueConstraint("owner_id", "completed_fingerprint", name="uq_inspection_runs_owner_completed_fingerprint"),
    )


class PaperAccount(Base):
    __tablename__ = "paper_accounts"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    currency = Column(String(10), nullable=False, default="INR")
    total_cash_units = Column(BigInteger, nullable=False, default=0)
    reserved_cash_units = Column(BigInteger, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))
    updated_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc), onupdate=lambda: datetime.datetime.now(datetime.timezone.utc))

    __table_args__ = (
        CheckConstraint("total_cash_units >= 0", name="ck_paper_accounts_total_cash_nonneg"),
        CheckConstraint("reserved_cash_units >= 0 AND reserved_cash_units <= total_cash_units", name="ck_paper_accounts_reserved_cash_bound"),
        CheckConstraint("version > 0", name="ck_paper_accounts_version_pos"),
        UniqueConstraint("id", "owner_id", name="uq_paper_accounts_id_owner"),
    )


class AccountLedgerEntry(Base):
    __tablename__ = "account_ledger_entries"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id = Column(String(36), ForeignKey("paper_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    sequence_number = Column(BigInteger, nullable=False)
    entry_type = Column(String(30), nullable=False)
    settled_cash_delta_units = Column(BigInteger, nullable=False, default=0)
    reserved_cash_delta_units = Column(BigInteger, nullable=False, default=0)
    settled_cash_after_units = Column(BigInteger, nullable=False, default=0)
    reserved_cash_after_units = Column(BigInteger, nullable=False, default=0)
    amount_units = Column(BigInteger, nullable=False, default=0)
    balance_after_units = Column(BigInteger, nullable=False, default=0)
    order_id = Column(String(36), nullable=True)
    fill_id = Column(String(36), nullable=True)
    reason_code = Column(String(50), nullable=False)
    idempotency_key = Column(String(64), nullable=False)
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))

    __table_args__ = (
        CheckConstraint("sequence_number > 0", name="ck_account_ledger_seq_pos"),
        CheckConstraint("settled_cash_after_units >= 0", name="ck_account_ledger_settled_after_nonneg"),
        CheckConstraint("reserved_cash_after_units >= 0", name="ck_account_ledger_reserved_after_nonneg"),
        UniqueConstraint("account_id", "sequence_number", name="uq_account_ledger_account_seq"),
        UniqueConstraint("account_id", "idempotency_key", name="uq_account_ledger_account_idempotency"),
        ForeignKeyConstraint(["account_id", "owner_id"], ["paper_accounts.id", "paper_accounts.owner_id"], name="fk_account_ledger_account_owner"),
    )


class StrategyActionPolicy(Base):
    __tablename__ = "strategy_action_policies"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    strategy_id = Column(String(36), ForeignKey("strategies.id"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    payload = Column(JSON, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))
    updated_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc), onupdate=lambda: datetime.datetime.now(datetime.timezone.utc))

    __table_args__ = (
        CheckConstraint("version > 0", name="ck_action_policies_version_pos"),
        UniqueConstraint("strategy_id", "version", name="uq_action_policies_strategy_version"),
        UniqueConstraint("id", "owner_id", name="uq_action_policies_id_owner"),
        ForeignKeyConstraint(["strategy_id", "owner_id"], ["strategies.id", "strategies.owner_id"], name="fk_action_policies_strategy_owner"),
    )


class RiskPolicy(Base):
    __tablename__ = "risk_policies"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    payload = Column(JSON, nullable=False)
    is_default = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))
    updated_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc), onupdate=lambda: datetime.datetime.now(datetime.timezone.utc))

    __table_args__ = (
        CheckConstraint("version > 0", name="ck_risk_policies_version_pos"),
        UniqueConstraint("owner_id", "name", "version", name="uq_risk_policies_owner_name_version"),
        UniqueConstraint("id", "owner_id", name="uq_risk_policies_id_owner"),
    )


class StrategyRuntime(Base):
    __tablename__ = "strategy_runtimes"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    strategy_id = Column(String(36), nullable=False, index=True)
    action_policy_id = Column(String(36), nullable=False, index=True)
    risk_policy_id = Column(String(36), nullable=False, index=True)
    account_id = Column(String(36), nullable=False, index=True)
    status = Column(String(20), nullable=False, default="DRAFT")
    trading_mode = Column(String(50), nullable=False, default="PAPER")
    dataset_id = Column(String(255), nullable=False)
    timeframe = Column(String(50), nullable=False)
    strategy_snapshot = Column(JSON, nullable=True)
    action_policy_snapshot = Column(JSON, nullable=True)
    risk_policy_snapshot = Column(JSON, nullable=True)
    instrument_spec_snapshot = Column(JSON, nullable=True)
    fee_model_snapshot = Column(JSON, nullable=True)
    slippage_model_snapshot = Column(JSON, nullable=True)
    dataset_checksum = Column(String(64), nullable=True)
    manifest_version = Column(String(50), nullable=False, default="1.0.0")
    engine_version = Column(String(50), nullable=False, default="1.0.0")
    runtime_schema_version = Column(String(50), nullable=False, default="1.0.0")
    last_processed_candle_timestamp = Column(UTCDateTime, nullable=True)
    consecutive_errors = Column(Integer, nullable=False, default=0)
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))
    updated_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc), onupdate=lambda: datetime.datetime.now(datetime.timezone.utc))

    __table_args__ = (
        CheckConstraint("status IN ('DRAFT', 'READY', 'RUNNING', 'PAUSED', 'HALTED', 'STOPPED', 'COMPLETED', 'ERROR')", name="ck_strategy_runtimes_status"),
        CheckConstraint("trading_mode IN ('PAPER', 'BROKER_SANDBOX', 'BROKER_SANDBOX_RECORDED_FIXTURE')", name="ck_strategy_runtimes_trading_mode"),
        CheckConstraint("version > 0", name="ck_strategy_runtimes_version_pos"),
        CheckConstraint("consecutive_errors >= 0", name="ck_strategy_runtimes_errors_nonneg"),
        UniqueConstraint("id", "owner_id", name="uq_strategy_runtimes_id_owner"),
        UniqueConstraint("owner_id", "id", name="uq_strategy_runtimes_owner_resource"),
        ForeignKeyConstraint(["account_id", "owner_id"], ["paper_accounts.id", "paper_accounts.owner_id"], name="fk_strategy_runtimes_account_owner"),
        ForeignKeyConstraint(["strategy_id", "owner_id"], ["strategies.id", "strategies.owner_id"], name="fk_strategy_runtimes_strategy_owner"),
        ForeignKeyConstraint(["action_policy_id", "owner_id"], ["strategy_action_policies.id", "strategy_action_policies.owner_id"], name="fk_strategy_runtimes_action_policy_owner"),
        ForeignKeyConstraint(["risk_policy_id", "owner_id"], ["risk_policies.id", "risk_policies.owner_id"], name="fk_strategy_runtimes_risk_policy_owner"),
    )


class OrderIntent(Base):
    __tablename__ = "order_intents"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    runtime_id = Column(String(36), nullable=False, index=True)
    action_mapping_id = Column(String(50), nullable=False)
    requested_instrument_id = Column(String(255), nullable=False)
    resolved_instrument_id = Column(String(255), nullable=False)
    intent_type = Column(String(20), nullable=False)
    reduce_only = Column(Boolean, nullable=False, default=False)
    side = Column(String(10), nullable=False)
    quantity_units = Column(BigInteger, nullable=False)
    order_type = Column(String(10), nullable=False)
    limit_price_units = Column(BigInteger, nullable=True)
    time_in_force = Column(String(10), nullable=False)
    source_candle_timestamp = Column(UTCDateTime, nullable=False)
    source_evaluation_fingerprint = Column(String(64), nullable=False)
    trigger_event_key = Column(String(64), nullable=False)
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))

    __table_args__ = (
        CheckConstraint("intent_type IN ('ENTRY', 'EXIT', 'REDUCE', 'REVERSE')", name="ck_order_intents_type"),
        CheckConstraint("side IN ('BUY', 'SELL')", name="ck_order_intents_side"),
        CheckConstraint("quantity_units > 0", name="ck_order_intents_qty_pos"),
        CheckConstraint("order_type IN ('MARKET', 'LIMIT')", name="ck_order_intents_order_type"),
        CheckConstraint("time_in_force IN ('DAY', 'GTC', 'IOC')", name="ck_order_intents_tif"),
        CheckConstraint("order_type != 'LIMIT' OR (limit_price_units IS NOT NULL AND limit_price_units > 0)", name="ck_order_intents_limit_price_pos"),
        CheckConstraint("order_type != 'MARKET' OR limit_price_units IS NULL", name="ck_order_intents_market_no_price"),
        UniqueConstraint("runtime_id", "trigger_event_key", name="uq_order_intents_trigger_event"),
        UniqueConstraint("id", "owner_id", name="uq_order_intents_id_owner"),
        ForeignKeyConstraint(["runtime_id", "owner_id"], ["strategy_runtimes.id", "strategy_runtimes.owner_id"], name="fk_order_intents_runtime_owner"),
    )


class Order(Base):
    __tablename__ = "orders"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    runtime_id = Column(String(36), nullable=False, index=True)
    intent_id = Column(String(36), nullable=False, unique=True, index=True)
    account_id = Column(String(36), nullable=False, index=True)
    order_sequence_number = Column(BigInteger, nullable=False)
    instrument_id = Column(String(255), nullable=False)
    side = Column(String(10), nullable=False)
    order_type = Column(String(10), nullable=False)
    quantity_units = Column(BigInteger, nullable=False)
    limit_price_units = Column(BigInteger, nullable=True)
    filled_quantity_units = Column(BigInteger, nullable=False, default=0)
    status = Column(String(30), nullable=False, default="CREATED")
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))
    updated_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc), onupdate=lambda: datetime.datetime.now(datetime.timezone.utc))

    __table_args__ = (
        CheckConstraint("side IN ('BUY', 'SELL')", name="ck_orders_side"),
        CheckConstraint("order_type IN ('MARKET', 'LIMIT')", name="ck_orders_order_type"),
        CheckConstraint("quantity_units > 0", name="ck_orders_qty_pos"),
        CheckConstraint("order_sequence_number > 0", name="ck_orders_seq_pos"),
        CheckConstraint("filled_quantity_units >= 0 AND filled_quantity_units <= quantity_units", name="ck_orders_filled_bounds"),
        CheckConstraint("status IN ('CREATED', 'ACCEPTED', 'PENDING_SUBMISSION', 'ACKNOWLEDGED', 'PARTIALLY_FILLED', 'FILLED', 'CANCEL_PENDING', 'CANCELLED', 'REJECTED', 'PROVIDER_REJECTED', 'RECONCILIATION_REQUIRED', 'EXPIRED', 'RISK_REJECTED', 'ERROR')", name="ck_orders_status"),
        CheckConstraint("order_type != 'LIMIT' OR (limit_price_units IS NOT NULL AND limit_price_units > 0)", name="ck_orders_limit_requires_price"),
        CheckConstraint("order_type != 'MARKET' OR limit_price_units IS NULL", name="ck_orders_market_forbids_price"),
        CheckConstraint("version > 0", name="ck_orders_version_pos"),
        UniqueConstraint("runtime_id", "order_sequence_number", name="uq_orders_runtime_seq"),
        UniqueConstraint("id", "owner_id", name="uq_orders_id_owner"),
        UniqueConstraint("owner_id", "id", name="uq_orders_owner_id"),
        ForeignKeyConstraint(["runtime_id", "owner_id"], ["strategy_runtimes.id", "strategy_runtimes.owner_id"], name="fk_orders_runtime_owner"),
        ForeignKeyConstraint(["account_id", "owner_id"], ["paper_accounts.id", "paper_accounts.owner_id"], name="fk_orders_account_owner"),
        ForeignKeyConstraint(["intent_id", "owner_id"], ["order_intents.id", "order_intents.owner_id"], name="fk_orders_intent_owner"),
    )


class OrderEvent(Base):
    __tablename__ = "order_events"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    order_id = Column(String(36), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True)
    sequence_number = Column(Integer, nullable=False)
    previous_status = Column(String(20), nullable=False)
    new_status = Column(String(20), nullable=False)
    actor = Column(String(50), nullable=False)
    reason_code = Column(String(50), nullable=False)
    metadata_json = Column(JSON, nullable=True)
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))

    __table_args__ = (
        CheckConstraint("sequence_number > 0", name="ck_order_events_seq_pos"),
        UniqueConstraint("order_id", "sequence_number", name="uq_order_events_order_seq"),
    )


class Fill(Base):
    __tablename__ = "fills"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    order_id = Column(String(36), nullable=False, index=True)
    account_id = Column(String(36), nullable=False, index=True)
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    instrument_id = Column(String(255), nullable=False)
    side = Column(String(10), nullable=False)
    quantity_units = Column(BigInteger, nullable=False)
    price_units = Column(BigInteger, nullable=False)
    fee_units = Column(BigInteger, nullable=False, default=0)
    candle_timestamp = Column(UTCDateTime, nullable=False)
    fill_idempotency_key = Column(String(64), nullable=False, unique=True, index=True)
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))

    __table_args__ = (
        CheckConstraint("quantity_units > 0", name="ck_fills_qty_pos"),
        CheckConstraint("price_units > 0", name="ck_fills_price_pos"),
        CheckConstraint("fee_units >= 0", name="ck_fills_fee_nonneg"),
        UniqueConstraint("id", "owner_id", name="uq_fills_id_owner"),
        ForeignKeyConstraint(["order_id", "owner_id"], ["orders.id", "orders.owner_id"], name="fk_fills_order_owner"),
        ForeignKeyConstraint(["account_id", "owner_id"], ["paper_accounts.id", "paper_accounts.owner_id"], name="fk_fills_account_owner"),
    )


class PaperPosition(Base):
    __tablename__ = "paper_positions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    account_id = Column(String(36), nullable=False, index=True)
    instrument_id = Column(String(255), nullable=False)
    net_quantity_units = Column(BigInteger, nullable=False, default=0)
    average_entry_price_units = Column(BigInteger, nullable=False, default=0)
    cost_basis_units = Column(BigInteger, nullable=False, default=0)
    gross_realized_pnl_units = Column(BigInteger, nullable=False, default=0)
    total_fees_units = Column(BigInteger, nullable=False, default=0)
    net_realized_pnl_units = Column(BigInteger, nullable=False, default=0)
    last_mark_price_units = Column(BigInteger, nullable=False, default=0)
    unrealized_pnl_units = Column(BigInteger, nullable=False, default=0)
    updated_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc), onupdate=lambda: datetime.datetime.now(datetime.timezone.utc))

    __table_args__ = (
        CheckConstraint("cost_basis_units >= 0", name="ck_paper_positions_cost_basis_nonneg"),
        CheckConstraint("total_fees_units >= 0", name="ck_paper_positions_total_fees_nonneg"),
        UniqueConstraint("account_id", "instrument_id", name="uq_paper_positions_account_instrument"),
        ForeignKeyConstraint(["account_id", "owner_id"], ["paper_accounts.id", "paper_accounts.owner_id"], name="fk_paper_positions_account_owner"),
    )


class KillSwitch(Base):
    __tablename__ = "kill_switches"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    target_key = Column(String(50), nullable=False, unique=True, index=True)
    scope = Column(String(20), nullable=False)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    is_active = Column(Boolean, nullable=False, default=False)
    engaged_by = Column(String(36), ForeignKey("users.id"), nullable=True)
    engaged_at = Column(UTCDateTime, nullable=True)
    reason = Column(String(500), nullable=True)

    __table_args__ = (
        CheckConstraint("scope IN ('GLOBAL', 'USER')", name="ck_kill_switches_scope"),
        CheckConstraint("scope != 'GLOBAL' OR user_id IS NULL", name="ck_kill_switches_global_no_user"),
        CheckConstraint("scope != 'USER' OR user_id IS NOT NULL", name="ck_kill_switches_user_requires_user"),
        UniqueConstraint("target_key", name="uq_kill_switches_target_key"),
        UniqueConstraint("scope", "user_id", name="uq_kill_switches_scope_user"),
    )


class ApiIdempotencyRecord(Base):
    __tablename__ = "api_idempotency_records"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    key = Column(String(64), nullable=False)
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    request_hash = Column(String(64), nullable=False)
    response_status = Column(Integer, nullable=False)
    response_body = Column(JSON, nullable=False)
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))

    __table_args__ = (
        UniqueConstraint("owner_id", "key", name="uq_api_idempotency_owner_key"),
    )


class RuntimeEvent(Base):
    __tablename__ = "runtime_events"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    runtime_id = Column(String(36), nullable=False, index=True)
    sequence_number = Column(Integer, nullable=False)
    previous_status = Column(String(20), nullable=False)
    new_status = Column(String(20), nullable=False)
    actor = Column(String(50), nullable=False)
    reason_code = Column(String(50), nullable=False)
    metadata_json = Column(JSON, nullable=True)
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))

    __table_args__ = (
        CheckConstraint("sequence_number > 0", name="ck_runtime_events_seq_pos"),
        UniqueConstraint("runtime_id", "sequence_number", name="uq_runtime_events_runtime_seq"),
    )


class ActionDecision(Base):
    __tablename__ = "action_decisions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    runtime_id = Column(String(36), nullable=False, index=True)
    candle_timestamp = Column(UTCDateTime, nullable=False)
    action_mapping_id = Column(String(50), nullable=False)
    decision = Column(String(20), nullable=False)  # EXECUTED or IGNORED
    reason_code = Column(String(50), nullable=False)
    intent_id = Column(String(36), nullable=True)
    metadata_json = Column(JSON, nullable=True)
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))


class RiskDecision(Base):
    __tablename__ = "risk_decisions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    intent_id = Column(String(36), nullable=False, index=True)
    passed = Column(Boolean, nullable=False)
    reason_code = Column(String(50), nullable=False)
    message = Column(String(500), nullable=False)
    metrics_json = Column(JSON, nullable=True)
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))
    __table_args__ = (
        ForeignKeyConstraint(["owner_id", "intent_id"], ["order_intents.owner_id", "order_intents.id"], onupdate="RESTRICT", ondelete="RESTRICT", name="fk_risk_decisions_intent_owner"),
    )


class ProviderConnection(Base):
    __tablename__ = "provider_connections"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    provider_name = Column(String(50), nullable=False, default="UPSTOX")
    environment = Column(String(50), nullable=False, default="SANDBOX")
    credential_reference = Column(String(100), nullable=False)
    credential_version = Column(String(50), nullable=False, default="v1")
    status = Column(String(50), nullable=False, default="CONFIGURED")
    last_successful_transmission_at = Column(UTCDateTime, nullable=True)
    sanitized_error_code = Column(String(100), nullable=True)
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))
    updated_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc), onupdate=lambda: datetime.datetime.now(datetime.timezone.utc))

    __table_args__ = (
        UniqueConstraint("owner_id", "provider_name", "environment", name="uq_provider_conns_owner_prov_env"),
        CheckConstraint("environment = 'SANDBOX'", name="ck_provider_connections_env_sandbox"),
        CheckConstraint("provider_name = 'UPSTOX'", name="ck_provider_connections_provider_upstox"),
        CheckConstraint("status IN ('CONFIGURED', 'DISABLED', 'ERROR')", name="ck_provider_connections_status"),
    )


class ProviderInstrumentMapping(Base):
    __tablename__ = "provider_instrument_mappings"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    tradepro_instrument_id = Column(String(100), nullable=False, index=True)
    provider_instrument_token = Column(String(100), nullable=False)
    exchange = Column(String(20), nullable=False)
    segment = Column(String(20), nullable=False)
    symbol = Column(String(100), nullable=False)
    expiry_date = Column(UTCDateTime, nullable=True)
    strike_price_units = Column(BigInteger, nullable=True)
    option_type = Column(String(10), nullable=True)
    lot_size_units = Column(Integer, nullable=False, default=1)
    tick_size_units = Column(Integer, nullable=False, default=5)
    freeze_quantity_units = Column(Integer, nullable=False, default=1800)
    verification_status = Column(String(20), nullable=False, default="UNVERIFIED")
    verified_by = Column(String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True)
    verified_at = Column(UTCDateTime, nullable=True)
    verification_audit_json = Column(JSON, nullable=True)
    mapping_version = Column(Integer, nullable=False, default=1)
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))
    updated_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc), onupdate=lambda: datetime.datetime.now(datetime.timezone.utc))

    __table_args__ = (
        UniqueConstraint("owner_id", "tradepro_instrument_id", "mapping_version", name="uq_prov_inst_map_owner_inst_ver"),
        CheckConstraint("verification_status IN ('UNVERIFIED', 'VERIFIED', 'REJECTED', 'DISABLED')", name="ck_prov_inst_map_status"),
    )


class SubmissionOutbox(Base):
    __tablename__ = "submission_outbox"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    order_id = Column(String(36), nullable=False, index=True)
    action_type = Column(String(20), nullable=False)
    priority = Column(Integer, nullable=False, default=10, index=True)
    status = Column(String(30), nullable=False, default="PENDING")
    idempotency_key = Column(String(100), nullable=False)
    canonical_payload_hash = Column(String(64), nullable=False)
    payload_json = Column(JSON, nullable=False)
    claim_lease_until = Column(UTCDateTime, nullable=True, index=True)
    claimed_by = Column(String(100), nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=5)
    next_attempt_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc), index=True)
    last_error_code = Column(String(100), nullable=True)
    last_error_message = Column(String(500), nullable=True)
    transmission_started_at = Column(UTCDateTime, nullable=True)
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))
    updated_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc), onupdate=lambda: datetime.datetime.now(datetime.timezone.utc))

    __table_args__ = (
        ForeignKeyConstraint(["owner_id", "order_id"], ["orders.owner_id", "orders.id"], onupdate="RESTRICT", ondelete="RESTRICT", name="fk_submission_outbox_order_owner"),
        UniqueConstraint("owner_id", "idempotency_key", name="uq_submission_outbox_owner_idem"),
        UniqueConstraint("owner_id", "id", name="uq_submission_outbox_owner_resource"),
        CheckConstraint("action_type IN ('PLACE', 'CANCEL')", name="ck_submission_outbox_action_type"),
        CheckConstraint("(action_type = 'CANCEL' AND priority = 0) OR (action_type = 'PLACE' AND priority = 10)", name="ck_submission_outbox_priority_action"),
        CheckConstraint("status IN ('PENDING', 'CLAIMED', 'DELIVERED', 'RETRY_SCHEDULED', 'RECONCILIATION_REQUIRED', 'DEAD_LETTER')", name="ck_submission_outbox_status"),
    )


class ExternalOrderLink(Base):
    __tablename__ = "external_order_links"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    order_id = Column(String(36), nullable=False, index=True)
    provider_name = Column(String(50), nullable=False, default="UPSTOX")
    provider_order_id = Column(String(100), nullable=False, index=True)
    submitted_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))

    __table_args__ = (
        ForeignKeyConstraint(["owner_id", "order_id"], ["orders.owner_id", "orders.id"], onupdate="RESTRICT", ondelete="RESTRICT", name="fk_external_order_links_order_owner"),
        UniqueConstraint("owner_id", "provider_order_id", name="uq_ext_order_links_owner_prov_id"),
        CheckConstraint("provider_name = 'UPSTOX'", name="ck_external_order_links_provider"),
    )


class ReconciliationRecord(Base):
    __tablename__ = "reconciliation_records"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String(36), ForeignKey("users.id", onupdate="RESTRICT", ondelete="RESTRICT"), nullable=False, index=True)
    order_id = Column(String(36), nullable=False, index=True)
    outbox_id = Column(String(36), nullable=False, index=True)
    status = Column(String(10), nullable=False, server_default='OPEN')
    resolution_type = Column(String(30), nullable=True)
    resolved_by = Column(String(36), ForeignKey("users.id", onupdate="RESTRICT", ondelete="RESTRICT"), nullable=True)
    provider_order_reference = Column(String(100), nullable=True)
    notes = Column(String(1000), nullable=True)
    resolved_at = Column(UTCDateTime(), nullable=True)
    created_at = Column(UTCDateTime(), nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))

    __table_args__ = (
        CheckConstraint("status IN ('OPEN', 'RESOLVED')", name="ck_reconciliation_records_status"),
        CheckConstraint("resolution_type IN ('PLACE_CONFIRMED', 'PLACE_REJECTED', 'CANCEL_CONFIRMED', 'CANCEL_NOT_CONFIRMED')", name="ck_reconciliation_records_resolution_type"),
        CheckConstraint(
            "(status = 'OPEN' AND resolution_type IS NULL AND resolved_by IS NULL AND resolved_at IS NULL) OR "
            "(status = 'RESOLVED' AND resolution_type IS NOT NULL AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL)",
            name="ck_reconciliation_records_lifecycle"
        ),
        ForeignKeyConstraint(["owner_id", "order_id"], ["orders.owner_id", "orders.id"], onupdate="RESTRICT", ondelete="RESTRICT", name="fk_reconciliation_records_order_owner"),
        ForeignKeyConstraint(["owner_id", "outbox_id"], ["submission_outbox.owner_id", "submission_outbox.id"], onupdate="RESTRICT", ondelete="RESTRICT", name="fk_reconciliation_records_outbox_owner"),
        UniqueConstraint("owner_id", "outbox_id", name="uq_reconciliation_records_owner_outbox"),
    )


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    worker_id = Column(String(100), nullable=False, unique=True, index=True)
    owner_id = Column(String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True)
    provider_name = Column(String(50), nullable=False, default="UPSTOX")
    status = Column(String(20), nullable=False, default="HEALTHY")
    last_heartbeat_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))
    batch_count = Column(Integer, nullable=False, default=0)
    processed_count = Column(Integer, nullable=False, default=0)
    error_count = Column(Integer, nullable=False, default=0)
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc))
    updated_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc), onupdate=lambda: datetime.datetime.now(datetime.timezone.utc))

    __table_args__ = (
        CheckConstraint("status IN ('HEALTHY', 'STOPPED', 'ERROR')", name="ck_worker_heartbeats_status"),
    )


from src.engine.orchestration.storage import ExactInteger


class RuntimeOrchestrationConfig(Base):
    __tablename__ = "runtime_orchestration_configs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String(36), nullable=False)
    runtime_id = Column(String(36), nullable=False)
    source_type = Column(String(30), nullable=False)
    source_namespace = Column(String(100), nullable=False)
    execution_policy = Column(String(30), nullable=False)
    snapshot_fingerprint = Column(String(64), nullable=False)
    snapshot_json = Column(Text, nullable=False)
    consent_at = Column(UTCDateTime, nullable=False)
    consent_policy_version = Column(String(30), nullable=False)
    consent_fingerprint = Column(String(64), nullable=False)
    source_policy_version = Column(String(30), nullable=False)
    alignment_offset_seconds = Column(ExactInteger(), nullable=False)
    timeframe = Column(String(3), nullable=False)
    replay_open_at = Column(UTCDateTime, nullable=False)
    replay_close_at = Column(UTCDateTime, nullable=False)
    checkpoint_close_at = Column(UTCDateTime, nullable=True)
    lease_owner = Column(String(100), nullable=True)
    lease_expires_at = Column(UTCDateTime, nullable=True)
    fencing_generation = Column(ExactInteger(), nullable=False, server_default=text("1"))
    retry_count = Column(ExactInteger(), nullable=False, server_default=text("0"))
    next_attempt_at = Column(UTCDateTime, nullable=True)
    last_reason_code = Column(String(64), nullable=True)
    created_at = Column(UTCDateTime, nullable=False)
    updated_at = Column(UTCDateTime, nullable=False)

    __table_args__ = (
        CheckConstraint("typeof(alignment_offset_seconds) = 'integer' AND typeof(fencing_generation) = 'integer' AND typeof(retry_count) = 'integer'", name="ck_orch_config_storage").ddl_if(dialect="sqlite"),
        UniqueConstraint("runtime_id", name="uq_orch_config_runtime"),
        UniqueConstraint("owner_id", "runtime_id", name="uq_orch_config_owner_runtime"),
        UniqueConstraint("owner_id", "runtime_id", "id", "snapshot_fingerprint", "timeframe", name="uq_orch_config_identity"),
        UniqueConstraint("owner_id", "runtime_id", "source_namespace", "timeframe", "source_policy_version", "alignment_offset_seconds", name="uq_orch_config_source"),
        ForeignKeyConstraint(["owner_id", "runtime_id"], ["strategy_runtimes.owner_id", "strategy_runtimes.id"], onupdate="RESTRICT", ondelete="RESTRICT", name="fk_orch_config_runtime"),
        ForeignKeyConstraint(["owner_id"], ["users.id"], onupdate="RESTRICT", ondelete="RESTRICT", name="fk_orch_config_confirming_user"),
        CheckConstraint("source_policy_version = 'packaged_alignment_v1' AND alignment_offset_seconds >= 0 AND alignment_offset_seconds < CASE timeframe WHEN '5m' THEN 300 ELSE 900 END AND alignment_offset_seconds = CAST(alignment_offset_seconds AS INTEGER)", name="ck_orch_config_alignment"),
        CheckConstraint("source_type = 'FIXTURE_REPLAY'", name="ck_orch_config_source"),
        CheckConstraint("execution_policy = 'INTERNAL_MOCK_ONLY'", name="ck_orch_config_execution"),
        CheckConstraint("timeframe IN ('5m', '15m')", name="ck_orch_config_timeframe"),
        CheckConstraint("length(id) BETWEEN 1 AND 36 AND length(owner_id) BETWEEN 1 AND 36 AND length(runtime_id) BETWEEN 1 AND 36", name="ck_orch_config_ids"),
        CheckConstraint("length(source_namespace) BETWEEN 1 AND 100", name="ck_orch_config_namespace"),
        CheckConstraint("length(snapshot_fingerprint) = 64", name="ck_orch_config_fingerprint"),
        CheckConstraint("length(snapshot_json) BETWEEN 2 AND 262144", name="ck_orch_config_snapshot"),
        CheckConstraint("consent_policy_version = 'fixture_consent_v1' AND length(consent_fingerprint) = 64", name="ck_orch_config_consent"),
        CheckConstraint("replay_close_at > replay_open_at", name="ck_orch_config_replay"),
        CheckConstraint("checkpoint_close_at IS NULL OR (checkpoint_close_at > replay_open_at AND checkpoint_close_at <= replay_close_at)", name="ck_orch_config_checkpoint"),
        CheckConstraint("(lease_owner IS NULL AND lease_expires_at IS NULL) OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL AND length(lease_owner) BETWEEN 1 AND 100)", name="ck_orch_config_lease"),
        CheckConstraint("fencing_generation BETWEEN 1 AND 9223372036854775807 AND fencing_generation = CAST(fencing_generation AS BIGINT)", name="ck_orch_config_generation"),
        CheckConstraint("retry_count BETWEEN 0 AND 100 AND retry_count = CAST(retry_count AS INTEGER)", name="ck_orch_config_retries"),
        CheckConstraint("last_reason_code IS NULL OR length(last_reason_code) BETWEEN 1 AND 64", name="ck_orch_config_reason"),
        CheckConstraint("updated_at >= created_at AND consent_at <= created_at", name="ck_orch_config_times"),
        Index("ix_orch_config_retry", "next_attempt_at", "runtime_id"),
        Index("ix_orch_config_lease", "lease_expires_at"),
    )


class CompletedCandleEvent(Base):
    __tablename__ = "completed_candle_events"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String(36), nullable=False)
    runtime_id = Column(String(36), nullable=False)
    source_type = Column(String(30), nullable=False)
    source_namespace = Column(String(100), nullable=False)
    source_event_id = Column(String(100), nullable=False)
    dataset_id = Column(String(100), nullable=False)
    dataset_checksum = Column(String(64), nullable=False)
    instrument_id = Column(String(100), nullable=False)
    timeframe = Column(String(3), nullable=False)
    series_role = Column(String(10), nullable=False)
    source_policy_version = Column(String(30), nullable=False)
    alignment_offset_seconds = Column(ExactInteger(), nullable=False)
    open_at = Column(UTCDateTime, nullable=False)
    close_at = Column(UTCDateTime, nullable=False)
    received_at = Column(UTCDateTime, nullable=False)
    price_scale = Column(ExactInteger(), nullable=False)
    volume_scale = Column(ExactInteger(), nullable=False)
    open_units = Column(ExactInteger(), nullable=False)
    high_units = Column(ExactInteger(), nullable=False)
    low_units = Column(ExactInteger(), nullable=False)
    close_units = Column(ExactInteger(), nullable=False)
    volume_units = Column(ExactInteger(), nullable=False)
    is_closed = Column(Boolean, nullable=False)
    revision = Column(ExactInteger(), nullable=False)
    content_fingerprint = Column(String(64), nullable=False)

    __table_args__ = (
        CheckConstraint("typeof(alignment_offset_seconds) = 'integer' AND typeof(price_scale) = 'integer' AND typeof(volume_scale) = 'integer' AND typeof(open_units) = 'integer' AND typeof(high_units) = 'integer' AND typeof(low_units) = 'integer' AND typeof(close_units) = 'integer' AND typeof(volume_units) = 'integer' AND typeof(revision) = 'integer'", name="ck_orch_candle_storage").ddl_if(dialect="sqlite"),
        UniqueConstraint("owner_id", "runtime_id", "id", name="uq_orch_candle_resource"),
        UniqueConstraint("owner_id", "runtime_id", "content_fingerprint", name="uq_orch_candle_content"),
        UniqueConstraint("owner_id", "runtime_id", "source_namespace", "series_role", "dataset_id", "source_event_id", name="uq_orch_candle_event"),
        UniqueConstraint("owner_id", "runtime_id", "series_role", "instrument_id", "timeframe", "close_at", name="uq_orch_candle_interval"),
        ForeignKeyConstraint(["owner_id", "runtime_id", "source_namespace", "timeframe", "source_policy_version", "alignment_offset_seconds"], ["runtime_orchestration_configs.owner_id", "runtime_orchestration_configs.runtime_id", "runtime_orchestration_configs.source_namespace", "runtime_orchestration_configs.timeframe", "runtime_orchestration_configs.source_policy_version", "runtime_orchestration_configs.alignment_offset_seconds"], onupdate="RESTRICT", ondelete="RESTRICT", name="fk_orch_candle_config"),
        CheckConstraint("source_type = 'FIXTURE_REPLAY'", name="ck_orch_candle_source"),
        CheckConstraint("series_role IN ('REFERENCE', 'SUBJECT')", name="ck_orch_candle_role"),
        CheckConstraint("source_policy_version = 'packaged_alignment_v1' AND alignment_offset_seconds >= 0 AND alignment_offset_seconds < CASE timeframe WHEN '5m' THEN 300 ELSE 900 END AND alignment_offset_seconds = CAST(alignment_offset_seconds AS INTEGER)", name="ck_orch_candle_alignment"),
        CheckConstraint("timeframe IN ('5m', '15m')", name="ck_orch_candle_timeframe"),
        CheckConstraint("revision = 1 AND is_closed IS TRUE", name="ck_orch_candle_final"),
        CheckConstraint("length(id) BETWEEN 1 AND 36 AND length(owner_id) BETWEEN 1 AND 36 AND length(runtime_id) BETWEEN 1 AND 36", name="ck_orch_candle_ids"),
        CheckConstraint("length(source_namespace) BETWEEN 1 AND 100 AND length(source_event_id) BETWEEN 1 AND 100 AND length(dataset_id) BETWEEN 1 AND 100 AND length(instrument_id) BETWEEN 1 AND 100", name="ck_orch_candle_identifiers"),
        CheckConstraint("length(dataset_checksum) = 64 AND length(content_fingerprint) = 64", name="ck_orch_candle_hashes"),
        CheckConstraint("close_at > open_at AND received_at >= close_at", name="ck_orch_candle_times"),
        CheckConstraint("price_scale BETWEEN 0 AND 8 AND volume_scale BETWEEN 0 AND 8 AND price_scale = CAST(price_scale AS INTEGER) AND volume_scale = CAST(volume_scale AS INTEGER)", name="ck_orch_candle_scales"),
        CheckConstraint("open_units BETWEEN 1 AND 9000000000000000 AND high_units BETWEEN 1 AND 9000000000000000 AND low_units BETWEEN 1 AND 9000000000000000 AND close_units BETWEEN 1 AND 9000000000000000 AND volume_units BETWEEN 0 AND 9000000000000000 AND open_units = CAST(open_units AS BIGINT) AND high_units = CAST(high_units AS BIGINT) AND low_units = CAST(low_units AS BIGINT) AND close_units = CAST(close_units AS BIGINT) AND volume_units = CAST(volume_units AS BIGINT)", name="ck_orch_candle_units"),
        CheckConstraint("high_units >= open_units AND high_units >= close_units AND high_units >= low_units AND low_units <= open_units AND low_units <= close_units", name="ck_orch_candle_geometry"),
        Index("ix_orch_candle_series", "owner_id", "runtime_id", "series_role", "close_at"),
    )


class RuntimeEvaluation(Base):
    __tablename__ = "runtime_evaluations"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String(36), nullable=False)
    runtime_id = Column(String(36), nullable=False)
    config_id = Column(String(36), nullable=False)
    snapshot_fingerprint = Column(String(64), nullable=False)
    evaluation_fingerprint = Column(String(64), nullable=False)
    timeframe = Column(String(3), nullable=False)
    close_at = Column(UTCDateTime, nullable=False)
    reference_candle_id = Column(String(36), nullable=False)
    subject_candle_id = Column(String(36), nullable=True)
    required_candles_json = Column(Text, nullable=False)
    evaluation_status = Column(String(20), nullable=False)
    action_outcome = Column(String(30), nullable=False)
    risk_outcome = Column(String(20), nullable=False)
    no_order_reason = Column(String(64), nullable=True)
    audit_json = Column(Text, nullable=False)
    risk_summary_json = Column(Text, nullable=False)
    finalized_at = Column(UTCDateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("owner_id", "runtime_id", "id", name="uq_orch_eval_resource"),
        UniqueConstraint("evaluation_fingerprint", name="uq_orch_eval_fingerprint"),
        UniqueConstraint("owner_id", "runtime_id", "timeframe", "close_at", name="uq_orch_eval_interval"),
        ForeignKeyConstraint(["owner_id", "runtime_id", "config_id", "snapshot_fingerprint", "timeframe"], ["runtime_orchestration_configs.owner_id", "runtime_orchestration_configs.runtime_id", "runtime_orchestration_configs.id", "runtime_orchestration_configs.snapshot_fingerprint", "runtime_orchestration_configs.timeframe"], onupdate="RESTRICT", ondelete="RESTRICT", name="fk_orch_eval_config"),
        ForeignKeyConstraint(["owner_id", "runtime_id", "reference_candle_id"], ["completed_candle_events.owner_id", "completed_candle_events.runtime_id", "completed_candle_events.id"], onupdate="RESTRICT", ondelete="RESTRICT", name="fk_orch_eval_reference"),
        ForeignKeyConstraint(["owner_id", "runtime_id", "subject_candle_id"], ["completed_candle_events.owner_id", "completed_candle_events.runtime_id", "completed_candle_events.id"], onupdate="RESTRICT", ondelete="RESTRICT", name="fk_orch_eval_subject"),
        CheckConstraint("length(id) BETWEEN 1 AND 36 AND length(owner_id) BETWEEN 1 AND 36 AND length(runtime_id) BETWEEN 1 AND 36 AND length(config_id) BETWEEN 1 AND 36", name="ck_orch_eval_ids"),
        CheckConstraint("length(snapshot_fingerprint) = 64 AND length(evaluation_fingerprint) = 64", name="ck_orch_eval_hashes"),
        CheckConstraint("timeframe IN ('5m', '15m')", name="ck_orch_eval_timeframe"),
        CheckConstraint("evaluation_status IN ('TRUE', 'FALSE', 'UNAVAILABLE', 'INVALID')", name="ck_orch_eval_status"),
        CheckConstraint("action_outcome IN ('NO_ACTION', 'REJECTED', 'ACCEPTED_INTERNAL')", name="ck_orch_eval_action"),
        CheckConstraint("risk_outcome IN ('NOT_RUN', 'REJECTED', 'ACCEPTED')", name="ck_orch_eval_risk"),
        CheckConstraint("(action_outcome = 'ACCEPTED_INTERNAL' AND risk_outcome = 'ACCEPTED' AND no_order_reason IS NULL AND evaluation_status IN ('TRUE', 'FALSE')) OR (action_outcome != 'ACCEPTED_INTERNAL' AND no_order_reason IS NOT NULL AND length(no_order_reason) BETWEEN 1 AND 64)", name="ck_orch_eval_outcome"),
        CheckConstraint("length(required_candles_json) BETWEEN 2 AND 2048 AND length(audit_json) BETWEEN 2 AND 65536 AND length(risk_summary_json) BETWEEN 2 AND 65536", name="ck_orch_eval_evidence"),
        CheckConstraint("finalized_at >= close_at", name="ck_orch_eval_time"),
        Index("ix_orch_eval_history", "owner_id", "runtime_id", "close_at"),
    )


_ORCHESTRATION_MUTABLE_FIELDS = frozenset({
    "checkpoint_close_at", "lease_owner", "lease_expires_at", "fencing_generation",
    "retry_count", "next_attempt_at", "last_reason_code", "updated_at",
})


@event.listens_for(RuntimeOrchestrationConfig, "before_update")
def _guard_orchestration_config(mapper, connection, target):
    state = inspect(target)
    table = RuntimeOrchestrationConfig.__table__
    previous = connection.execute(table.select().where(table.c.id == state.identity[0])).mappings().one()
    for attribute in state.attrs:
        if (attribute.history.has_changes() and attribute.key not in _ORCHESTRATION_MUTABLE_FIELDS
                and getattr(target, attribute.key) != previous[attribute.key]):
            raise ValueError(f"Immutable orchestration configuration field: {attribute.key}")
    # Read persisted values even when SQLAlchemy expired an attribute after
    # commit; history.deleted alone does not protect unloaded attributes.
    for field in ("fencing_generation", "checkpoint_close_at"):
        if previous[field] is not None:
            if getattr(target, field) is None or getattr(target, field) < previous[field]:
                raise ValueError(f"Orchestration {field} cannot move backwards")


@event.listens_for(CompletedCandleEvent, "before_update")
@event.listens_for(RuntimeEvaluation, "before_update")
def _guard_orchestration_audit_update(mapper, connection, target):
    state = inspect(target)
    table = target.__table__
    previous = connection.execute(table.select().where(table.c.id == state.identity[0])).mappings().one()
    if any(attribute.history.has_changes() and getattr(target, attribute.key) != previous[attribute.key]
           for attribute in state.attrs):
        raise ValueError("Orchestration audit records are immutable")


@event.listens_for(CompletedCandleEvent, "before_delete")
@event.listens_for(RuntimeEvaluation, "before_delete")
@event.listens_for(RuntimeOrchestrationConfig, "before_delete")
def _guard_orchestration_audit_delete(mapper, connection, target):
    raise ValueError("Orchestration audit records cannot be deleted through the ORM")


@event.listens_for(RuntimeOrchestrationConfig, "before_insert")
def _validate_orchestration_config(mapper, connection, target):
    from src.engine.orchestration.models import OrchestrationSnapshot
    from src.engine.orchestration.fingerprint import canonical_json, orchestration_snapshot_v1
    from src.engine.orchestration.evidence import config_consent_fingerprint
    from src.engine.orchestration.source_policy import packaged_alignment
    from src.engine.manifest import get_dataset_entry
    if len(target.snapshot_json) > 262144:
        raise ValueError("Snapshot exceeds limit")
    snapshot = OrchestrationSnapshot.model_validate_json(target.snapshot_json)
    for dataset in snapshot.datasets:
        policy = packaged_alignment(dataset.dataset_id)
        entry = get_dataset_entry(dataset.dataset_id)
        if (policy.version, policy.timeframe, policy.offset_seconds) != (
                snapshot.source_policy_version, snapshot.timeframe, snapshot.alignment_offset_seconds):
            raise ValueError("Snapshot disagrees with approved source alignment")
        if (dataset.checksum, dataset.instrument_id, dataset.series_role.value) != (
                entry.dataset_checksum, entry.instrument_id, entry.category.value):
            raise ValueError("Snapshot disagrees with approved manifest provenance")
    for field in ("owner_id", "runtime_id", "timeframe", "source_type", "source_namespace",
                  "source_policy_version", "alignment_offset_seconds", "execution_policy",
                  "replay_open_at", "replay_close_at"):
        if getattr(snapshot, field) != getattr(target, field):
            raise ValueError("Configuration disagrees with frozen snapshot")
    if orchestration_snapshot_v1(snapshot) != target.snapshot_fingerprint:
        raise ValueError("Snapshot fingerprint mismatch")
    if config_consent_fingerprint(target) != target.consent_fingerprint:
        raise ValueError("Consent fingerprint mismatch")
    target.snapshot_json = canonical_json(snapshot.model_dump(mode="python"))


@event.listens_for(CompletedCandleEvent, "before_insert")
def _validate_orchestration_candle(mapper, connection, target):
    from src.engine.orchestration.models import CompletedCandle
    payload = {field: getattr(target, field) for field in CompletedCandle.model_fields if field != "contract_version"}
    candle = CompletedCandle(**payload)
    if candle.content_fingerprint != target.content_fingerprint:
        raise ValueError("Candle fingerprint mismatch")


@event.listens_for(RuntimeEvaluation, "before_insert")
def _validate_orchestration_evidence(mapper, connection, target):
    import json
    from pydantic import TypeAdapter
    from src.engine.orchestration.models import RequiredCandleIdentity
    from src.engine.orchestration.fingerprint import canonical_json
    from src.engine.orchestration.evidence import evaluation_evidence
    if len(target.required_candles_json) > 2048:
        raise ValueError("Required candle evidence exceeds limit")
    items = TypeAdapter(list[RequiredCandleIdentity]).validate_python(json.loads(target.required_candles_json))
    if not 1 <= len(items) <= 2 or len({item.series_role for item in items}) != len(items):
        raise ValueError("Invalid required candle evidence")
    target.required_candles_json = canonical_json([item.model_dump(mode="python") for item in items])
    target.audit_json = evaluation_evidence(target.audit_json)
    target.risk_summary_json = evaluation_evidence(target.risk_summary_json, risk=True)


@event.listens_for(RuntimeOrchestrationConfig, "before_insert")
@event.listens_for(RuntimeOrchestrationConfig, "before_update")
@event.listens_for(RuntimeEvaluation, "before_insert")
def _validate_orchestration_codes(mapper, connection, target):
    import re
    for field in ("lease_owner", "last_reason_code", "no_order_reason"):
        value = getattr(target, field, None)
        if value is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", value):
            raise ValueError("Invalid internal evidence code")
