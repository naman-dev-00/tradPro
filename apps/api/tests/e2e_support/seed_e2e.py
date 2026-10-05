import os
import sys
import json
import secrets
import pathlib
from typing import Dict, Any
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Import models & auth hashing

SENTINEL_FILENAME = ".tradepro-e2e-disposable"

def validate_disposable_e2e_environment(
    app_env: str | None,
    database_url: str | None,
    e2e_temp_dir: str | None,
    primary_dev_db_path: pathlib.Path | None = None
) -> pathlib.Path:
    """
    Strict safety check ensuring the database is an ephemeral disposable SQLite database
    created exclusively by the E2E test runner inside a dedicated temp directory.
    """
    if app_env != "test":
        raise ValueError(f"E2E seed helper strictly requires APP_ENV='test'. Received: '{app_env}'")

    if not database_url or not database_url.startswith("sqlite:///"):
        raise ValueError(f"E2E seed helper requires a local SQLite URL ('sqlite:///...'). Received: '{database_url}'")

    if not e2e_temp_dir:
        raise ValueError("E2E_TEMP_DIR environment variable must be specified.")

    # 1. Resolve temp directory to canonical absolute path
    temp_dir_path = pathlib.Path(e2e_temp_dir).resolve()
    if not temp_dir_path.exists() or not temp_dir_path.is_dir():
        raise ValueError(f"E2E temporary directory does not exist or is not a directory: {temp_dir_path}")

    if not temp_dir_path.name.startswith("tradepro_e2e_"):
        raise ValueError(f"E2E temporary directory name must start with 'tradepro_e2e_'. Received: '{temp_dir_path.name}'")

    # 2. Check for sentinel file
    sentinel_path = temp_dir_path / SENTINEL_FILENAME
    if not sentinel_path.exists():
        raise ValueError(f"E2E temporary directory is missing required sentinel file '{SENTINEL_FILENAME}'.")

    # 3. Extract and resolve database file path
    db_file_str = database_url[len("sqlite:///"):]
    db_file_path = pathlib.Path(db_file_str)
    if not db_file_path.is_absolute():
        posix_candidate = pathlib.Path("/" + db_file_str).resolve()
        try:
            posix_candidate.relative_to(temp_dir_path)
            db_file_path = posix_candidate
        except ValueError:
            pass
    db_file_path = db_file_path.resolve()

    # 4. Check that db file is strictly inside the temp directory
    try:
        db_file_path.relative_to(temp_dir_path)
    except ValueError:
        raise ValueError(f"Database path '{db_file_path}' is outside the E2E temporary directory '{temp_dir_path}'.")

    # 5. Explicitly check against normal development tradepro.db path
    if primary_dev_db_path is None:
        # Default canonical location of apps/api/tradepro.db
        current_file = pathlib.Path(__file__).resolve()
        primary_dev_db_path = (current_file.parent.parent.parent / "tradepro.db").resolve()

    if db_file_path == primary_dev_db_path:
        raise ValueError(f"CRITICAL SAFETY VIOLATION: Refusing to seed normal development database at '{primary_dev_db_path}'.")

    return db_file_path

