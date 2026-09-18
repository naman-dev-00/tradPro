"""Frozen Phase 1 schema. Revision 0006_strategy_orchestrator."""
from alembic import op
import sqlalchemy as sa
from src.database import UTCDateTime
from src.engine.orchestration.storage import ExactInteger

revision = "0006_strategy_orchestrator"
down_revision = "0005_upstox_sandbox"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('runtime_orchestration_configs',
        sa.Column('id', sa.String(length=36), nullable=False, primary_key=True),
        sa.Column('owner_id', sa.String(length=36), nullable=False),
        sa.Column('runtime_id', sa.String(length=36), nullable=False),
        sa.Column('source_type', sa.String(length=30), nullable=False),
        sa.Column('source_namespace', sa.String(length=100), nullable=False),
        sa.Column('execution_policy', sa.String(length=30), nullable=False),
        sa.Column('snapshot_fingerprint', sa.String(length=64), nullable=False),
        sa.Column('snapshot_json', sa.Text(), nullable=False),
        sa.Column('consent_at', UTCDateTime(), nullable=False),
        sa.Column('consent_policy_version', sa.String(length=30), nullable=False),
        sa.Column('consent_fingerprint', sa.String(length=64), nullable=False),
        sa.Column('source_policy_version', sa.String(length=30), nullable=False),
        sa.Column('alignment_offset_seconds', ExactInteger(), nullable=False),
        sa.Column('timeframe', sa.String(length=3), nullable=False),
        sa.Column('replay_open_at', UTCDateTime(), nullable=False),
        sa.Column('replay_close_at', UTCDateTime(), nullable=False),
        sa.Column('checkpoint_close_at', UTCDateTime(), nullable=True),
        sa.Column('lease_owner', sa.String(length=100), nullable=True),
        sa.Column('lease_expires_at', UTCDateTime(), nullable=True),
        sa.Column('fencing_generation', ExactInteger(), nullable=False, server_default=sa.text('1')),
        sa.Column('retry_count', ExactInteger(), nullable=False, server_default=sa.text('0')),
        sa.Column('next_attempt_at', UTCDateTime(), nullable=True),
        sa.Column('last_reason_code', sa.String(length=64), nullable=True),
        sa.Column('created_at', UTCDateTime(), nullable=False),
        sa.Column('updated_at', UTCDateTime(), nullable=False),
        sa.CheckConstraint("source_policy_version = 'packaged_alignment_v1' AND alignment_offset_seconds >= 0 AND alignment_offset_seconds < CASE timeframe WHEN '5m' THEN 300 ELSE 900 END AND alignment_offset_seconds = CAST(alignment_offset_seconds AS INTEGER)", name='ck_orch_config_alignment'),
        sa.CheckConstraint('checkpoint_close_at IS NULL OR (checkpoint_close_at > replay_open_at AND checkpoint_close_at <= replay_close_at)', name='ck_orch_config_checkpoint'),
        sa.CheckConstraint("consent_policy_version = 'fixture_consent_v1' AND length(consent_fingerprint) = 64", name='ck_orch_config_consent'),
        sa.CheckConstraint("execution_policy = 'INTERNAL_MOCK_ONLY'", name='ck_orch_config_execution'),
        sa.CheckConstraint('length(snapshot_fingerprint) = 64', name='ck_orch_config_fingerprint'),
        sa.CheckConstraint('fencing_generation BETWEEN 1 AND 9223372036854775807 AND fencing_generation = CAST(fencing_generation AS BIGINT)', name='ck_orch_config_generation'),
        sa.CheckConstraint('length(id) BETWEEN 1 AND 36 AND length(owner_id) BETWEEN 1 AND 36 AND length(runtime_id) BETWEEN 1 AND 36', name='ck_orch_config_ids'),
        sa.CheckConstraint('(lease_owner IS NULL AND lease_expires_at IS NULL) OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL AND length(lease_owner) BETWEEN 1 AND 100)', name='ck_orch_config_lease'),
        sa.CheckConstraint('length(source_namespace) BETWEEN 1 AND 100', name='ck_orch_config_namespace'),
        sa.CheckConstraint('last_reason_code IS NULL OR length(last_reason_code) BETWEEN 1 AND 64', name='ck_orch_config_reason'),
        sa.CheckConstraint('replay_close_at > replay_open_at', name='ck_orch_config_replay'),
        sa.CheckConstraint('retry_count BETWEEN 0 AND 100 AND retry_count = CAST(retry_count AS INTEGER)', name='ck_orch_config_retries'),
        sa.CheckConstraint('length(snapshot_json) BETWEEN 2 AND 262144', name='ck_orch_config_snapshot'),
        sa.CheckConstraint("source_type = 'FIXTURE_REPLAY'", name='ck_orch_config_source'),
        sa.CheckConstraint("typeof(alignment_offset_seconds) = 'integer' AND typeof(fencing_generation) = 'integer' AND typeof(retry_count) = 'integer'", name='ck_orch_config_storage').ddl_if(dialect="sqlite"),
        sa.CheckConstraint("timeframe IN ('5m', '15m')", name='ck_orch_config_timeframe'),
        sa.CheckConstraint('updated_at >= created_at AND consent_at <= created_at', name='ck_orch_config_times'),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], onupdate="RESTRICT", ondelete="RESTRICT", name='fk_orch_config_confirming_user'),
        sa.ForeignKeyConstraint(['owner_id', 'runtime_id'], ['strategy_runtimes.owner_id', 'strategy_runtimes.id'], onupdate="RESTRICT", ondelete="RESTRICT", name='fk_orch_config_runtime'),
        sa.UniqueConstraint('owner_id', 'runtime_id', 'id', 'snapshot_fingerprint', 'timeframe', name='uq_orch_config_identity'),
        sa.UniqueConstraint('owner_id', 'runtime_id', name='uq_orch_config_owner_runtime'),
        sa.UniqueConstraint('runtime_id', name='uq_orch_config_runtime'),
        sa.UniqueConstraint('owner_id', 'runtime_id', 'source_namespace', 'timeframe', 'source_policy_version', 'alignment_offset_seconds', name='uq_orch_config_source'),
    )
    op.create_index('ix_orch_config_lease', 'runtime_orchestration_configs', ['lease_expires_at'])
    op.create_index('ix_orch_config_retry', 'runtime_orchestration_configs', ['next_attempt_at', 'runtime_id'])

    op.create_table('completed_candle_events',
        sa.Column('id', sa.String(length=36), nullable=False, primary_key=True),
        sa.Column('owner_id', sa.String(length=36), nullable=False),
        sa.Column('runtime_id', sa.String(length=36), nullable=False),
        sa.Column('source_type', sa.String(length=30), nullable=False),
        sa.Column('source_namespace', sa.String(length=100), nullable=False),
        sa.Column('source_event_id', sa.String(length=100), nullable=False),
        sa.Column('dataset_id', sa.String(length=100), nullable=False),
        sa.Column('dataset_checksum', sa.String(length=64), nullable=False),
        sa.Column('instrument_id', sa.String(length=100), nullable=False),
        sa.Column('timeframe', sa.String(length=3), nullable=False),
        sa.Column('series_role', sa.String(length=10), nullable=False),
        sa.Column('source_policy_version', sa.String(length=30), nullable=False),
        sa.Column('alignment_offset_seconds', ExactInteger(), nullable=False),
        sa.Column('open_at', UTCDateTime(), nullable=False),
        sa.Column('close_at', UTCDateTime(), nullable=False),
        sa.Column('received_at', UTCDateTime(), nullable=False),
        sa.Column('price_scale', ExactInteger(), nullable=False),
        sa.Column('volume_scale', ExactInteger(), nullable=False),
        sa.Column('open_units', ExactInteger(), nullable=False),
        sa.Column('high_units', ExactInteger(), nullable=False),
        sa.Column('low_units', ExactInteger(), nullable=False),
        sa.Column('close_units', ExactInteger(), nullable=False),
        sa.Column('volume_units', ExactInteger(), nullable=False),
        sa.Column('is_closed', sa.Boolean(), nullable=False),
        sa.Column('revision', ExactInteger(), nullable=False),
        sa.Column('content_fingerprint', sa.String(length=64), nullable=False),
        sa.CheckConstraint("source_policy_version = 'packaged_alignment_v1' AND alignment_offset_seconds >= 0 AND alignment_offset_seconds < CASE timeframe WHEN '5m' THEN 300 ELSE 900 END AND alignment_offset_seconds = CAST(alignment_offset_seconds AS INTEGER)", name='ck_orch_candle_alignment'),
        sa.CheckConstraint('revision = 1 AND is_closed IS TRUE', name='ck_orch_candle_final'),
        sa.CheckConstraint('high_units >= open_units AND high_units >= close_units AND high_units >= low_units AND low_units <= open_units AND low_units <= close_units', name='ck_orch_candle_geometry'),
        sa.CheckConstraint('length(dataset_checksum) = 64 AND length(content_fingerprint) = 64', name='ck_orch_candle_hashes'),
        sa.CheckConstraint('length(source_namespace) BETWEEN 1 AND 100 AND length(source_event_id) BETWEEN 1 AND 100 AND length(dataset_id) BETWEEN 1 AND 100 AND length(instrument_id) BETWEEN 1 AND 100', name='ck_orch_candle_identifiers'),
        sa.CheckConstraint('length(id) BETWEEN 1 AND 36 AND length(owner_id) BETWEEN 1 AND 36 AND length(runtime_id) BETWEEN 1 AND 36', name='ck_orch_candle_ids'),
        sa.CheckConstraint("series_role IN ('REFERENCE', 'SUBJECT')", name='ck_orch_candle_role'),
        sa.CheckConstraint('price_scale BETWEEN 0 AND 8 AND volume_scale BETWEEN 0 AND 8 AND price_scale = CAST(price_scale AS INTEGER) AND volume_scale = CAST(volume_scale AS INTEGER)', name='ck_orch_candle_scales'),
        sa.CheckConstraint("source_type = 'FIXTURE_REPLAY'", name='ck_orch_candle_source'),
        sa.CheckConstraint("typeof(alignment_offset_seconds) = 'integer' AND typeof(price_scale) = 'integer' AND typeof(volume_scale) = 'integer' AND typeof(open_units) = 'integer' AND typeof(high_units) = 'integer' AND typeof(low_units) = 'integer' AND typeof(close_units) = 'integer' AND typeof(volume_units) = 'integer' AND typeof(revision) = 'integer'", name='ck_orch_candle_storage').ddl_if(dialect="sqlite"),
        sa.CheckConstraint("timeframe IN ('5m', '15m')", name='ck_orch_candle_timeframe'),
        sa.CheckConstraint('close_at > open_at AND received_at >= close_at', name='ck_orch_candle_times'),
        sa.CheckConstraint('open_units BETWEEN 1 AND 9000000000000000 AND high_units BETWEEN 1 AND 9000000000000000 AND low_units BETWEEN 1 AND 9000000000000000 AND close_units BETWEEN 1 AND 9000000000000000 AND volume_units BETWEEN 0 AND 9000000000000000 AND open_units = CAST(open_units AS BIGINT) AND high_units = CAST(high_units AS BIGINT) AND low_units = CAST(low_units AS BIGINT) AND close_units = CAST(close_units AS BIGINT) AND volume_units = CAST(volume_units AS BIGINT)', name='ck_orch_candle_units'),
        sa.ForeignKeyConstraint(['owner_id', 'runtime_id', 'source_namespace', 'timeframe', 'source_policy_version', 'alignment_offset_seconds'], ['runtime_orchestration_configs.owner_id', 'runtime_orchestration_configs.runtime_id', 'runtime_orchestration_configs.source_namespace', 'runtime_orchestration_configs.timeframe', 'runtime_orchestration_configs.source_policy_version', 'runtime_orchestration_configs.alignment_offset_seconds'], onupdate="RESTRICT", ondelete="RESTRICT", name='fk_orch_candle_config'),
        sa.UniqueConstraint('owner_id', 'runtime_id', 'content_fingerprint', name='uq_orch_candle_content'),
        sa.UniqueConstraint('owner_id', 'runtime_id', 'source_namespace', 'series_role', 'dataset_id', 'source_event_id', name='uq_orch_candle_event'),
        sa.UniqueConstraint('owner_id', 'runtime_id', 'series_role', 'instrument_id', 'timeframe', 'close_at', name='uq_orch_candle_interval'),
        sa.UniqueConstraint('owner_id', 'runtime_id', 'id', name='uq_orch_candle_resource'),
    )
    op.create_index('ix_orch_candle_series', 'completed_candle_events', ['owner_id', 'runtime_id', 'series_role', 'close_at'])

    op.create_table('runtime_evaluations',
        sa.Column('id', sa.String(length=36), nullable=False, primary_key=True),
        sa.Column('owner_id', sa.String(length=36), nullable=False),
        sa.Column('runtime_id', sa.String(length=36), nullable=False),
        sa.Column('config_id', sa.String(length=36), nullable=False),
        sa.Column('snapshot_fingerprint', sa.String(length=64), nullable=False),
        sa.Column('evaluation_fingerprint', sa.String(length=64), nullable=False),
        sa.Column('timeframe', sa.String(length=3), nullable=False),
        sa.Column('close_at', UTCDateTime(), nullable=False),
        sa.Column('reference_candle_id', sa.String(length=36), nullable=False),
        sa.Column('subject_candle_id', sa.String(length=36), nullable=True),
        sa.Column('required_candles_json', sa.Text(), nullable=False),
        sa.Column('evaluation_status', sa.String(length=20), nullable=False),
        sa.Column('action_outcome', sa.String(length=30), nullable=False),
        sa.Column('risk_outcome', sa.String(length=20), nullable=False),
        sa.Column('no_order_reason', sa.String(length=64), nullable=True),
        sa.Column('audit_json', sa.Text(), nullable=False),
        sa.Column('risk_summary_json', sa.Text(), nullable=False),
        sa.Column('finalized_at', UTCDateTime(), nullable=False),
        sa.CheckConstraint("action_outcome IN ('NO_ACTION', 'REJECTED', 'ACCEPTED_INTERNAL')", name='ck_orch_eval_action'),
        sa.CheckConstraint('length(required_candles_json) BETWEEN 2 AND 2048 AND length(audit_json) BETWEEN 2 AND 65536 AND length(risk_summary_json) BETWEEN 2 AND 65536', name='ck_orch_eval_evidence'),
        sa.CheckConstraint('length(snapshot_fingerprint) = 64 AND length(evaluation_fingerprint) = 64', name='ck_orch_eval_hashes'),
        sa.CheckConstraint('length(id) BETWEEN 1 AND 36 AND length(owner_id) BETWEEN 1 AND 36 AND length(runtime_id) BETWEEN 1 AND 36 AND length(config_id) BETWEEN 1 AND 36', name='ck_orch_eval_ids'),
        sa.CheckConstraint("(action_outcome = 'ACCEPTED_INTERNAL' AND risk_outcome = 'ACCEPTED' AND no_order_reason IS NULL AND evaluation_status IN ('TRUE', 'FALSE')) OR (action_outcome != 'ACCEPTED_INTERNAL' AND no_order_reason IS NOT NULL AND length(no_order_reason) BETWEEN 1 AND 64)", name='ck_orch_eval_outcome'),
        sa.CheckConstraint("risk_outcome IN ('NOT_RUN', 'REJECTED', 'ACCEPTED')", name='ck_orch_eval_risk'),
        sa.CheckConstraint("evaluation_status IN ('TRUE', 'FALSE', 'UNAVAILABLE', 'INVALID')", name='ck_orch_eval_status'),
        sa.CheckConstraint('finalized_at >= close_at', name='ck_orch_eval_time'),
        sa.CheckConstraint("timeframe IN ('5m', '15m')", name='ck_orch_eval_timeframe'),
        sa.ForeignKeyConstraint(['owner_id', 'runtime_id', 'config_id', 'snapshot_fingerprint', 'timeframe'], ['runtime_orchestration_configs.owner_id', 'runtime_orchestration_configs.runtime_id', 'runtime_orchestration_configs.id', 'runtime_orchestration_configs.snapshot_fingerprint', 'runtime_orchestration_configs.timeframe'], onupdate="RESTRICT", ondelete="RESTRICT", name='fk_orch_eval_config'),
        sa.ForeignKeyConstraint(['owner_id', 'runtime_id', 'reference_candle_id'], ['completed_candle_events.owner_id', 'completed_candle_events.runtime_id', 'completed_candle_events.id'], onupdate="RESTRICT", ondelete="RESTRICT", name='fk_orch_eval_reference'),
        sa.ForeignKeyConstraint(['owner_id', 'runtime_id', 'subject_candle_id'], ['completed_candle_events.owner_id', 'completed_candle_events.runtime_id', 'completed_candle_events.id'], onupdate="RESTRICT", ondelete="RESTRICT", name='fk_orch_eval_subject'),
        sa.UniqueConstraint('evaluation_fingerprint', name='uq_orch_eval_fingerprint'),
        sa.UniqueConstraint('owner_id', 'runtime_id', 'timeframe', 'close_at', name='uq_orch_eval_interval'),
        sa.UniqueConstraint('owner_id', 'runtime_id', 'id', name='uq_orch_eval_resource'),
    )
    op.create_index('ix_orch_eval_history', 'runtime_evaluations', ['owner_id', 'runtime_id', 'close_at'])


def downgrade():
    bind = op.get_bind()
    tables = ("runtime_evaluations", "completed_candle_events", "runtime_orchestration_configs")
    # Refuse before any DDL, including when only a configuration exists.
    for table in tables:
        if bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar():
            raise RuntimeError(f"Downgrade refused: populated orchestration table {table}")
    for table in tables:
        op.drop_table(table)
