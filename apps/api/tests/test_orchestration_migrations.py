"""Migrated-schema tests, parameterized over SQLite and isolated PostgreSQL databases."""
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
from src.engine.orchestration.storage import ExactInteger
from src.models import RuntimeOrchestrationConfig, CompletedCandleEvent, RuntimeEvaluation
from tests.orchestration_support import seed_graph, seed_parent, NOW, OPEN, CLOSE

HEAD = "0006_strategy_orchestrator"
PREVIOUS = "0005_upstox_sandbox"
MODELS = (RuntimeOrchestrationConfig, CompletedCandleEvent, RuntimeEvaluation)
TABLES = tuple(model.__tablename__ for model in MODELS)


def migration_config(url):
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "src/migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


@pytest.fixture(scope="module", params=["sqlite", "postgresql"])
def migrated_database(request, tmp_path_factory):
    admin = None
    if request.param == "sqlite":
        url = "sqlite:///" + (tmp_path_factory.mktemp("orchestration") / "audit.db").as_posix()
    else:
        configured = os.environ.get("DATABASE_URL", "")
        if not configured.startswith("postgresql"):
            pytest.skip("PostgreSQL: CI-PENDING (no PostgreSQL test connection)")
        # Reuse CI's existing database, with one isolated schema for this module.
        # Authentication/DDL failures are errors, never reasons to skip.
        admin = sa.create_engine(configured, isolation_level="AUTOCOMMIT")
        schema = "test_orch_" + uuid.uuid4().hex
        with admin.connect() as conn:
            conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        parsed = sa.engine.make_url(configured)
        options = dict(parsed.query)
        options["options"] = "-csearch_path=" + schema
        url = parsed.set(query=options).render_as_string(hide_password=False)
    config = migration_config(url)
    engine = create_db_engine(url)
    try:
        command.upgrade(config, HEAD)
        yield engine, config
    finally:
        engine.dispose()
        if admin is not None:
            with admin.connect() as conn:
                conn.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
            admin.dispose()


@pytest.fixture
def migrated(migrated_database):
    engine, config = migrated_database
    yield engine, config
    # Tests may commit, or exercise downgrade. Reset only this isolated schema's
    # rows in child-first order; do not run ORM guards during fixture cleanup.
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture
def populated(migrated):
    engine, _ = migrated
    with Session(engine) as session:
        seed_graph(session)
        seed_parent(session, "other")
        session.commit()
    return engine


def test_sole_head_and_revision_length():
    graph = ScriptDirectory.from_config(migration_config("sqlite:///:memory:"))
    assert graph.get_heads() == [HEAD]
    assert graph.get_revision(HEAD).down_revision == PREVIOUS
    assert len(HEAD) == 26 <= 32


def test_fresh_and_stepwise_empty_downgrade_reupgrade(migrated):
    engine, config = migrated
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar() == HEAD
    assert set(TABLES) <= set(sa.inspect(engine).get_table_names())
    command.downgrade(config, PREVIOUS)
    assert not set(TABLES) & set(sa.inspect(engine).get_table_names())
    command.upgrade(config, HEAD)
    assert set(TABLES) <= set(sa.inspect(engine).get_table_names())


def test_orm_reflected_schema_parity(migrated):
    engine, _ = migrated
    inspector = sa.inspect(engine)
    for model in MODELS:
        table = model.__table__
        columns = {col["name"]: col for col in inspector.get_columns(table.name)}
        assert set(columns) == set(table.c.keys())
        for col in table.columns:
            reflected = columns[col.name]
            assert reflected["nullable"] == col.nullable
            if isinstance(col.type, ExactInteger):
                expected = sa.LargeBinary if engine.dialect.name == "sqlite" else sa.Numeric
                assert isinstance(reflected["type"], expected)
            else:
                assert reflected["type"]._type_affinity == col.type._type_affinity
            assert getattr(reflected["type"], "length", None) == getattr(col.type, "length", None)
            if col.server_default is None:
                assert reflected["default"] is None
            else:
                assert reflected["default"].split("::")[0].strip("'()") == str(col.server_default.arg)
        assert set(inspector.get_pk_constraint(table.name)["constrained_columns"]) == {c.name for c in table.primary_key}
        expected_unique = {c.name: tuple(col.name for col in c.columns) for c in table.constraints if isinstance(c, sa.UniqueConstraint)}
        assert {c["name"]: tuple(c["column_names"]) for c in inspector.get_unique_constraints(table.name)} == expected_unique
        expected_checks = {c.name: str(c.sqltext) for c in table.constraints if isinstance(c, sa.CheckConstraint) and (c._ddl_if is None or engine.dialect.name == "sqlite")}
        actual_checks = {c["name"]: c["sqltext"] for c in inspector.get_check_constraints(table.name)}
        assert set(actual_checks) == set(expected_checks)
        if engine.dialect.name == "sqlite":
            assert actual_checks == expected_checks
        expected_fks = {c.name: c for c in table.constraints if isinstance(c, sa.ForeignKeyConstraint)}
        actual_fks = {c["name"]: c for c in inspector.get_foreign_keys(table.name)}
        assert set(actual_fks) == set(expected_fks)
        for name, fk in expected_fks.items():
            reflected = actual_fks[name]
            assert reflected["constrained_columns"] == [c.name for c in fk.columns]
            assert reflected["constrained_columns"][0] == "owner_id"
            assert reflected["referred_columns"] == [e.column.name for e in fk.elements]
            assert reflected["referred_table"] == fk.referred_table.name
            assert reflected["options"]["ondelete"] == "RESTRICT"
            assert reflected["options"]["onupdate"] == "RESTRICT"
        assert {i["name"]: tuple(i["column_names"]) for i in inspector.get_indexes(table.name) if not i.get("duplicates_constraint")} == {i.name: tuple(c.name for c in i.columns) for i in table.indexes}


