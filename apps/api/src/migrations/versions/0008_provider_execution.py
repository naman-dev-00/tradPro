"""Provider-driven execution, verified consent binding, and sandbox outbox dispatch.

Revision ID: 0008_provider_execution
Revises: 0007_paper_execution
Create Date: 2026-10-06

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from src.database import UTCDateTime

# revision identifiers, used by Alembic.
revision: str = "0008_provider_execution"
down_revision: Union[str, None] = "0007_paper_execution"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Update check constraints on runtime_orchestration_configs
    with op.batch_alter_table("runtime_orchestration_configs") as batch_op:
        batch_op.drop_constraint("ck_orch_config_source", type_="check")
        batch_op.drop_constraint("ck_orch_config_execution", type_="check")
        batch_op.drop_constraint("ck_orch_config_consent", type_="check")
        batch_op.drop_constraint("ck_orch_config_alignment", type_="check")

        batch_op.create_check_constraint(
            "ck_orch_config_source",
            "source_type IN ('FIXTURE_REPLAY', 'PROVIDER_SANDBOX', 'PROVIDER_UPSTOX_V3')",
        )
        batch_op.create_check_constraint(
            "ck_orch_config_execution",
            "execution_policy IN ('INTERNAL_MOCK_ONLY', 'INTERNAL_PAPER', 'EXTERNAL_SANDBOX_DISPATCH')",
        )
        batch_op.create_check_constraint(
            "ck_orch_config_consent",
            "(((execution_policy = 'INTERNAL_MOCK_ONLY' AND consent_policy_version = 'fixture_consent_v1') OR (execution_policy = 'INTERNAL_PAPER' AND consent_policy_version = 'fixture_paper_consent_v1') OR (execution_policy = 'EXTERNAL_SANDBOX_DISPATCH' AND consent_policy_version = 'sandbox_consent_v1')) AND length(consent_fingerprint) = 64)",
        )
        batch_op.create_check_constraint(
            "ck_orch_config_alignment",
            "source_policy_version IN ('packaged_alignment_v1', 'provider_completed_v1') AND alignment_offset_seconds >= 0 AND alignment_offset_seconds < CASE timeframe WHEN '5m' THEN 300 ELSE 900 END AND alignment_offset_seconds = CAST(alignment_offset_seconds AS INTEGER)",
        )

    # 2. Update check constraints on completed_candle_events
    with op.batch_alter_table("completed_candle_events") as batch_op:
        batch_op.drop_constraint("ck_orch_candle_source", type_="check")
        batch_op.drop_constraint("ck_orch_candle_alignment", type_="check")

        batch_op.create_check_constraint(
            "ck_orch_candle_source",
            "source_type IN ('FIXTURE_REPLAY', 'PROVIDER_SANDBOX', 'PROVIDER_UPSTOX_V3')",
        )
        batch_op.create_check_constraint(
            "ck_orch_candle_alignment",
            "source_policy_version IN ('packaged_alignment_v1', 'provider_completed_v1') AND alignment_offset_seconds >= 0 AND alignment_offset_seconds < CASE timeframe WHEN '5m' THEN 300 ELSE 900 END AND alignment_offset_seconds = CAST(alignment_offset_seconds AS INTEGER)",
        )

    # 3. Update check constraints on runtime_evaluations
    with op.batch_alter_table("runtime_evaluations") as batch_op:
        batch_op.drop_constraint("ck_orch_eval_action", type_="check")
        batch_op.drop_constraint("ck_orch_eval_outcome", type_="check")

        batch_op.create_check_constraint(
            "ck_orch_eval_action",
            "action_outcome IN ('NO_ACTION', 'REJECTED', 'ACCEPTED_INTERNAL', 'ACCEPTED_SANDBOX')",
        )
        batch_op.create_check_constraint(
            "ck_orch_eval_outcome",
            "(action_outcome IN ('ACCEPTED_INTERNAL', 'ACCEPTED_SANDBOX') AND risk_outcome = 'ACCEPTED' AND no_order_reason IS NULL AND evaluation_status IN ('TRUE', 'FALSE')) OR (action_outcome NOT IN ('ACCEPTED_INTERNAL', 'ACCEPTED_SANDBOX') AND no_order_reason IS NOT NULL AND length(no_order_reason) BETWEEN 1 AND 64)",
        )


def downgrade() -> None:
    conn = op.get_bind()

    # Guard 1: Cannot downgrade if runtime_orchestration_configs contains provider or sandbox records
    has_sandbox_configs = conn.execute(
        sa.text(
            "SELECT 1 FROM runtime_orchestration_configs WHERE execution_policy = 'EXTERNAL_SANDBOX_DISPATCH' OR source_type != 'FIXTURE_REPLAY' LIMIT 1"
        )
    ).fetchone()
    if has_sandbox_configs:
        raise RuntimeError("Cannot downgrade: runtime_orchestration_configs contains provider/sandbox records.")

    # Guard 2: Cannot downgrade if completed_candle_events contains non-fixture records
    has_provider_candles = conn.execute(
        sa.text("SELECT 1 FROM completed_candle_events WHERE source_type != 'FIXTURE_REPLAY' LIMIT 1")
    ).fetchone()
    if has_provider_candles:
        raise RuntimeError("Cannot downgrade: completed_candle_events contains non-fixture records.")

    # Guard 3: Cannot downgrade if runtime_evaluations contains ACCEPTED_SANDBOX records
    has_sandbox_evals = conn.execute(
        sa.text("SELECT 1 FROM runtime_evaluations WHERE action_outcome = 'ACCEPTED_SANDBOX' LIMIT 1")
    ).fetchone()
    if has_sandbox_evals:
        raise RuntimeError("Cannot downgrade: runtime_evaluations contains ACCEPTED_SANDBOX records.")

    # Restore runtime_orchestration_configs to 0007 state
    with op.batch_alter_table("runtime_orchestration_configs") as batch_op:
        batch_op.drop_constraint("ck_orch_config_source", type_="check")
        batch_op.drop_constraint("ck_orch_config_execution", type_="check")
        batch_op.drop_constraint("ck_orch_config_consent", type_="check")
        batch_op.drop_constraint("ck_orch_config_alignment", type_="check")

        batch_op.create_check_constraint(
            "ck_orch_config_source",
            "source_type = 'FIXTURE_REPLAY'",
        )
        batch_op.create_check_constraint(
            "ck_orch_config_execution",
            "execution_policy IN ('INTERNAL_MOCK_ONLY', 'INTERNAL_PAPER')",
        )
        batch_op.create_check_constraint(
            "ck_orch_config_consent",
            "(((execution_policy = 'INTERNAL_MOCK_ONLY' AND consent_policy_version = 'fixture_consent_v1') OR (execution_policy = 'INTERNAL_PAPER' AND consent_policy_version = 'fixture_paper_consent_v1')) AND length(consent_fingerprint) = 64)",
        )
        batch_op.create_check_constraint(
            "ck_orch_config_alignment",
            "source_policy_version = 'packaged_alignment_v1' AND alignment_offset_seconds >= 0 AND alignment_offset_seconds < CASE timeframe WHEN '5m' THEN 300 ELSE 900 END AND alignment_offset_seconds = CAST(alignment_offset_seconds AS INTEGER)",
        )

    # Restore completed_candle_events to 0006 state
    with op.batch_alter_table("completed_candle_events") as batch_op:
        batch_op.drop_constraint("ck_orch_candle_source", type_="check")
        batch_op.drop_constraint("ck_orch_candle_alignment", type_="check")

        batch_op.create_check_constraint(
            "ck_orch_candle_source",
            "source_type = 'FIXTURE_REPLAY'",
        )
        batch_op.create_check_constraint(
            "ck_orch_candle_alignment",
            "source_policy_version = 'packaged_alignment_v1' AND alignment_offset_seconds >= 0 AND alignment_offset_seconds < CASE timeframe WHEN '5m' THEN 300 ELSE 900 END AND alignment_offset_seconds = CAST(alignment_offset_seconds AS INTEGER)",
        )

    # Restore runtime_evaluations to 0006 state
    with op.batch_alter_table("runtime_evaluations") as batch_op:
        batch_op.drop_constraint("ck_orch_eval_action", type_="check")
        batch_op.drop_constraint("ck_orch_eval_outcome", type_="check")

        batch_op.create_check_constraint(
            "ck_orch_eval_action",
            "action_outcome IN ('NO_ACTION', 'REJECTED', 'ACCEPTED_INTERNAL')",
        )
        batch_op.create_check_constraint(
            "ck_orch_eval_outcome",
            "(action_outcome = 'ACCEPTED_INTERNAL' AND risk_outcome = 'ACCEPTED' AND no_order_reason IS NULL AND evaluation_status IN ('TRUE', 'FALSE')) OR (action_outcome != 'ACCEPTED_INTERNAL' AND no_order_reason IS NOT NULL AND length(no_order_reason) BETWEEN 1 AND 64)",
        )
