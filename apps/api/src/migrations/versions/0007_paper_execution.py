"""Automated paper execution, durable evaluation linkages, and paired consent

Revision ID: 0007_paper_execution
Revises: 0006_strategy_orchestrator
Create Date: 2026-09-22

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from src.database import UTCDateTime

# revision identifiers, used by Alembic.
revision: str = "0007_paper_execution"
down_revision: Union[str, None] = "0006_strategy_orchestrator"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add evaluation_id to order_intents with composite FK and unique constraint
    with op.batch_alter_table("order_intents") as batch_op:
        batch_op.add_column(sa.Column("evaluation_id", sa.String(36), nullable=True))
        batch_op.create_foreign_key(
            "fk_order_intents_evaluation",
            "runtime_evaluations",
            ["owner_id", "runtime_id", "evaluation_id"],
            ["owner_id", "runtime_id", "id"],
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint(
            "uq_order_intents_eval_action",
            ["runtime_id", "evaluation_id", "action_mapping_id"],
        )

    # 2. Add evaluation_id to action_decisions with composite FK and unique constraint
    with op.batch_alter_table("action_decisions") as batch_op:
        batch_op.add_column(sa.Column("evaluation_id", sa.String(36), nullable=True))
        batch_op.create_foreign_key(
            "fk_action_decisions_evaluation",
            "runtime_evaluations",
            ["owner_id", "runtime_id", "evaluation_id"],
            ["owner_id", "runtime_id", "id"],
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint(
            "uq_action_decisions_eval_action",
            ["runtime_id", "evaluation_id", "action_mapping_id"],
        )

    # 3. Update execution and consent check constraints in runtime_orchestration_configs
    with op.batch_alter_table("runtime_orchestration_configs") as batch_op:
        batch_op.drop_constraint("ck_orch_config_execution", type_="check")
        batch_op.drop_constraint("ck_orch_config_consent", type_="check")
        batch_op.create_check_constraint(
            "ck_orch_config_execution",
            "execution_policy IN ('INTERNAL_MOCK_ONLY', 'INTERNAL_PAPER')",
        )
        batch_op.create_check_constraint(
            "ck_orch_config_consent",
            "(((execution_policy = 'INTERNAL_MOCK_ONLY' AND consent_policy_version = 'fixture_consent_v1') OR (execution_policy = 'INTERNAL_PAPER' AND consent_policy_version = 'fixture_paper_consent_v1')) AND length(consent_fingerprint) = 64)",
        )


def downgrade() -> None:
    conn = op.get_bind()

    # Guard 1: Cannot downgrade if action_decisions contains records linked to evaluations
    has_linked_actions = conn.execute(
        sa.text("SELECT 1 FROM action_decisions WHERE evaluation_id IS NOT NULL LIMIT 1")
    ).fetchone()
    if has_linked_actions:
        raise RuntimeError("Cannot downgrade: action_decisions contains records linked to evaluations.")

    # Guard 2: Cannot downgrade if order_intents contains records linked to evaluations
    has_linked_intents = conn.execute(
        sa.text("SELECT 1 FROM order_intents WHERE evaluation_id IS NOT NULL LIMIT 1")
    ).fetchone()
    if has_linked_intents:
        raise RuntimeError("Cannot downgrade: order_intents contains records linked to evaluations.")

    # Guard 3: Cannot downgrade if runtime_orchestration_configs contains INTERNAL_PAPER records
    has_paper_configs = conn.execute(
        sa.text("SELECT 1 FROM runtime_orchestration_configs WHERE execution_policy = 'INTERNAL_PAPER' LIMIT 1")
    ).fetchone()
    if has_paper_configs:
        raise RuntimeError("Cannot downgrade: runtime_orchestration_configs contains INTERNAL_PAPER records.")

    # Drop constraints and columns from action_decisions
    with op.batch_alter_table("action_decisions") as batch_op:
        batch_op.drop_constraint("fk_action_decisions_evaluation", type_="foreignkey")
        batch_op.drop_constraint("uq_action_decisions_eval_action", type_="unique")
        batch_op.drop_column("evaluation_id")

    # Drop constraints and columns from order_intents
    with op.batch_alter_table("order_intents") as batch_op:
        batch_op.drop_constraint("fk_order_intents_evaluation", type_="foreignkey")
        batch_op.drop_constraint("uq_order_intents_eval_action", type_="unique")
        batch_op.drop_column("evaluation_id")

    # Restore individual check constraints on runtime_orchestration_configs
    with op.batch_alter_table("runtime_orchestration_configs") as batch_op:
        batch_op.drop_constraint("ck_orch_config_execution", type_="check")
        batch_op.drop_constraint("ck_orch_config_consent", type_="check")
        batch_op.create_check_constraint(
            "ck_orch_config_execution",
            "execution_policy = 'INTERNAL_MOCK_ONLY'",
        )
        batch_op.create_check_constraint(
            "ck_orch_config_consent",
            "consent_policy_version = 'fixture_consent_v1' AND length(consent_fingerprint) = 64",
        )