@pytest.mark.parametrize("level", [1, 2, 3])
def test_populated_downgrade_refused_for_each_table(migrated, level):
    engine, config = migrated
    with Session(engine) as session:
        seed_graph(session, level)
        session.commit()
    with pytest.raises(RuntimeError, match=TABLES[level - 1]):
        command.downgrade(config, PREVIOUS)
    assert set(TABLES) <= set(sa.inspect(engine).get_table_names())


# One invalid mutation for every new CHECK constraint. Raw SQL deliberately
# bypasses ORM immutability to prove database enforcement independently.
CHECK_CASES = [
    (0, "alignment", {"alignment_offset_seconds": -1}),
    (0, "source", {"source_type": "REALTIME_PROVIDER"}),
    (0, "execution", {"execution_policy": "EXTERNAL"}),
    (0, "timeframe", {"timeframe": "1d"}),
    (0, "ids", {"id": ""}),
    (0, "namespace", {"source_namespace": ""}),
    (0, "fingerprint", {"snapshot_fingerprint": "bad"}),
    (0, "snapshot", {"snapshot_json": "x"}),
    (0, "consent", {"consent_policy_version": ""}),
    (0, "replay", {"replay_close_at": OPEN}),
    (0, "checkpoint", {"checkpoint_close_at": OPEN}),
    (0, "lease", {"lease_owner": "worker"}),
    (0, "generation", {"fencing_generation": 0}),
    (0, "retries", {"retry_count": 101}),
    (0, "reason", {"last_reason_code": ""}),
    (0, "times", {"updated_at": OPEN}),
    (1, "source", {"source_type": "REALTIME_PROVIDER"}),
    (1, "role", {"series_role": "OTHER"}),
    (1, "alignment", {"alignment_offset_seconds": -1}),
    (1, "timeframe", {"timeframe": "1d"}),
    (1, "final", {"is_closed": False}),
    (1, "ids", {"id": ""}),
    (1, "identifiers", {"dataset_id": ""}),
    (1, "hashes", {"content_fingerprint": "bad"}),
    (1, "times", {"received_at": OPEN}),
    (1, "scales", {"price_scale": 9}),
    (1, "units", {"open_units": 0}),
    (1, "geometry", {"high_units": 91}),
    (2, "ids", {"id": ""}),
    (2, "hashes", {"evaluation_fingerprint": "bad"}),
    (2, "timeframe", {"timeframe": "1d"}),
    (2, "status", {"evaluation_status": "RUNNING"}),
    (2, "action", {"action_outcome": "EXTERNAL_ORDER"}),
    (2, "risk", {"risk_outcome": "OTHER"}),
    (2, "outcome", {"no_order_reason": None}),
    (2, "evidence", {"audit_json": "x" * 65537}),
    (2, "time", {"finalized_at": OPEN}),
]


@pytest.mark.parametrize("model_index,check,changes", CHECK_CASES)
def test_database_checks(populated, model_index, check, changes):
    table = MODELS[model_index].__table__
    with populated.begin() as conn:
        with pytest.raises(sa.exc.IntegrityError):
            with conn.begin_nested():
                conn.execute(table.update().values(**changes))


