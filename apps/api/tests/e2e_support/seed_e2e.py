import os
import sys
import json
import secrets
import pathlib
from typing import Dict, Any
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Import models & auth hashing
from src.models import Base, User, Strategy
from src.auth.security import hash_password

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
                            "rhs": {"type": "NUMBER", "value": 100.0}
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
                        "stop_loss_pct": 2.0,
                        "take_profit_pct": 5.0,
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