def seed_e2e_database() -> Dict[str, Any]:
    app_env = os.getenv("APP_ENV")
    database_url = os.getenv("DATABASE_URL")
    e2e_temp_dir = os.getenv("E2E_TEMP_DIR")
    manifest_path = os.getenv("E2E_MANIFEST_PATH")

    if not manifest_path and e2e_temp_dir:
        manifest_path = str(pathlib.Path(e2e_temp_dir) / "e2e_manifest.json")

    validate_disposable_e2e_environment(app_env, database_url, e2e_temp_dir)

    from src.models import User, Strategy
    from src.auth.security import hash_password

    engine = create_engine(database_url)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()

    manifest: Dict[str, Any] = {
        "users": {},
        "strategies": {}
    }

    try:
        # Generate in-memory random test credentials
        test_accounts = [
            ("admin", "e2e_admin", "e2e_admin@example.com", "ADMIN", True),
            ("editor", "e2e_editor", "e2e_editor@example.com", "EDITOR", True),
            ("viewer", "e2e_viewer", "e2e_viewer@example.com", "VIEWER", True),
            ("other_editor", "e2e_other_editor", "e2e_other_editor@example.com", "EDITOR", True),
            ("disabled_user", "e2e_disabled_user", "e2e_disabled@example.com", "EDITOR", False),
        ]

        created_users = {}

        for role_key, username, email, role, is_active in test_accounts:
            password = secrets.token_urlsafe(24)
            pwd_hash = hash_password(password)

            user = User(
                username=username,
                normalized_username=username.casefold(),
                email=email,
                normalized_email=email.casefold(),
                hashed_password=pwd_hash,
                role=role,
                is_active=is_active
            )
            db.add(user)
            db.flush()

            created_users[role_key] = user
            manifest["users"][role_key] = {
                "id": str(user.id),
                "username": username,
                "email": email,
                "password": password,
                "role": role,
                "is_active": is_active
            }

        # Create starter strategy owned by editor
        editor_user = created_users["editor"]
        starter_strategy = Strategy(
            owner_id=editor_user.id,
            name="E2E Starter Strategy",
            description="Synthetic strategy created for automated E2E browser tests",
            timeframe="15m",
            candidate_selection_mode="FIRST_ELIGIBLE",
            payload={
                "id": "e2e-starter-strat",
                "name": "E2E Starter Strategy",
                "timeframe": "15m",
                "candidate_selection_mode": "FIRST_ELIGIBLE",
                "global_conditions": {
                    "id": "global.group.0",
                    "type": "AND",
                    "conditions": [
                        {
                            "id": "global.cond.sma",
                            "type": "CONDITION",
                            "lhs": {"indicator": "SMA", "symbol": "SYNTH_REF", "params": {"period": 14}},
                            "operator": "GREATER_THAN",
                            "rhs": {"type": "NUMBER", "value": 100}
                        }
                    ]
                },
                "candidate_conditions": {
                    "id": "candidate.group.0",
                    "type": "AND",
                    "conditions": []
                },
                "action": {
                    "type": "PAPER_TRADE",
                    "risk_config": {
                        "max_position_size": 10000,
                        "stop_loss_pct": 2,
                        "take_profit_pct": 5,
                        "validity_window": 5
                    }
                }
            }
        )
        db.add(starter_strategy)
        db.flush()

        # Create starter paper account for editor
        from decimal import Decimal
        from src.services.paper_service import PaperService
        starter_account = PaperService.create_account(
            db,
            owner_id=editor_user.id,
            name="Primary Paper Account",
            initial_balance=Decimal("100000.00"),
            currency="INR"
        )

        manifest["accounts"] = {
            "starter": {
                "id": str(starter_account.id),
                "owner_id": str(editor_user.id),
                "name": starter_account.name,
                "available_cash": "100000.00"
            }
        }

        # Create starter orchestration runtime for editor in READY status
        import datetime
        import uuid
        from src.models import (
            ProviderConnection,
            ProviderInstrumentMapping,
            StrategyActionPolicy,
            RiskPolicy,
            StrategyRuntime,
        )

        action_policy_payload = {
            "action_mappings": [
                {
                    "mapping_id": "act_e2e_buy",
                    "instrument_id": "NIFTY_23000_PE",
                    "side": "BUY",
                    "order_type": "MARKET",
                    "quantity_units": 1,
                    "time_in_force": "DAY",
                }
            ]
        }
        action_pol = StrategyActionPolicy(
            id=str(uuid.uuid4()),
            owner_id=editor_user.id,
            strategy_id=starter_strategy.id,
            name="E2E Starter Action Policy",
            version=1,
            payload=action_policy_payload,
        )
        db.add(action_pol)

        risk_policy_payload = {
            "risk_config": {
                "max_notional_per_order": 500000000,
                "max_open_orders": 5,
                "max_open_positions": 5,
                "max_trades_per_day": 10,
            }
        }
        risk_pol = RiskPolicy(
            id=str(uuid.uuid4()),
            owner_id=editor_user.id,
            name="E2E Starter Risk Policy",
            version=1,
            payload=risk_policy_payload,
        )
        db.add(risk_pol)
        db.flush()

        conn = ProviderConnection(
            id=str(uuid.uuid4()),
            owner_id=editor_user.id,
            provider_name="UPSTOX",
            environment="SANDBOX",
            credential_reference="e2e_cred_ref",
            credential_version="v1",
            status="CONFIGURED",
        )
        db.add(conn)
        db.flush()

        mapping = ProviderInstrumentMapping(
            id=str(uuid.uuid4()),
            owner_id=editor_user.id,
            tradepro_instrument_id="synthetic_candidate_option_pe_23000_15m",
            provider_instrument_token="256265",
            exchange="NSE",
            segment="OPTION",
            symbol="NIFTY_23000_PE",
            lot_size_units=1,
            tick_size_units=5,
            freeze_quantity_units=1800,
            verification_status="VERIFIED",
            mapping_version=1,
        )
        db.add(mapping)
        db.flush()

        t_open = datetime.datetime(2026, 8, 28, 9, 15, tzinfo=datetime.timezone.utc)
        t_close = datetime.datetime(2026, 8, 28, 15, 30, tzinfo=datetime.timezone.utc)
        timeframe = "15m"

        starter_runtime = StrategyRuntime(
            id=str(uuid.uuid4()),
            owner_id=editor_user.id,
            strategy_id=starter_strategy.id,
            account_id=starter_account.id,
            action_policy_id=action_pol.id,
            risk_policy_id=risk_pol.id,
            dataset_id="synthetic_candidate_option_pe_23000_15m",
            timeframe=timeframe,
            trading_mode="PAPER",
            status="READY",
            version=1,
            strategy_snapshot=starter_strategy.payload,
            action_policy_snapshot=action_policy_payload,
            risk_policy_snapshot=risk_policy_payload,
            instrument_spec_snapshot={
                "instrument_id": "synthetic_candidate_option_pe_23000_15m",
                "price_scale": 2,
                "lot_size_units": 1,
                "tick_size_units": 5,
            },
        )
        db.add(starter_runtime)
        db.flush()

        db.commit()

        # Write manifest file to protected temp directory
        if manifest_path:
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2)

        return manifest

    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
        engine.dispose()

if __name__ == "__main__":
    try:
        res = seed_e2e_database()
        print(f"E2E Database seeded successfully with {len(res['users'])} test accounts.")
    except Exception as e:
        print(f"Error seeding E2E database: {e}", file=sys.stderr)
        sys.exit(1)
