"""Add inspection_runs table for persistent history and historical replays

Revision ID: 0002_inspection_history
Revises: 0001_initial_schema
Create Date: 2026-08-29 17:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0002_inspection_history'
down_revision: Union[str, None] = '0001_initial_schema'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'inspection_runs',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('strategy_id', sa.String(length=36), nullable=True),
        sa.Column('strategy_version_snapshot', sa.String(length=100), nullable=True),
        sa.Column('strategy_definition_snapshot', sa.JSON(), nullable=True),
        sa.Column('run_type', sa.String(length=50), nullable=False),
        sa.Column('reference_dataset_id', sa.String(length=255), nullable=True),
        sa.Column('subject_dataset_ids', sa.JSON(), nullable=False),
        sa.Column('requested_start_timestamp', sa.DateTime(timezone=True), nullable=True),
        sa.Column('requested_end_timestamp', sa.DateTime(timezone=True), nullable=True),
        sa.Column('requested_evaluation_timestamp', sa.DateTime(timezone=True), nullable=True),
        sa.Column('timeframe', sa.String(length=50), nullable=False),
        sa.Column('engine_version', sa.String(length=50), nullable=False, server_default='1.0.0'),
        sa.Column('manifest_version', sa.String(length=50), nullable=False, server_default='1.0.0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.String(length=50), nullable=False),
        sa.Column('failure_summary', sa.String(length=2048), nullable=True),
        sa.Column('result_summary', sa.String(length=2048), nullable=True),
        sa.Column('result_payload', sa.JSON(), nullable=True),
        sa.Column('synthetic_data_confirmed', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('request_fingerprint', sa.String(length=64), nullable=True),
        sa.Column('completed_fingerprint', sa.String(length=64), nullable=True),
        sa.Column('manifest_checksums_snapshot', sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint("run_type IN ('SINGLE_SERIES', 'MULTI_SERIES', 'HISTORICAL_REPLAY')", name='ck_inspection_runs_run_type'),
        sa.CheckConstraint("status IN ('COMPLETED', 'FAILED')", name='ck_inspection_runs_status'),
        sa.CheckConstraint("synthetic_data_confirmed IS TRUE", name='ck_inspection_runs_synthetic_confirmed'),
        sa.CheckConstraint(
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
            name='ck_inspection_runs_completed_fields'
        ),
        sa.CheckConstraint(
            "status != 'FAILED' OR ("
            "failure_summary IS NOT NULL AND "
            "result_payload IS NULL"
            ")",
            name='ck_inspection_runs_failed_fields'
        ),
        sa.UniqueConstraint('completed_fingerprint', name='uq_inspection_runs_completed_fingerprint')
    )
    op.create_index(op.f('ix_inspection_runs_strategy_id'), 'inspection_runs', ['strategy_id'], unique=False)
    op.create_index(op.f('ix_inspection_runs_run_type'), 'inspection_runs', ['run_type'], unique=False)
    op.create_index(op.f('ix_inspection_runs_status'), 'inspection_runs', ['status'], unique=False)
    op.create_index(op.f('ix_inspection_runs_created_at'), 'inspection_runs', ['created_at'], unique=False)
    op.create_index(op.f('ix_inspection_runs_request_fingerprint'), 'inspection_runs', ['request_fingerprint'], unique=False)
    op.create_index(op.f('ix_inspection_runs_completed_fingerprint'), 'inspection_runs', ['completed_fingerprint'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_inspection_runs_completed_fingerprint'), table_name='inspection_runs')
    op.drop_index(op.f('ix_inspection_runs_request_fingerprint'), table_name='inspection_runs')
    op.drop_index(op.f('ix_inspection_runs_created_at'), table_name='inspection_runs')
    op.drop_index(op.f('ix_inspection_runs_status'), table_name='inspection_runs')
    op.drop_index(op.f('ix_inspection_runs_run_type'), table_name='inspection_runs')
    op.drop_index(op.f('ix_inspection_runs_strategy_id'), table_name='inspection_runs')
    op.drop_table('inspection_runs')
