"""Tests for migration 0007_paper_execution: populated upgrade, schema parity, and downgrade guards."""
import os
import uuid
from pathlib import Path
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from src.database import Base, create_db_engine
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

HEAD = "0007_paper_execution"
PREVIOUS = "0006_strategy_orchestrator"


def migration_config(url: str) -> Config:
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "src/migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


from tests.paper_database_support import paper_test_database


@pytest.fixture(scope="module", params=["sqlite", "postgresql"])
def migrated_database(request, tmp_path_factory):
    path = tmp_path_factory.mktemp("paper_mig") / "audit.db"
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
    path = tmp_path_factory.mktemp("mig_target") / "audit.db"
    with paper_test_database(request.param, path) as (engine, url):
        yield engine, migration_config(url)


def test_0007_revision_graph():
    graph = ScriptDirectory.from_config(migration_config("sqlite:///:memory:"))
    rev = graph.get_revision(HEAD)
    assert rev is not None
    assert rev.down_revision == PREVIOUS
    assert len(HEAD) <= 32


def test_0007_schema_parity(clean_migrated):
    engine, _ = clean_migrated
    inspector = sa.inspect(engine)

    # 1. order_intents.evaluation_id exists and is nullable
    oi_cols = {col["name"]: col for col in inspector.get_columns("order_intents")}
    assert "evaluation_id" in oi_cols
    assert oi_cols["evaluation_id"]["nullable"] is True

    # 2. action_decisions.evaluation_id exists and is nullable
    ad_cols = {col["name"]: col for col in inspector.get_columns("action_decisions")}
    assert "evaluation_id" in ad_cols
    assert ad_cols["evaluation_id"]["nullable"] is True

    # 3. Check constraints on runtime_orchestration_configs
    checks = {c["name"]: c["sqltext"] for c in inspector.get_check_constraints("runtime_orchestration_configs")}
    assert "ck_orch_config_execution" in checks
    assert "INTERNAL_PAPER" in checks["ck_orch_config_execution"]
    assert "ck_orch_config_consent" in checks
    assert "fixture_paper_consent_v1" in checks["ck_orch_config_consent"]

    # 4. Unique constraints
    oi_uqs = {c["name"]: tuple(c["column_names"]) for c in inspector.get_unique_constraints("order_intents")}
    assert "uq_order_intents_eval_action" in oi_uqs
    assert set(oi_uqs["uq_order_intents_eval_action"]) == {"runtime_id", "evaluation_id", "action_mapping_id"}

    ad_uqs = {c["name"]: tuple(c["column_names"]) for c in inspector.get_unique_constraints("action_decisions")}
    assert "uq_action_decisions_eval_action" in ad_uqs
    assert set(ad_uqs["uq_action_decisions_eval_action"]) == {"runtime_id", "evaluation_id", "action_mapping_id"}


from tests.orchestration_support import seed_graph, CLOSE

def test_0007_populated_upgrade_from_0006(migration_target):
    engine, config = migration_target

    # 1. Upgrade to 0006
    command.upgrade(config, PREVIOUS)

    # Seed baseline records in 0006: User, Strategy, PaperAccount, StrategyRuntime, RuntimeOrchestrationConfig
    with Session(engine) as session:
        seed_graph(session, level=1)
        session.commit()

    # Seed action_decision and order_intent without evaluation_id using raw SQL at rev 0006
    act_id = str(uuid.uuid4())
    intent_id = str(uuid.uuid4())
    close_str = CLOSE.isoformat()
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO action_decisions (id, owner_id, runtime_id, candle_timestamp, action_mapping_id, decision, reason_code, created_at) "
                "VALUES (:id, 'owner', 'runtime', :candle_timestamp, 'act_1', 'EXECUTED', 'RULE_MATCH', :created_at)"
            ),
            {"id": act_id, "candle_timestamp": close_str, "created_at": close_str},
        )
        conn.execute(
            sa.text(
                "INSERT INTO order_intents (id, owner_id, runtime_id, action_mapping_id, requested_instrument_id, resolved_instrument_id, intent_type, side, quantity_units, order_type, time_in_force, source_candle_timestamp, source_evaluation_fingerprint, trigger_event_key, created_at) "
                "VALUES (:id, 'owner', 'runtime', 'act_1', 'NIFTY', 'NIFTY', 'ENTRY', 'BUY', 10, 'MARKET', 'DAY', :source_candle_timestamp, :fingerprint, 'evt_key_1', :created_at)"
            ),
            {"id": intent_id, "source_candle_timestamp": close_str, "fingerprint": "0" * 64, "created_at": close_str},
        )

    # 2. Upgrade to 0007 on populated data
    command.upgrade(config, HEAD)

    # 3. Verify data preserved and new columns nullable
    with Session(engine) as session:
        act = session.query(ActionDecision).filter(ActionDecision.id == act_id).one()
        assert act.evaluation_id is None
        assert act.action_mapping_id == "act_1"

        ont = session.query(OrderIntent).filter(OrderIntent.id == intent_id).one()
        assert ont.evaluation_id is None
        assert ont.requested_instrument_id == "NIFTY"

    # Clean child tables before empty downgrade back to 0006
    with Session(engine) as session:
        session.query(ActionDecision).delete()
        session.query(OrderIntent).delete()
        session.query(RuntimeEvaluation).delete()
        session.query(CompletedCandleEvent).delete()
        session.commit()

    command.downgrade(config, PREVIOUS)
    with Session(engine) as session:
        assert session.query(RuntimeOrchestrationConfig).count() == 1