@pytest.mark.parametrize("field", ["open_units", "high_units", "low_units", "close_units", "volume_units", "price_scale", "volume_scale"])
def test_cannot_persist_fractional_scaled_integers(populated, field):
    with populated.begin() as conn:
        row = conn.execute(sa.select(CompletedCandleEvent.__table__)).mappings().one()
        with pytest.raises(sa.exc.IntegrityError):
            with conn.begin_nested():
                conn.execute(sa.text(f"UPDATE completed_candle_events SET {field} = :value"), {"value": row[field] + 0.5})


def test_every_check_has_a_behavioral_case():
    prefixes = ["ck_orch_config_", "ck_orch_candle_", "ck_orch_eval_"]
    for index, model in enumerate(MODELS):
        assert {c.name for c in model.__table__.constraints if isinstance(c, sa.CheckConstraint) and c._ddl_if is None} == {
            prefixes[index] + name for i, name, _ in CHECK_CASES if i == index
        }


@pytest.mark.parametrize("model_index", [0, 1, 2])
def test_owner_not_null_and_cross_owner_rejected(populated, model_index):
    table = MODELS[model_index].__table__
    with populated.begin() as conn:
        for owner in (None, "ownerother"):
            with pytest.raises(sa.exc.IntegrityError):
                with conn.begin_nested():
                    conn.execute(table.update().values(owner_id=owner))


@pytest.mark.parametrize("changes", [
    {"config_id": "missing"}, {"runtime_id": "runtimeother"},
    {"snapshot_fingerprint": "b" * 64}, {"reference_candle_id": "missing"},
    {"subject_candle_id": "missing"},
])
def test_evaluation_associations_are_consistent(populated, changes):
    with populated.begin() as conn:
        with pytest.raises(sa.exc.IntegrityError):
            with conn.begin_nested():
                conn.execute(RuntimeEvaluation.__table__.update().values(**changes))


@pytest.mark.parametrize("model_index,changes", [
    (0, {}),
    (1, {"source_event_id": "different"}),  # same content
    (1, {"content_fingerprint": "b" * 64, "high_units": 130}),  # same event
    (1, {"source_event_id": "different", "content_fingerprint": "b" * 64, "high_units": 130}),  # conflicting interval
    (2, {}),
    (2, {"evaluation_fingerprint": "b" * 64}),  # same interval, different evidence
])
def test_duplicate_and_conflicting_identities(populated, model_index, changes):
    table = MODELS[model_index].__table__
    with populated.begin() as conn:
        row = dict(conn.execute(sa.select(table)).mappings().one())
        row.update(id="another", **changes)
        with pytest.raises(sa.exc.IntegrityError):
            with conn.begin_nested():
                conn.execute(table.insert().values(**row))
        assert conn.execute(sa.select(sa.func.count()).select_from(table)).scalar() == 1


@pytest.mark.parametrize("model_index", [0, 1, 2])
def test_orm_audits_are_immutable(populated, model_index):
    model = MODELS[model_index]
    with Session(populated) as session:
        row = session.query(model).one()
        setattr(row, "source_namespace" if model_index < 2 else "audit_json", "changed")
        with pytest.raises(ValueError, match="[Ii]mmutable"):
            session.flush()
        session.rollback()
        session.delete(session.query(model).one())
        with pytest.raises(ValueError, match="cannot be deleted"):
            session.flush()


def test_mutable_claims_and_monotonic_checkpoint(populated):
    with Session(populated) as session:
        config = session.query(RuntimeOrchestrationConfig).one()
        config.fencing_generation = 2
        config.checkpoint_close_at = CLOSE
        config.lease_owner = "worker"
        config.lease_expires_at = NOW
        session.commit()
        # Attributes are expired by commit; a backwards write must still fail.
        config.fencing_generation = 1
        with pytest.raises(ValueError, match="backwards"):
            session.flush()


def test_checkpoint_cannot_be_cleared_after_commit(populated):
    with Session(populated) as session:
        config = session.query(RuntimeOrchestrationConfig).one()
        config.checkpoint_close_at = CLOSE
        session.commit()
        config.checkpoint_close_at = None
        with pytest.raises(ValueError, match="backwards"):
            session.flush()


@pytest.mark.parametrize("table_name", ["strategy_runtimes", "runtime_orchestration_configs", "completed_candle_events"])
def test_parent_delete_restricted(populated, table_name):
    table = Base.metadata.tables[table_name]
    with populated.begin() as conn:
        with pytest.raises(sa.exc.IntegrityError):
            with conn.begin_nested():
                conn.execute(table.delete())



