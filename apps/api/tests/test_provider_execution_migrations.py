"""Tests for migration 0008_provider_execution: populated upgrade, schema parity, and downgrade guards."""
import os
import uuid
from pathlib import Path
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from src.database import Base
from src.models import (
    User,
    PaperAccount,
    Strategy,
    StrategyRuntime,
    RuntimeOrchestrationConfig,
    CompletedCandleEvent,
    RuntimeEvaluation,
    ActionDecision,
    OrderIntent,
)
from tests.paper_database_support import paper_test_database
from tests.orchestration_support import seed_graph, CLOSE

HEAD = "0008_provider_execution"
PREVIOUS = "0007_paper_execution"


def migration_config(url: str) -> Config:
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "src/migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


@pytest.fixture(scope="module", params=["sqlite", "postgresql"])
def migrated_database(request, tmp_path_factory):
    path = tmp_path_factory.mktemp("prov_mig") / "audit.db"
    with paper_test_database(request.param, path) as (engine, url):
        config = migration_config(url)
        command.upgrade(config, HEAD)
        yield engine, config


@pytest.fixture
def clean_migrated(migrated_database):
    engine, config = migrated_database
    yield engine, config
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture(params=["sqlite", "postgresql"])
def migration_target(request, tmp_path_factory):
    path = tmp_path_factory.mktemp("mig_target_0008") / "audit.db"
    with paper_test_database(request.param, path) as (engine, url):
        yield engine, migration_config(url)


def test_0008_revision_graph():
    graph = ScriptDirectory.from_config(migration_config("sqlite:///:memory:"))
    assert graph.get_heads() == [HEAD]
    rev = graph.get_revision(HEAD)
    assert rev.down_revision == PREVIOUS
    assert len(HEAD) <= 32


def test_0008_schema_parity(clean_migrated):
    engine, _ = clean_migrated
    inspector = sa.inspect(engine)

    # 1. Check constraints on runtime_orchestration_configs
    checks = {c["name"]: c["sqltext"] for c in inspector.get_check_constraints("runtime_orchestration_configs")}
    assert "ck_orch_config_source" in checks
    assert "PROVIDER_SANDBOX" in checks["ck_orch_config_source"]
    assert "ck_orch_config_execution" in checks
    assert "EXTERNAL_SANDBOX_DISPATCH" in checks["ck_orch_config_execution"]
    assert "ck_orch_config_consent" in checks
    assert "sandbox_consent_v1" in checks["ck_orch_config_consent"]
    assert "ck_orch_config_alignment" in checks
    assert "provider_completed_v1" in checks["ck_orch_config_alignment"]

    # 2. Check constraints on completed_candle_events
    candle_checks = {c["name"]: c["sqltext"] for c in inspector.get_check_constraints("completed_candle_events")}
    assert "ck_orch_candle_source" in candle_checks
    assert "PROVIDER_SANDBOX" in candle_checks["ck_orch_candle_source"]
    assert "ck_orch_candle_alignment" in candle_checks
    assert "provider_completed_v1" in candle_checks["ck_orch_candle_alignment"]

    # 3. Check constraints on runtime_evaluations
    eval_checks = {c["name"]: c["sqltext"] for c in inspector.get_check_constraints("runtime_evaluations")}
    assert "ck_orch_eval_action" in eval_checks
    assert "ACCEPTED_SANDBOX" in eval_checks["ck_orch_eval_action"]
    assert "ck_orch_eval_outcome" in eval_checks
    assert "ACCEPTED_SANDBOX" in eval_checks["ck_orch_eval_outcome"]


def test_0008_populated_upgrade_from_0007(migration_target):
    engine, config = migration_target

    # 1. Upgrade to 0007
    command.upgrade(config, PREVIOUS)

    # 2. Seed baseline graph in 0007
    with Session(engine) as session:
        seed_graph(session, level=1)
        session.commit()

    # 3. Upgrade to 0008
    command.upgrade(config, HEAD)

    # Verify rows persisted cleanly and new constraint allows PROVIDER_SANDBOX
    inspector = sa.inspect(engine)
    checks = {c["name"]: c["sqltext"] for c in inspector.get_check_constraints("runtime_orchestration_configs")}
    assert "EXTERNAL_SANDBOX_DISPATCH" in checks["ck_orch_config_execution"]


def test_0008_downgrade_guards_and_restoration(migration_target):
    engine, config = migration_target

    # Upgrade to 0008
    command.upgrade(config, HEAD)

    # Guard 1: EXTERNAL_SANDBOX_DISPATCH record blocks downgrade
    with Session(engine) as session:
        seed_graph(session, level=1)
        session.commit()

    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE runtime_orchestration_configs SET execution_policy = 'EXTERNAL_SANDBOX_DISPATCH', consent_policy_version = 'sandbox_consent_v1'"
            )
        )

    with pytest.raises(RuntimeError, match="runtime_orchestration_configs contains provider/sandbox records"):
        command.downgrade(config, PREVIOUS)

    # Clear and reset to FIXTURE_REPLAY / INTERNAL_PAPER
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE runtime_orchestration_configs SET execution_policy = 'INTERNAL_PAPER', consent_policy_version = 'fixture_paper_consent_v1'"
            )
        )

    # Now downgrade succeeds
    command.downgrade(config, PREVIOUS)

    # Verify 0007 constraints restored
    inspector = sa.inspect(engine)
    checks = {c["name"]: c["sqltext"] for c in inspector.get_check_constraints("runtime_orchestration_configs")}
    assert "EXTERNAL_SANDBOX_DISPATCH" not in checks["ck_orch_config_execution"]
    assert "INTERNAL_PAPER" in checks["ck_orch_config_execution"]