def test_0007_downgrade_guards(migration_target):
    engine, config = migration_target
    command.upgrade(config, HEAD)

    # Setup base entities
    evaluation_id = None
    with Session(engine) as session:
        seed_graph(session)
        evaluation = session.query(RuntimeEvaluation).one()
        evaluation_id = str(evaluation.id)

        # Guard 1: action_decisions with evaluation_id blocks downgrade
        act = ActionDecision(
            id=str(uuid.uuid4()),
            owner_id="owner",
            runtime_id="runtime",
            evaluation_id=evaluation_id,
            candle_timestamp=CLOSE,
            action_mapping_id="act_guard",
            decision="EXECUTED",
            reason_code="RULE_MATCH",
        )
        session.add(act)
        session.commit()

    with pytest.raises(RuntimeError, match="action_decisions contains records linked to evaluations"):
        command.downgrade(config, PREVIOUS)

    # Clear action decision evaluation_id
    with Session(engine) as session:
        session.query(ActionDecision).delete()
        session.commit()

        # Guard 2: order_intents with evaluation_id blocks downgrade
        intent = OrderIntent(
            id=str(uuid.uuid4()),
            owner_id="owner",
            runtime_id="runtime",
            evaluation_id=evaluation_id,
            action_mapping_id="act_guard_intent",
            intent_type="ENTRY",
            requested_instrument_id="NIFTY",
            resolved_instrument_id="NIFTY",
            side="BUY",
            quantity_units=5,
            order_type="MARKET",
            time_in_force="DAY",
            source_candle_timestamp=CLOSE,
            source_evaluation_fingerprint="0" * 64,
            trigger_event_key="evt_guard_intent",
        )
        session.add(intent)
        session.commit()

    with pytest.raises(RuntimeError, match="order_intents contains records linked to evaluations"):
        command.downgrade(config, PREVIOUS)

    # Clear order intent evaluation_id
    with Session(engine) as session:
        session.query(OrderIntent).delete()
        session.commit()

    # Guard 3: runtime_orchestration_configs with INTERNAL_PAPER blocks downgrade
    with engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE runtime_orchestration_configs SET execution_policy = 'INTERNAL_PAPER', consent_policy_version = 'fixture_paper_consent_v1'")
        )

    with pytest.raises(RuntimeError, match="runtime_orchestration_configs contains INTERNAL_PAPER records"):
        command.downgrade(config, PREVIOUS)

    # Clean up INTERNAL_PAPER config, candle events, and evaluation, then verify downgrade succeeds
    with engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE runtime_orchestration_configs SET execution_policy = 'INTERNAL_MOCK_ONLY', consent_policy_version = 'fixture_consent_v1'")
        )
    with Session(engine) as session:
        session.query(RuntimeEvaluation).delete()
        session.query(CompletedCandleEvent).delete()
        session.commit()

    command.downgrade(config, PREVIOUS)