def test_integer_storage_and_bounds(populated):
    fields = ["open_units", "high_units", "low_units", "close_units", "volume_units", "price_scale", "volume_scale"]
    with populated.begin() as conn:
        for field in fields:
            upper = 8 if field.endswith("scale") else 9_000_000_000_000_000
            bad = [upper + 1, 1.5]
            if populated.dialect.name == "sqlite":
                bad += ["1", "1.5", "abc"]
            for value in bad:
                with pytest.raises(sa.exc.IntegrityError):
                    with conn.begin_nested():
                        conn.execute(sa.text(f"UPDATE completed_candle_events SET {field} = :value"), {"value": value})
        # Typed SQLAlchemy paths reject even numeric strings before coercion.
        for value in ("100", 100.0, True):
            with pytest.raises(sa.exc.StatementError, match="exact Python integer"):
                with conn.begin_nested():
                    conn.execute(CompletedCandleEvent.__table__.update().values(open_units=value))
        if populated.dialect.name == "sqlite":
            assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            for table, field in [("runtime_orchestration_configs", "retry_count"), ("completed_candle_events", "revision")]:
                with pytest.raises(sa.exc.IntegrityError):
                    with conn.begin_nested():
                        conn.execute(sa.text(f"UPDATE {table} SET {field} = :value"), {"value": "1"})


def test_refresh_expire_and_same_value_assignments(populated):
    with Session(populated) as session:
        for model in MODELS:
            row = session.query(model).one()
            session.refresh(row)
            values = {column.name: getattr(row, column.name) for column in model.__table__.columns}
            session.expire(row)
            for key, value in values.items():
                setattr(row, key, value)
            session.flush()
        session.commit()


def test_alignment_cannot_create_second_evaluation(populated):
    with Session(populated) as session:
        config = session.query(RuntimeOrchestrationConfig).one()
        config.alignment_offset_seconds = 60
        with pytest.raises(ValueError, match="Immutable"):
            session.commit()
        session.rollback()
    with populated.begin() as conn:
        config = RuntimeOrchestrationConfig.__table__
        row = dict(conn.execute(sa.select(config)).mappings().one())
        row.update(id="second", alignment_offset_seconds=60, snapshot_fingerprint="b" * 64)
        with pytest.raises(sa.exc.IntegrityError):
            with conn.begin_nested():
                conn.execute(config.insert().values(**row))
        evaluation = RuntimeEvaluation.__table__
        row = dict(conn.execute(sa.select(evaluation)).mappings().one())
        row.update(id="second", snapshot_fingerprint="b" * 64, evaluation_fingerprint="b" * 64)
        with pytest.raises(sa.exc.IntegrityError):
            with conn.begin_nested():
                conn.execute(evaluation.insert().values(**row))
        assert conn.execute(sa.select(sa.func.count()).select_from(evaluation)).scalar() == 1


def test_structured_consent_and_evidence_insert_guards(migrated):
    import json
    from src.engine.orchestration.evidence import config_consent_fingerprint
    engine, _ = migrated
    with Session(engine) as session:
        seed_graph(session)
        config = session.query(RuntimeOrchestrationConfig).one()
        assert not hasattr(config, "consent_evidence")
        assert config.consent_fingerprint == config_consent_fingerprint(config)
        session.commit()
    for field, bad in [("audit_json", '{"request_body":{}}'), ("risk_summary_json", '{"token":"test"}'),
                       ("audit_json", '{"condition_ids":["bad\\ncode"]}')]:
        with Session(engine) as session:
            existing = session.query(RuntimeEvaluation).one()
            row = {c.name: getattr(existing, c.name) for c in RuntimeEvaluation.__table__.columns}
            row.update(id="bad", **{field: bad})
            session.add(RuntimeEvaluation(**row))
            with pytest.raises(ValueError):
                session.flush()


def test_fk_parent_keys_and_postgres_ddl():
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable
    for model in MODELS:
        table = model.__table__
        ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        assert "typeof(" not in ddl
        for constraint in table.constraints:
            if constraint.name:
                assert len(constraint.name) <= 63
            if isinstance(constraint, sa.ForeignKeyConstraint):
                assert list(constraint.columns)[0].name == "owner_id"
                parent_columns = tuple(element.column.name for element in constraint.elements)
                parent = constraint.referred_table
                keys = {tuple(column.name for column in key.columns) for key in parent.constraints
                        if isinstance(key, (sa.PrimaryKeyConstraint, sa.UniqueConstraint))}
                assert parent_columns in keys
                assert constraint.ondelete == constraint.onupdate == "RESTRICT"
