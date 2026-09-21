import sys
import getpass
import argparse
from typing import Optional, List
from sqlalchemy.orm import Session
from src.database import SessionLocal
from src.models import User, LEGACY_PRINCIPAL_ID
from src.auth.normalization import normalize_username, normalize_email
from src.auth.security import hash_password, validate_password_length
from src.auth.session import revoke_all_user_sessions, cleanup_sessions
from src.auth.admin_service import check_last_admin_protection, transfer_legacy_resources

def get_db() -> Session:
    return SessionLocal()

def prompt_password() -> str:
    """Securely prompts for password with confirmation via getpass. Never accepts CLI argument or env var."""
    pwd1 = getpass.getpass("Enter password: ")
    validate_password_length(pwd1)
    pwd2 = getpass.getpass("Confirm password: ")
    if pwd1 != pwd2:
        print("Error: Passwords do not match.", file=sys.stderr)
        sys.exit(1)
    return pwd1

def cmd_create_admin(args):
    disp_user, norm_user = normalize_username(args.username)
    disp_email, norm_email = normalize_email(args.email)
    password = prompt_password()
    pwd_hash = hash_password(password)

    db = get_db()
    try:
        existing = db.query(User).filter(
            (User.normalized_username == norm_user) | (User.normalized_email == norm_email)
        ).first()
        if existing:
            print(f"Error: User with username '{disp_user}' or email '{disp_email}' already exists.", file=sys.stderr)
            sys.exit(1)

        user = User(
            username=disp_user,
            normalized_username=norm_user,
            email=disp_email,
            normalized_email=norm_email,
            hashed_password=pwd_hash,
            role="ADMIN",
            is_active=True
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        print(f"Successfully created administrator: {user.username} ({user.email}) [ID: {user.id}]")
    finally:
        db.close()

def cmd_create_user(args):
    role = args.role.upper()
    if role not in ("VIEWER", "EDITOR", "ADMIN"):
        print(f"Error: Invalid role '{args.role}'. Must be VIEWER, EDITOR, or ADMIN.", file=sys.stderr)
        sys.exit(1)

    disp_user, norm_user = normalize_username(args.username)
    disp_email, norm_email = normalize_email(args.email)
    password = prompt_password()
    pwd_hash = hash_password(password)

    db = get_db()
    try:
        existing = db.query(User).filter(
            (User.normalized_username == norm_user) | (User.normalized_email == norm_email)
        ).first()
        if existing:
            print(f"Error: User with username '{disp_user}' or email '{disp_email}' already exists.", file=sys.stderr)
            sys.exit(1)

        user = User(
            username=disp_user,
            normalized_username=norm_user,
            email=disp_email,
            normalized_email=norm_email,
            hashed_password=pwd_hash,
            role=role,
            is_active=True
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        print(f"Successfully created user: {user.username} ({user.email}) [{user.role}] [ID: {user.id}]")
    finally:
        db.close()

def cmd_list_users(args):
    db = get_db()
    try:
        users = db.query(User).order_by(User.created_at.asc()).all()
        print(f"{'ID':<38} {'Username':<20} {'Email':<30} {'Role':<10} {'Active':<8} {'Created At'}")
        print("-" * 125)
        for u in users:
            print(f"{u.id:<38} {u.username:<20} {u.email:<30} {u.role:<10} {str(u.is_active):<8} {u.created_at.strftime('%Y-%m-%d %H:%M:%S')}")
    finally:
        db.close()

def cmd_reset_password(args):
    _, norm_user = normalize_username(args.username)
    db = get_db()
    try:
        user = db.query(User).filter(User.normalized_username == norm_user).first()
        if not user or user.id == LEGACY_PRINCIPAL_ID:
            print(f"Error: User '{args.username}' not found or is legacy principal.", file=sys.stderr)
            sys.exit(1)

        password = prompt_password()
        user.hashed_password = hash_password(password)
        db.commit()

        # Revoke all active sessions
        revoked = revoke_all_user_sessions(db, user.id)
        print(f"Successfully reset password for user '{user.username}'. Revoked {revoked} active session(s).")
    finally:
        db.close()

def cmd_set_status(args):
    _, norm_user = normalize_username(args.username)
    active_bool = args.active.lower() in ("true", "1", "yes")

    db = get_db()
    try:
        user = db.query(User).filter(User.normalized_username == norm_user).first()
        if not user or user.id == LEGACY_PRINCIPAL_ID:
            print(f"Error: User '{args.username}' not found or is legacy principal.", file=sys.stderr)
            sys.exit(1)

        # Concurrency-safe last admin check
        check_last_admin_protection(db, user, new_active_status=active_bool)

        user.is_active = active_bool
        db.commit()

        if not active_bool:
            revoked = revoke_all_user_sessions(db, user.id)
            print(f"Deactivated user '{user.username}'. Revoked {revoked} active session(s).")
        else:
            print(f"Activated user '{user.username}'.")
    finally:
        db.close()

def cmd_cleanup_sessions(args):
    db = get_db()
    try:
        deleted = cleanup_sessions(db, retention_days=args.retention_days)
        print(f"Session cleanup completed. Deleted {deleted} expired/revoked session record(s) older than {args.retention_days} day(s).")
    finally:
        db.close()

def cmd_transfer_legacy(args):
    _, norm_user = normalize_username(args.target_username)
    resource_ids = [r.strip() for r in args.resource_ids.split(",") if r.strip()]
    if not resource_ids or len(resource_ids) > 100:
        print("Error: --resource-ids must contain between 1 and 100 comma-separated IDs.", file=sys.stderr)
        sys.exit(1)

    db = get_db()
    try:
        user = db.query(User).filter(User.normalized_username == norm_user).first()
        if not user or not user.is_active or user.id == LEGACY_PRINCIPAL_ID:
            print(f"Error: Target user '{args.target_username}' not found, inactive, or is legacy principal.", file=sys.stderr)
            sys.exit(1)

        transferred, rejected = transfer_legacy_resources(
            db=db,
            target_user_id=user.id,
            resource_type=args.resource_type.upper(),
            resource_ids=resource_ids
        )
        print(f"Legacy resource transfer completed: {transferred} transferred, {rejected} rejected/not found.")
    finally:
        db.close()

import math
import re

def validate_worker_args(args):
    """Strictly validate bounded worker parameters, rejecting extreme values, NaN, and Infinity."""
    # Batch size: integer, 1 to 100
    if args.batch_size is None or not isinstance(args.batch_size, int) or isinstance(args.batch_size, bool) or not (1 <= args.batch_size <= 100):
        print("Error: --batch-size must be an integer between 1 and 100.", file=sys.stderr)
        sys.exit(2)

    # Lease duration: integer, 5 to 300 seconds
    if args.lease_duration is None or not isinstance(args.lease_duration, int) or isinstance(args.lease_duration, bool) or not (5 <= args.lease_duration <= 300):
        print("Error: --lease-duration must be an integer between 5 and 300 seconds.", file=sys.stderr)
        sys.exit(2)

    # Poll interval: float/int, 0.1 to 60.0 seconds; reject NaN and Inf
    if (
        args.poll_interval is None
        or not isinstance(args.poll_interval, (int, float))
        or isinstance(args.poll_interval, bool)
        or math.isnan(args.poll_interval)
        or math.isinf(args.poll_interval)
        or not (0.1 <= args.poll_interval <= 60.0)
    ):
        print("Error: --poll-interval must be a valid number between 0.1 and 60.0 seconds.", file=sys.stderr)
        sys.exit(2)

    # Max runs: None (continuous mode) or integer, 1 to 10000
    if args.max_runs is not None:
        if (
            not isinstance(args.max_runs, int)
            or isinstance(args.max_runs, bool)
            or not (1 <= args.max_runs <= 10000)
        ):
            print("Error: --max-runs must be an integer between 1 and 10000, or omitted for continuous mode.", file=sys.stderr)
            sys.exit(2)

    # Worker ID: optional string, 1 to 50 chars matching ^[A-Za-z0-9_.-]+$
    if args.worker_id is not None:
        if (
            not isinstance(args.worker_id, str)
            or not (1 <= len(args.worker_id) <= 50)
            or not re.match(r"^[A-Za-z0-9_.-]+$", args.worker_id)
        ):
            print("Error: --worker-id must be 1-50 characters matching ^[A-Za-z0-9_.-]+$.", file=sys.stderr)
            sys.exit(2)

def cmd_sandbox_worker(args):
    validate_worker_args(args)
    from src.engine.sandbox.outbox_worker import SandboxOutboxWorker
    worker = SandboxOutboxWorker(
        worker_id=args.worker_id,
        batch_size=args.batch_size,
        lease_duration_seconds=args.lease_duration,
        poll_interval_seconds=args.poll_interval,
    )
    worker.run(max_runs=args.max_runs)

def cmd_evaluation_worker(args):
    import os
    app_env = os.environ.get("APP_ENV", "development").lower()
    if app_env == "production":
        print("Error: Strategy evaluation worker execution is strictly prohibited in production.", file=sys.stderr)
        sys.exit(1)

    # In test mode, guard disposable database target
    if app_env == "test":
        db_url = os.environ.get("DATABASE_URL", "")
        from src.database_safety import reject_development_test_target, require_disposable_target
        reject_development_test_target(db_url)
        require_disposable_target(db_url)

    # Validate argument bounds
    validate_worker_args(args)

    from src.engine.orchestration.worker import StrategyEvaluationWorker
    worker = StrategyEvaluationWorker(
        worker_id=args.worker_id,
        batch_size=args.batch_size,
        lease_duration_seconds=args.lease_duration,
        poll_interval_seconds=args.poll_interval,
    )
    worker.run(max_runs=args.max_runs)

def main():
    parser = argparse.ArgumentParser(description="TradePro Administrative CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # sandbox-outbox-worker
    worker_p = subparsers.add_parser("sandbox-outbox-worker", help="Run the Upstox sandbox outbox transmission worker")
    worker_p.add_argument("--batch-size", type=int, default=10, help="Batch size for claiming outbox records")
    worker_p.add_argument("--lease-duration", type=int, default=30, help="Lease duration in seconds")
    worker_p.add_argument("--poll-interval", type=float, default=1.0, help="Poll interval in seconds")
    worker_p.add_argument("--worker-id", default=None, help="Explicit worker ID identifier")
    worker_p.add_argument("--max-runs", type=int, default=None, help="Maximum worker loop iterations (for testing)")
    worker_p.set_defaults(func=cmd_sandbox_worker)

    # strategy-evaluation-worker
    orch_worker_p = subparsers.add_parser("strategy-evaluation-worker", help="Run the Strategy Orchestration fixture replay evaluation worker")
    orch_worker_p.add_argument("--batch-size", type=int, default=10, help="Batch size for claiming orchestration runtimes")
    orch_worker_p.add_argument("--lease-duration", type=int, default=30, help="Lease duration in seconds")
    orch_worker_p.add_argument("--poll-interval", type=float, default=1.0, help="Poll interval in seconds")
    orch_worker_p.add_argument("--worker-id", default=None, help="Explicit worker ID identifier")
    orch_worker_p.add_argument("--max-runs", type=int, default=None, help="Maximum worker loop iterations (for testing)")
    orch_worker_p.set_defaults(func=cmd_evaluation_worker)

    # users group
    users_parser = subparsers.add_parser("users", help="User administration commands")
    user_subs = users_parser.add_subparsers(dest="subcommand", required=True)

    # create-admin
    admin_p = user_subs.add_parser("create-admin", help="Create an initial administrator")
    admin_p.add_argument("--username", required=True, help="Administrator username")
    admin_p.add_argument("--email", required=True, help="Administrator email")
    admin_p.set_defaults(func=cmd_create_admin)

    # create-user
    user_p = user_subs.add_parser("create-user", help="Create a user with specified role")
    user_p.add_argument("--username", required=True, help="Username")
    user_p.add_argument("--email", required=True, help="Email")
    user_p.add_argument("--role", default="VIEWER", choices=["VIEWER", "EDITOR", "ADMIN"], help="User role")
    user_p.set_defaults(func=cmd_create_user)

    # list
    list_p = user_subs.add_parser("list", help="List all registered users")
    list_p.set_defaults(func=cmd_list_users)

    # reset-password
    reset_p = user_subs.add_parser("reset-password", help="Reset a user password")
    reset_p.add_argument("--username", required=True, help="Username")
    reset_p.set_defaults(func=cmd_reset_password)

    # set-status
    status_p = user_subs.add_parser("set-status", help="Enable or disable a user account")
    status_p.add_argument("--username", required=True, help="Username")
    status_p.add_argument("--active", required=True, choices=["true", "false"], help="Active status")
    status_p.set_defaults(func=cmd_set_status)

    # sessions cleanup
    sess_parser = subparsers.add_parser("sessions", help="Session management commands")
    sess_subs = sess_parser.add_subparsers(dest="subcommand", required=True)
    cleanup_p = sess_subs.add_parser("cleanup", help="Purge expired and revoked sessions")
    cleanup_p.add_argument("--retention-days", type=int, default=7, help="Retention period in days for expired/revoked sessions")
    cleanup_p.set_defaults(func=cmd_cleanup_sessions)

    # transfers legacy
    trans_parser = subparsers.add_parser("transfers", help="Resource transfer commands")
    trans_subs = trans_parser.add_subparsers(dest="subcommand", required=True)
    legacy_p = trans_subs.add_parser("legacy", help="Transfer legacy principal resources to active user")
    legacy_p.add_argument("--target-username", required=True, help="Target username")
    legacy_p.add_argument("--resource-type", default="ALL", choices=["ALL", "STRATEGIES", "REPLAYS"], help="Resource type to transfer")
    legacy_p.add_argument("--resource-ids", required=True, help="Comma-separated list of up to 100 resource IDs")
    legacy_p.set_defaults(func=cmd_transfer_legacy)

    parsed_args = parser.parse_args()
    parsed_args.func(parsed_args)

if __name__ == "__main__":
    main()
