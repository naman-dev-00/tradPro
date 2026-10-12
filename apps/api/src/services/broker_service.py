"""Broker Service: OAuth 2.0 lifecycle, encrypted token storage, read-only snapshots, and live readiness evaluation.

Guarantees:
- Single-use, owner/session-bound OAuth state with atomic consumption before HTTP token exchange.
- Zero plaintext tokens/secrets in logs, responses, or review bundles.
- Remote HTTP fetching performed without holding database locks.
- Version and credential_generation rechecks before persisting snapshots.
- Invalidation of old active snapshots on disconnect/reconnect.
- Multi-gate structured readiness check with live_dispatch_enabled hardcoded to False.
- Strict isolation from internal paper accounting.
"""
import datetime
import ipaddress
import json
import logging
import os
import secrets
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

from sqlalchemy import func, or_, and_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.engine.broker.secret_store import secret_store
from src.engine.broker.upstox_read_adapter import (
    BrokerAuthError,
    BrokerRateLimitError,
    BrokerTransportError,
    UpstoxReadAdapter,
)
from src.engine.market_data.market_schedule import (
    MarketSessionStatus,
    get_market_session_status,
)
from src.models import (
    BrokerSnapshot,
    KillSwitch,
    OAuthStateRecord,
    ProviderConnection,
    ReconciliationRecord,
)

logger = logging.getLogger("tradepro.broker.service")

DEFAULT_ALLOWED_REDIRECT_URIS = [
    "http://localhost:3000/broker-readiness",
    "http://127.0.0.1:3000/broker-readiness",
]


class BrokerServiceError(Exception):
    """Base exception for broker operations."""
    pass


class BrokerNotConfiguredError(BrokerServiceError):
    pass


class BrokerUnsupportedCapabilityError(BrokerServiceError):
    pass


class InvalidOAuthStateError(BrokerServiceError):
    pass


class OAuthReplayError(BrokerServiceError):
    pass


class OAuthExpiredError(BrokerServiceError):
    pass


class OAuthSecurityError(BrokerServiceError):
    pass


class BrokerSecurityError(BrokerServiceError):
    pass


class BrokerConflictError(BrokerServiceError):
    pass


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _compute_next_upstox_expiry_utc(reference_time_utc: Optional[datetime.datetime] = None) -> datetime.datetime:
    """
    Computes token expiry at 03:30 AM IST of the following trading morning.
    IST is UTC + 05:30. 03:30 AM IST corresponds to 22:00 UTC (previous day calendar date).
    """
    ref = reference_time_utc or _now_utc()
    ist_offset = datetime.timedelta(hours=5, minutes=30)
    ist_now = ref + ist_offset

    candidate_ist = ist_now.replace(hour=3, minute=30, second=0, microsecond=0)
    if candidate_ist <= ist_now:
        candidate_ist += datetime.timedelta(days=1)

    candidate_utc = candidate_ist - ist_offset
    return candidate_utc


class BrokerService:
    """Manages secure Upstox OAuth connections, encrypted tokens, snapshots, and readiness."""

    @staticmethod
    def generate_authorization_url(
        db: Session,
        owner_id: str,
        session_id: str,
        environment: str = "LIVE",
        client_ip: Optional[str] = None,
        redirect_uri_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Generates a single-use, owner/session-bound OAuth state and authorization URL.
        Rejects sandbox environment and un-registered redirect URIs.
        """
        if environment.upper() == "SANDBOX":
            raise BrokerUnsupportedCapabilityError(
                "Upstox Sandbox does not support OAuth authentication or live portfolio capabilities."
            )

        client_id = os.environ.get("UPSTOX_CLIENT_ID") or os.environ.get("UPSTOX_LIVE_CLIENT_ID", "")
        if not client_id.strip():
            raise BrokerNotConfiguredError("Upstox client credentials are not configured on this server.")

        # Allowed redirect URIs validation
        raw_allowed = os.environ.get("UPSTOX_ALLOWED_REDIRECT_URIS", "").strip()
        if raw_allowed:
            allowed_uris = [u.strip() for u in raw_allowed.split(",") if u.strip()]
        else:
            env_redirect = os.environ.get("UPSTOX_REDIRECT_URI", "").strip()
            allowed_uris = [env_redirect] if env_redirect else DEFAULT_ALLOWED_REDIRECT_URIS

        redirect_uri = (redirect_uri_override or os.environ.get("UPSTOX_REDIRECT_URI") or allowed_uris[0]).strip()
        if redirect_uri not in allowed_uris:
            raise OAuthSecurityError(f"Redirect URI '{redirect_uri}' is not in the registered allowlist.")

        # Generate 32 bytes cryptographically random state (64 hex characters)
        state_token = secrets.token_hex(32)
        now = _now_utc()
        expires_at = now + datetime.timedelta(seconds=300)  # 5-minute TTL

        # Persistent authorization fence covering initiation
        conn = db.query(ProviderConnection).filter(
            ProviderConnection.owner_id == owner_id,
            ProviderConnection.provider_name == "UPSTOX",
            ProviderConnection.environment == environment,
        ).with_for_update().first()

        if not conn:
            conn = ProviderConnection(
                owner_id=owner_id,
                provider_name="UPSTOX",
                environment=environment,
                status="NOT_CONFIGURED",
                version=1,
                credential_generation=1,
                created_at=now,
                updated_at=now,
            )
            db.add(conn)
            db.flush()

        auth_fence = secrets.token_hex(16)
        conn.authorization_fence = auth_fence
        conn.updated_at = now

        # Invalidate any older unconsumed states for this owner & environment
        db.query(OAuthStateRecord).filter(
            OAuthStateRecord.owner_id == owner_id,
            OAuthStateRecord.environment == environment,
            OAuthStateRecord.consumed == False,
        ).update({"consumed": True, "consumed_at": now}, synchronize_session=False)

        oauth_state = OAuthStateRecord(
            state_token=state_token,
            owner_id=owner_id,
            session_id=session_id,
            environment=environment,
            redirect_uri=redirect_uri,
            client_ip=client_ip,
            authorization_fence=auth_fence,
            credential_generation=conn.credential_generation,
            consumed=False,
            expires_at=expires_at,
            created_at=now,
        )
        db.add(oauth_state)
        db.commit()

        auth_url = (
            f"https://api.upstox.com/v2/login/authorization/dialog"
            f"?response_type=code"
            f"&client_id={quote(client_id.strip(), safe='')}"
            f"&redirect_uri={quote(redirect_uri.strip(), safe='')}"
            f"&state={state_token}"
        )

        return {
            "authorization_url": auth_url,
            "state": state_token,
            "environment": environment,
            "expires_in_seconds": 300,
        }

    @staticmethod
    def handle_oauth_callback(
        db: Session,
        code: str,
        state_token: str,
        current_user_id: str,
        current_session_id: str,
        adapter: Optional[UpstoxReadAdapter] = None,
    ) -> Dict[str, Any]:
        """
        Atomically consumes the single-use OAuth state, exchanges code for token outside DB tx,
        and saves provider-confirmed broker UCC bound to the local user in a fresh transaction.
        """
        now = _now_utc()

        # Step 1: Durably claim OAuth state in its own transaction before HTTP exchange
        oauth_state = db.query(OAuthStateRecord).filter(
            OAuthStateRecord.state_token == state_token
        ).with_for_update().first()

        if not oauth_state:
            raise InvalidOAuthStateError("Invalid or unrecognized OAuth state token.")

        if oauth_state.consumed:
            raise OAuthReplayError("OAuth state token has already been consumed (replay rejected).")

        if oauth_state.expires_at < now:
            raise OAuthExpiredError("OAuth state token has expired.")

        if oauth_state.owner_id != current_user_id:
            raise OAuthSecurityError("OAuth state owner mismatch: callback initiated by different user.")

        if oauth_state.session_id != current_session_id:
            raise OAuthSecurityError("OAuth state session mismatch: callback must complete in original session.")

        if oauth_state.environment.upper() == "SANDBOX":
            raise BrokerUnsupportedCapabilityError(
                "Upstox Sandbox does not support OAuth authentication or live portfolio capabilities."
            )

        # Capture all required primitive state values before committing consumption
        state_id = oauth_state.id
        state_redirect_uri = oauth_state.redirect_uri
        state_fence = oauth_state.authorization_fence
        state_generation = oauth_state.credential_generation
        state_env = oauth_state.environment
        state_owner_id = oauth_state.owner_id

        # Durably mark consumed before token exchange so failures/replays never reopen state
        oauth_state.consumed = True
        oauth_state.consumed_at = now
        db.commit()

        # Step 2: Server-side token exchange outside DB transaction
        # Assert db.in_transaction() is False at the actual token-exchange boundary
        assert not db.in_transaction(), "Database transaction must not be open during token exchange"

        client_id = os.environ.get("UPSTOX_CLIENT_ID") or os.environ.get("UPSTOX_LIVE_CLIENT_ID", "")
        client_secret = os.environ.get("UPSTOX_CLIENT_SECRET") or os.environ.get("UPSTOX_LIVE_CLIENT_SECRET", "")
        if not client_id.strip() or not client_secret.strip():
            raise BrokerNotConfiguredError("Server missing UPSTOX_CLIENT_ID or UPSTOX_CLIENT_SECRET.")

        read_adapter = adapter or UpstoxReadAdapter()
        token_data = read_adapter.exchange_code_for_token(
            code=code,
            client_id=client_id.strip(),
            client_secret=client_secret.strip(),
            redirect_uri=state_redirect_uri,
        )

        access_token = token_data.get("access_token")
        broker_user_id = token_data.get("user_id")
        account_name = token_data.get("user_name")
        broker_user_type = token_data.get("user_type")

        if not access_token or not broker_user_id:
            raise BrokerTransportError("Provider returned incomplete authorization credentials.")

        # Step 3: Begin fresh transaction to verify constraints and bind connection
        db.expire_all()

        # Verify expected operator UCC if configured
        expected_ucc = os.environ.get("UPSTOX_EXPECTED_UCC", "").strip()
        if expected_ucc and expected_ucc != broker_user_id:
            raise OAuthSecurityError(
                "Connected broker UCC does not match configured operator UCC."
            )

        # Atomic persisted authorization fence check: verify connection exists and fence matches
        conn = db.query(ProviderConnection).filter(
            ProviderConnection.owner_id == current_user_id,
            ProviderConnection.provider_name == "UPSTOX",
            ProviderConnection.environment == state_env,
        ).with_for_update().first()

        if (
            not conn
            or conn.authorization_fence is None
            or conn.authorization_fence != state_fence
            or conn.credential_generation != state_generation
        ):
            raise OAuthSecurityError("Authorization attempt was superseded, disconnected, or invalidated.")

        # Pre-check active conflict on another user
        existing_other = db.query(ProviderConnection).filter(
            ProviderConnection.broker_user_id == broker_user_id,
            ProviderConnection.owner_id != current_user_id,
            ProviderConnection.environment == state_env,
            ProviderConnection.status == "CONNECTED",
        ).first()
        if existing_other:
            raise BrokerConflictError("This broker account is already connected to another user.")

        # Encrypt access token at rest
        encrypted_token = secret_store.encrypt(access_token)
        token_expires_at = _compute_next_upstox_expiry_utc(now)

        is_initial_connect = (conn.status in ("NOT_CONFIGURED", "DISCONNECTED") and conn.encrypted_access_token is None)

        conn.status = "CONNECTED"
        conn.authorization_fence = None  # Fence successfully consumed!
        conn.broker_user_id = broker_user_id
        conn.account_name = account_name
        conn.broker_user_type = broker_user_type
        conn.encrypted_access_token = encrypted_token
        conn.token_acquired_at = now
        conn.token_expires_at = token_expires_at
        conn.token_scope = "read_only_readiness"
        conn.sanitized_error_code = None
        conn.version += 1
        if is_initial_connect and (conn.credential_generation or 1) == 1:
            conn.credential_generation = 1
        else:
            conn.credential_generation = (conn.credential_generation or 0) + 1
        conn.updated_at = now

        try:
            db.flush()
            # Invalidate active snapshots on reconnect
            db.query(BrokerSnapshot).filter(
                BrokerSnapshot.owner_id == current_user_id,
                BrokerSnapshot.connection_id == conn.id,
                BrokerSnapshot.status == "ACTIVE",
            ).update({"status": "SUPERSEDED"}, synchronize_session=False)
            db.commit()
        except IntegrityError:
            db.rollback()
            raise BrokerConflictError("This broker account is already connected to another user.") from None

        return {
            "provider": "UPSTOX",
            "environment": state_env,
            "status": "CONNECTED",
            "broker_user_id_masked": secret_store.mask_account_id(broker_user_id),
            "account_name": account_name,
            "token_valid": True,
            "token_expires_at": token_expires_at,
            "last_successful_refresh_at": conn.last_successful_refresh_at,
            "version": conn.version,
            "credential_generation": conn.credential_generation,
        }

    @staticmethod
    def get_connection_status(
        db: Session,
        owner_id: str,
        environment: str = "LIVE",
    ) -> Dict[str, Any]:
        """
        Pure read-only query returning sanitized connection metadata.
        Zero writes, additions, flushes, or commits.
        """
        conn = db.query(ProviderConnection).filter(
            ProviderConnection.owner_id == owner_id,
            ProviderConnection.provider_name == "UPSTOX",
            ProviderConnection.environment == environment,
        ).first()

        if not conn:
            return {
                "provider": "UPSTOX",
                "environment": environment,
                "status": "NOT_CONFIGURED",
                "broker_user_id_masked": None,
                "account_name": None,
                "token_valid": False,
                "token_expires_at": None,
                "last_successful_refresh_at": None,
                "version": 0,
                "credential_generation": 1,
            }

        now = _now_utc()
        has_valid_expiry = conn.token_expires_at is not None
        is_expired = not has_valid_expiry or (now >= conn.token_expires_at)
        effective_status = "EXPIRED" if (is_expired and conn.status == "CONNECTED") else conn.status
        token_valid = (
            (conn.status == "CONNECTED")
            and has_valid_expiry
            and (now < conn.token_expires_at)
            and bool(conn.encrypted_access_token)
        )

        return {
            "provider": conn.provider_name,
            "environment": conn.environment,
            "status": effective_status,
            "broker_user_id_masked": secret_store.mask_account_id(conn.broker_user_id) if conn.broker_user_id else None,
            "account_name": conn.account_name,
            "token_valid": token_valid,
            "token_expires_at": conn.token_expires_at,
            "last_successful_refresh_at": conn.last_successful_refresh_at,
            "version": conn.version,
            "credential_generation": conn.credential_generation or 1,
        }

    @staticmethod
    def disconnect(
        db: Session,
        owner_id: str,
        environment: str = "LIVE",
    ) -> Dict[str, Any]:
        """
        Disconnects the broker connection, bumps credential_generation,
        invalidates active snapshots, clears authorization fence,
        and invalidates any pending or in-flight authorization states.
        """
        conn = db.query(ProviderConnection).filter(
            ProviderConnection.owner_id == owner_id,
            ProviderConnection.provider_name == "UPSTOX",
            ProviderConnection.environment == environment,
        ).with_for_update().first()

        now = _now_utc()
        if not conn:
            conn = ProviderConnection(
                owner_id=owner_id,
                provider_name="UPSTOX",
                environment=environment,
                status="DISCONNECTED",
                credential_generation=2,
                version=1,
                created_at=now,
                updated_at=now,
            )
            db.add(conn)
        else:
            conn.status = "DISCONNECTED"
            conn.encrypted_access_token = None
            conn.authorization_fence = None
            conn.version += 1
            conn.credential_generation = (conn.credential_generation or 0) + 1
            conn.updated_at = now

        # Invalidate any pending or in-flight authorization states for this owner & environment
        db.query(OAuthStateRecord).filter(
            OAuthStateRecord.owner_id == owner_id,
            OAuthStateRecord.environment == environment,
            OAuthStateRecord.consumed == False,
        ).update({"consumed": True, "consumed_at": now}, synchronize_session=False)

        # Invalidate prior active snapshots on disconnect
        if conn.id:
            db.query(BrokerSnapshot).filter(
                BrokerSnapshot.owner_id == owner_id,
                BrokerSnapshot.connection_id == conn.id,
                BrokerSnapshot.status == "ACTIVE",
            ).update({"status": "SUPERSEDED"}, synchronize_session=False)

        db.commit()

        return {
            "provider": conn.provider_name,
            "environment": conn.environment,
            "status": "DISCONNECTED",
            "broker_user_id_masked": secret_store.mask_account_id(conn.broker_user_id),
            "account_name": conn.account_name,
            "token_valid": False,
            "token_expires_at": conn.token_expires_at,
            "last_successful_refresh_at": conn.last_successful_refresh_at,
            "version": conn.version,
            "credential_generation": conn.credential_generation,
        }

    @staticmethod
    def refresh_snapshots(
        db: Session,
        owner_id: str,
        environment: str = "LIVE",
        adapter: Optional[UpstoxReadAdapter] = None,
    ) -> Dict[str, Any]:
        """
        Explicit authenticated refresh action.
        Captures immutable fetch inputs, closes DB transaction, performs remote HTTP outside lock,
        and finalizes in a fresh transaction verifying connection version and credential_generation.
        """
        if environment.upper() == "SANDBOX":
            raise BrokerUnsupportedCapabilityError(
                "Upstox Sandbox does not support live snapshot retrieval."
            )

        # Step 1: Capture immutable fetch inputs
        conn = db.query(ProviderConnection).filter(
            ProviderConnection.owner_id == owner_id,
            ProviderConnection.provider_name == "UPSTOX",
            ProviderConnection.environment == environment,
        ).first()

        if not conn or conn.status != "CONNECTED" or not conn.encrypted_access_token:
            raise BrokerServiceError("No active broker connection available to refresh.")

        now = _now_utc()
        if conn.token_expires_at is None or now >= conn.token_expires_at:
            raise BrokerAuthError("Broker token has expired or is missing expiration timestamp. Daily reauthorization required.")

        connection_id = conn.id
        version_at_fetch = conn.version
        credential_generation_at_fetch = conn.credential_generation or 1
        expected_ucc = conn.broker_user_id
        encrypted_token = conn.encrypted_access_token

        # Decrypt token
        access_token = secret_store.decrypt(encrypted_token)

        # End database transaction before HTTP call
        db.rollback()

        # Step 2: Remote HTTP fetch outside DB transaction
        read_adapter = adapter or UpstoxReadAdapter()
        snapshot_payload = read_adapter.fetch_full_read_snapshot(access_token)

        # Confirm UCC matches
        fetched_ucc = snapshot_payload.get("profile", {}).get("user_id")
        if fetched_ucc != expected_ucc:
            raise BrokerSecurityError("Fetched broker UCC does not match connection UCC.")

        # Step 3: Finalize in a fresh transaction with authoritative version/generation checks
        db.expire_all()
        conn_for_update = db.query(ProviderConnection).filter(
            ProviderConnection.id == connection_id,
            ProviderConnection.owner_id == owner_id,
        ).with_for_update().first()

        if not conn_for_update or conn_for_update.status != "CONNECTED":
            raise BrokerConflictError("Connection state was disconnected or modified during snapshot fetch.")

        if conn_for_update.version != version_at_fetch:
            raise BrokerConflictError("Connection version changed concurrently during snapshot fetch.")

        if (conn_for_update.credential_generation or 1) != credential_generation_at_fetch:
            raise BrokerConflictError("Credential generation changed concurrently during snapshot fetch.")

        if conn_for_update.broker_user_id != expected_ucc:
            raise BrokerConflictError("Broker account UCC changed concurrently during snapshot fetch.")

        snapshot_time = _now_utc()

        # Validate required financial evidence completeness before persistence
        funds_payload = snapshot_payload.get("funds")
        equity_payload = funds_payload.get("equity") if isinstance(funds_payload, dict) else None
        if (
            not isinstance(funds_payload, dict)
            or not isinstance(equity_payload, dict)
            or "available_margin" not in equity_payload
            or equity_payload["available_margin"] is None
            or "used_margin" not in equity_payload
            or equity_payload["used_margin"] is None
            or not isinstance(snapshot_payload.get("holdings"), list)
            or not isinstance(snapshot_payload.get("positions"), list)
            or not isinstance(snapshot_payload.get("orders"), list)
        ):
            raise BrokerTransportError("Incomplete financial evidence in snapshot payload: missing required equity margins or portfolio structures.")

        # Mark prior snapshots as SUPERSEDED
        db.query(BrokerSnapshot).filter(
            BrokerSnapshot.owner_id == owner_id,
            BrokerSnapshot.connection_id == connection_id,
            BrokerSnapshot.status == "ACTIVE",
        ).update({"status": "SUPERSEDED"}, synchronize_session=False)

        # Insert new active snapshot with credential_generation binding
        new_snapshot = BrokerSnapshot(
            owner_id=owner_id,
            connection_id=connection_id,
            broker_user_id=expected_ucc,
            environment=environment,
            status="ACTIVE",
            acquired_at_utc=snapshot_time,
            funds_and_margins_json=snapshot_payload.get("funds"),
            holdings_json=snapshot_payload.get("holdings"),
            positions_json=snapshot_payload.get("positions"),
            orders_json=snapshot_payload.get("orders"),
            raw_fingerprint=snapshot_payload.get("fingerprint"),
            connection_version_at_fetch=version_at_fetch,
            credential_generation=credential_generation_at_fetch,
            created_at=snapshot_time,
        )
        db.add(new_snapshot)

        conn_for_update.last_successful_refresh_at = snapshot_time
        conn_for_update.version += 1
        conn_for_update.updated_at = snapshot_time
        db.commit()

        # Extract fund numbers
        funds = snapshot_payload.get("funds", {})
        equity_funds = funds.get("equity", {})
        avail_margin = None
        used_margin = None
        if "available_margin" in equity_funds and equity_funds["available_margin"] is not None:
            avail_margin = str(equity_funds["available_margin"])
        if "used_margin" in equity_funds and equity_funds["used_margin"] is not None:
            used_margin = str(equity_funds["used_margin"])

        return {
            "snapshot_id": new_snapshot.id,
            "broker_user_id_masked": secret_store.mask_account_id(expected_ucc),
            "environment": environment,
            "acquired_at_utc": snapshot_time,
            "freshness_seconds": 0,
            "raw_fingerprint": new_snapshot.raw_fingerprint,
            "available_margin": avail_margin,
            "used_margin": used_margin,
            "positions_count": len(snapshot_payload.get("positions", [])),
            "holdings_count": len(snapshot_payload.get("holdings", [])),
            "orders_count": len(snapshot_payload.get("orders", [])),
            "credential_generation": credential_generation_at_fetch,
        }

    @staticmethod
    def get_latest_snapshot(
        db: Session,
        owner_id: str,
        environment: str = "LIVE",
    ) -> Optional[Dict[str, Any]]:
        """
        Pure read-only query returning latest active broker snapshot bound to current connection.
        Zero writes, flushes, or commits.
        """
        conn = db.query(ProviderConnection).filter(
            ProviderConnection.owner_id == owner_id,
            ProviderConnection.provider_name == "UPSTOX",
            ProviderConnection.environment == environment,
        ).first()

        query = db.query(BrokerSnapshot).filter(
            BrokerSnapshot.owner_id == owner_id,
            BrokerSnapshot.environment == environment,
            BrokerSnapshot.status == "ACTIVE",
        )
        if conn and conn.id:
            query = query.filter(
                BrokerSnapshot.connection_id == conn.id,
                BrokerSnapshot.broker_user_id == conn.broker_user_id,
                BrokerSnapshot.credential_generation == conn.credential_generation,
            )

        snapshot = query.order_by(BrokerSnapshot.acquired_at_utc.desc()).first()

        if not snapshot:
            return None

        now = _now_utc()
        freshness_seconds = max(0, int((now - snapshot.acquired_at_utc).total_seconds()))

        return {
            "id": snapshot.id,
            "broker_user_id_masked": secret_store.mask_account_id(snapshot.broker_user_id),
            "environment": snapshot.environment,
            "status": snapshot.status,
            "acquired_at_utc": snapshot.acquired_at_utc,
            "freshness_seconds": freshness_seconds,
            "raw_fingerprint": snapshot.raw_fingerprint,
            "funds_and_margins": snapshot.funds_and_margins_json,
            "holdings": snapshot.holdings_json,
            "positions": snapshot.positions_json,
            "orders": snapshot.orders_json,
            "credential_generation": snapshot.credential_generation or 1,
        }

    @staticmethod
    def evaluate_live_readiness(
        db: Session,
        owner_id: str,
        environment: str = "LIVE",
        clock_fn: Optional[Callable[[], datetime.datetime]] = None,
    ) -> Dict[str, Any]:
        """
        Pure read-only evaluation of live readiness prerequisites.
        Zero mutations, zero network calls. live_dispatch_enabled is strictly False for Phase 7A.
        """
        now = clock_fn() if clock_fn else _now_utc()
        gates = []
        actionable_blockers = []

        # Gate 1: Broker Connection & UCC Binding
        conn = db.query(ProviderConnection).filter(
            ProviderConnection.owner_id == owner_id,
            ProviderConnection.provider_name == "UPSTOX",
            ProviderConnection.environment == environment,
        ).first()

        ucc_masked = secret_store.mask_account_id(conn.broker_user_id) if conn and conn.broker_user_id else None

        if not conn:
            gates.append({
                "gate_name": "broker_connection",
                "status": "BLOCKED",
                "details": "No broker connection configured.",
                "actionable_remedy": "Connect to Upstox via OAuth 2.0 flow.",
            })
            actionable_blockers.append("Broker connection not established.")
        elif conn.status != "CONNECTED":
            gates.append({
                "gate_name": "broker_connection",
                "status": "BLOCKED",
                "details": f"Connection status is {conn.status}.",
                "actionable_remedy": "Authorize and reconnect Upstox account.",
            })
            actionable_blockers.append(f"Broker connection is in {conn.status} state.")
        elif not conn.broker_user_id:
            gates.append({
                "gate_name": "broker_connection",
                "status": "BLOCKED",
                "details": "Authoritative broker UCC client ID is missing.",
                "actionable_remedy": "Reconnect Upstox to verify account identity.",
            })
            actionable_blockers.append("Broker account identity unverified.")
        else:
            gates.append({
                "gate_name": "broker_connection",
                "status": "READY",
                "details": f"Connected to broker account {ucc_masked}.",
                "actionable_remedy": None,
            })

        # Gate 2: Token Freshness & Expiration (Reject missing token expiry)
        if not conn or not conn.encrypted_access_token:
            gates.append({
                "gate_name": "token_freshness",
                "status": "BLOCKED",
                "details": "No access token present.",
                "actionable_remedy": "Log in with Upstox credentials to acquire a token.",
            })
            actionable_blockers.append("Missing broker access token.")
        elif conn.token_expires_at is None:
            gates.append({
                "gate_name": "token_freshness",
                "status": "BLOCKED",
                "details": "Token expiration timestamp is missing.",
                "actionable_remedy": "Reauthorize broker to acquire valid unexpired token with expiration metadata.",
            })
            actionable_blockers.append("Broker token missing expiration timestamp.")
        elif now >= conn.token_expires_at:
            gates.append({
                "gate_name": "token_freshness",
                "status": "BLOCKED",
                "details": f"Token expired at {conn.token_expires_at.isoformat()}.",
                "actionable_remedy": "Upstox tokens expire daily (~03:30 AM IST). Reauthorize broker.",
            })
            actionable_blockers.append("Broker daily token expired.")
        else:
            gates.append({
                "gate_name": "token_freshness",
                "status": "READY",
                "details": "Daily broker token is valid and unexpired.",
                "actionable_remedy": None,
            })

        # Gate 3: Static IP Registration Evidence (Reject invalid IP syntax, stale/future, and unbound evidence)
        static_ip = os.environ.get("UPSTOX_STATIC_IP", "").strip()
        if not static_ip:
            gates.append({
                "gate_name": "static_ip_configuration",
                "status": "BLOCKED",
                "details": "No registered Static IP configured (NSE Circular INVG67858 requires registered Static IP).",
                "actionable_remedy": "Configure UPSTOX_STATIC_IP in server environment matching registered Upstox My Apps IP.",
            })
            actionable_blockers.append("Static IP registration evidence missing.")
        else:
            try:
                ipaddress.ip_address(static_ip)
                valid_ip_syntax = True
            except ValueError:
                valid_ip_syntax = False

            if not valid_ip_syntax:
                gates.append({
                    "gate_name": "static_ip_configuration",
                    "status": "BLOCKED",
                    "details": f"Invalid Static IP address syntax: '{static_ip}'.",
                    "actionable_remedy": "Configure a valid IPv4 or IPv6 address in UPSTOX_STATIC_IP.",
                })
                actionable_blockers.append("Invalid Static IP syntax.")
            else:
                raw_json = os.environ.get("UPSTOX_STATIC_IP_REGISTRATION_JSON", "").strip()
                evidence: Dict[str, Any] = {}
                if raw_json:
                    try:
                        parsed = json.loads(raw_json)
                        if isinstance(parsed, dict):
                            evidence = parsed
                    except Exception:
                        evidence = {}

                def _extract_str(val: Any) -> Optional[str]:
                    return val.strip() if isinstance(val, str) and val.strip() else None

                evidence_ucc = _extract_str(evidence.get("broker_user_id")) or _extract_str(os.environ.get("UPSTOX_STATIC_IP_UCC"))
                evidence_egress = (
                    _extract_str(evidence.get("egress_ip"))
                    or _extract_str(os.environ.get("UPSTOX_STATIC_IP_EGRESS"))
                    or _extract_str(os.environ.get("UPSTOX_EGRESS_IP"))
                )
                evidence_provenance = (
                    _extract_str(evidence.get("provenance"))
                    or _extract_str(os.environ.get("UPSTOX_STATIC_IP_PROVENANCE"))
                )
                evidence_validated_at = (
                    _extract_str(evidence.get("validated_at"))
                    or _extract_str(os.environ.get("UPSTOX_STATIC_IP_VALIDATED_AT"))
                    or _extract_str(os.environ.get("UPSTOX_STATIC_IP_VERIFIED_AT"))
                )

                SUPPORTED_PROVENANCES = {
                    "UPSTOX_PORTAL",
                    "UPSTOX_DEVELOPER_PORTAL",
                    "AUTHORITATIVE_PORTAL",
                    "MY_APPS_PORTAL",
                }

                if not evidence_provenance and not evidence_validated_at and not evidence_ucc and not evidence_egress:
                    gates.append({
                        "gate_name": "static_ip_configuration",
                        "status": "UNKNOWN",
                        "details": f"Static IP ({static_ip}) configured but lacks authoritative registration/egress evidence.",
                        "actionable_remedy": "Provide UPSTOX_STATIC_IP_REGISTRATION_JSON with UCC, egress IP, provenance, and validated timestamp.",
                    })
                    actionable_blockers.append("Static IP registration lacks authoritative evidence.")
                elif not evidence_provenance:
                    gates.append({
                        "gate_name": "static_ip_configuration",
                        "status": "UNKNOWN",
                        "details": "Static IP registration evidence lacks provenance source.",
                        "actionable_remedy": f"Provide provenance source ({', '.join(sorted(SUPPORTED_PROVENANCES))}).",
                    })
                    actionable_blockers.append("Static IP registration lacks provenance source.")
                elif evidence_provenance not in SUPPORTED_PROVENANCES:
                    gates.append({
                        "gate_name": "static_ip_configuration",
                        "status": "BLOCKED",
                        "details": f"Unsupported Static IP evidence provenance: '{evidence_provenance}'.",
                        "actionable_remedy": f"Provenance must be one of: {', '.join(sorted(SUPPORTED_PROVENANCES))}.",
                    })
                    actionable_blockers.append("Unsupported Static IP evidence provenance.")
                elif not evidence_validated_at:
                    gates.append({
                        "gate_name": "static_ip_configuration",
                        "status": "UNKNOWN",
                        "details": "Static IP registration evidence lacks validation timestamp.",
                        "actionable_remedy": "Provide ISO-8601 validation timestamp in static IP registration evidence.",
                    })
                    actionable_blockers.append("Static IP registration lacks validation timestamp.")
                elif not evidence_ucc:
                    gates.append({
                        "gate_name": "static_ip_configuration",
                        "status": "BLOCKED",
                        "details": "Static IP registration evidence lacks UCC account binding.",
                        "actionable_remedy": "Provide broker_user_id in static IP registration evidence matching connected account.",
                    })
                    actionable_blockers.append("Static IP evidence lacks UCC binding.")
                elif conn and conn.broker_user_id and evidence_ucc != conn.broker_user_id:
                    gates.append({
                        "gate_name": "static_ip_configuration",
                        "status": "BLOCKED",
                        "details": f"Static IP registration evidence UCC ({secret_store.mask_account_id(evidence_ucc)}) does not match connected broker account ({ucc_masked}).",
                        "actionable_remedy": "Ensure static IP evidence is bound to the currently connected broker account UCC.",
                    })
                    actionable_blockers.append("Static IP evidence account mismatch.")
                elif not evidence_egress:
                    gates.append({
                        "gate_name": "static_ip_configuration",
                        "status": "BLOCKED",
                        "details": "Static IP registration evidence lacks egress IP binding.",
                        "actionable_remedy": "Provide egress_ip in static IP registration evidence matching configured UPSTOX_STATIC_IP.",
                    })
                    actionable_blockers.append("Static IP evidence lacks egress IP binding.")
                else:
                    try:
                        ipaddress.ip_address(evidence_egress)
                        valid_egress_syntax = True
                    except ValueError:
                        valid_egress_syntax = False

                    if not valid_egress_syntax:
                        gates.append({
                            "gate_name": "static_ip_configuration",
                            "status": "BLOCKED",
                            "details": f"Invalid Static IP evidence egress IP syntax: '{evidence_egress}'.",
                            "actionable_remedy": "Provide valid IPv4 or IPv6 address for egress_ip in evidence.",
                        })
                        actionable_blockers.append("Invalid Static IP evidence egress syntax.")
                    elif evidence_egress != static_ip:
                        gates.append({
                            "gate_name": "static_ip_configuration",
                            "status": "BLOCKED",
                            "details": f"Static IP evidence egress IP ({evidence_egress}) does not match configured static IP ({static_ip}).",
                            "actionable_remedy": "Ensure static IP evidence egress IP matches UPSTOX_STATIC_IP.",
                        })
                        actionable_blockers.append("Static IP evidence egress mismatch.")
                    else:
                        try:
                            val_dt = datetime.datetime.fromisoformat(evidence_validated_at.replace("Z", "+00:00"))
                            if val_dt > now + datetime.timedelta(seconds=60):
                                gates.append({
                                    "gate_name": "static_ip_configuration",
                                    "status": "BLOCKED",
                                    "details": f"Static IP validation timestamp is future-dated ({evidence_validated_at}).",
                                    "actionable_remedy": "Ensure validation timestamp reflects authoritative past or current verification.",
                                })
                                actionable_blockers.append("Static IP validation timestamp is future-dated.")
                            elif now - val_dt > datetime.timedelta(days=30):
                                gates.append({
                                    "gate_name": "static_ip_configuration",
                                    "status": "BLOCKED",
                                    "details": "Static IP registration evidence is stale (>30 days old).",
                                    "actionable_remedy": "Revalidate Static IP registration on broker developer portal.",
                                })
                                actionable_blockers.append("Static IP registration evidence is stale.")
                            else:
                                masked_ucc = secret_store.mask_account_id(evidence_ucc)
                                gates.append({
                                    "gate_name": "static_ip_configuration",
                                    "status": "READY",
                                    "details": f"Registered Static IP ({static_ip}) verified with {evidence_provenance} evidence for account {masked_ucc} ({evidence_validated_at}).",
                                    "actionable_remedy": None,
                                })
                        except Exception:
                            gates.append({
                                "gate_name": "static_ip_configuration",
                                "status": "BLOCKED",
                                "details": "Unparseable Static IP validation timestamp.",
                                "actionable_remedy": "Provide valid ISO-8601 validation timestamp.",
                            })
                            actionable_blockers.append("Static IP validation timestamp unparseable.")

        # Gate 4: Market Session & Segment Evidence (Authoritative calendar integration)
        session_status = get_market_session_status(now)
        if session_status == MarketSessionStatus.OPEN:
            gates.append({
                "gate_name": "market_session_and_segment",
                "status": "READY",
                "details": "Active regular market session per verified NSE calendar evidence.",
                "actionable_remedy": None,
            })
        elif session_status == MarketSessionStatus.HOLIDAY:
            gates.append({
                "gate_name": "market_session_and_segment",
                "status": "BLOCKED",
                "details": "Exchange is closed for official trading holiday.",
                "actionable_remedy": "Live trading readiness is active during regular trading sessions.",
            })
            actionable_blockers.append("Exchange closed (holiday).")
        elif session_status == MarketSessionStatus.CLOSED:
            gates.append({
                "gate_name": "market_session_and_segment",
                "status": "BLOCKED",
                "details": "Exchange outside regular continuous trading hours or weekend.",
                "actionable_remedy": "Market hours are 09:15 to 15:30 IST on regular trading days.",
            })
            actionable_blockers.append("Outside regular market hours.")
        else:
            gates.append({
                "gate_name": "market_session_and_segment",
                "status": "UNKNOWN",
                "details": "Exchange session status unknown or unverified for current timestamp (fail closed).",
                "actionable_remedy": "Verify date in official NSE circular schedule.",
            })
            actionable_blockers.append("Exchange session status unknown (unverified calendar evidence).")

        # Gate 5: Account & Risk Snapshot Evidence (Bound to connection, identity, environment, generation, and completeness)
        latest_snapshot = None
        if conn and conn.id:
            latest_snapshot = db.query(BrokerSnapshot).filter(
                BrokerSnapshot.owner_id == owner_id,
                BrokerSnapshot.connection_id == conn.id,
                BrokerSnapshot.broker_user_id == conn.broker_user_id,
                BrokerSnapshot.environment == environment,
                BrokerSnapshot.status == "ACTIVE",
            ).order_by(BrokerSnapshot.acquired_at_utc.desc()).first()
        else:
            latest_snapshot = db.query(BrokerSnapshot).filter(
                BrokerSnapshot.owner_id == owner_id,
                BrokerSnapshot.environment == environment,
                BrokerSnapshot.status == "ACTIVE",
            ).order_by(BrokerSnapshot.acquired_at_utc.desc()).first()

        if not latest_snapshot:
            gates.append({
                "gate_name": "account_risk_evidence",
                "status": "BLOCKED",
                "details": "No broker portfolio snapshot acquired.",
                "actionable_remedy": "Trigger a broker snapshot refresh to ingest current balances and positions.",
            })
            actionable_blockers.append("Broker snapshot missing.")
        elif conn and latest_snapshot.credential_generation != (conn.credential_generation or 1):
            gates.append({
                "gate_name": "account_risk_evidence",
                "status": "BLOCKED",
                "details": (
                    f"Broker snapshot belongs to credential generation {latest_snapshot.credential_generation}, "
                    f"but active connection is at generation {conn.credential_generation or 1}."
                ),
                "actionable_remedy": "Trigger a snapshot refresh using the reauthenticated credentials.",
            })
            actionable_blockers.append("Broker snapshot belongs to superseded credential generation.")
        elif latest_snapshot.acquired_at_utc > now + datetime.timedelta(seconds=60):
            gates.append({
                "gate_name": "account_risk_evidence",
                "status": "BLOCKED",
                "details": "Broker snapshot has future-dated acquisition timestamp.",
                "actionable_remedy": "Ensure server time synchronization and trigger a fresh snapshot.",
            })
            actionable_blockers.append("Broker snapshot timestamp is future-dated.")
        elif (
            latest_snapshot.funds_and_margins_json is None
            or not isinstance(latest_snapshot.funds_and_margins_json, dict)
            or "equity" not in latest_snapshot.funds_and_margins_json
            or not isinstance(latest_snapshot.funds_and_margins_json.get("equity"), dict)
            or "available_margin" not in latest_snapshot.funds_and_margins_json["equity"]
            or latest_snapshot.funds_and_margins_json["equity"]["available_margin"] is None
            or "used_margin" not in latest_snapshot.funds_and_margins_json["equity"]
            or latest_snapshot.funds_and_margins_json["equity"]["used_margin"] is None
            or latest_snapshot.holdings_json is None
            or not isinstance(latest_snapshot.holdings_json, list)
            or latest_snapshot.positions_json is None
            or not isinstance(latest_snapshot.positions_json, list)
            or latest_snapshot.orders_json is None
            or not isinstance(latest_snapshot.orders_json, list)
            or not latest_snapshot.raw_fingerprint
        ):
            gates.append({
                "gate_name": "account_risk_evidence",
                "status": "BLOCKED",
                "details": "Broker snapshot data is incomplete or missing required portfolio/margin sections.",
                "actionable_remedy": "Trigger a snapshot refresh to acquire complete funds, holdings, positions, and orders.",
            })
            actionable_blockers.append("Broker snapshot data incomplete.")
        else:
            age_seconds = int((now - latest_snapshot.acquired_at_utc).total_seconds())
            if age_seconds > 900:  # 15 minutes stale
                gates.append({
                    "gate_name": "account_risk_evidence",
                    "status": "BLOCKED",
                    "details": f"Broker snapshot is stale ({age_seconds} seconds old; max allowable is 900s).",
                    "actionable_remedy": "Refresh broker snapshot to obtain current margin and positions.",
                })
                actionable_blockers.append("Broker snapshot stale (>15m).")
            else:
                gates.append({
                    "gate_name": "account_risk_evidence",
                    "status": "READY",
                    "details": f"Broker snapshot is fresh ({age_seconds}s old) and bound to generation {latest_snapshot.credential_generation}.",
                    "actionable_remedy": None,
                })

        # Gate 6: Kill Switches & Reconciliation Blockers
        active_kill_switch = db.query(KillSwitch).filter(
            KillSwitch.is_active.is_(True),
            or_(
                KillSwitch.scope == "GLOBAL",
                and_(KillSwitch.scope == "USER", KillSwitch.user_id == owner_id),
            ),
        ).first()

        unresolved_reconciliations = db.query(func.count(ReconciliationRecord.id)).filter(
            ReconciliationRecord.owner_id == owner_id,
            ReconciliationRecord.status == "OPEN",
        ).scalar() or 0

        if active_kill_switch:
            gates.append({
                "gate_name": "kill_switch_and_reconciliation",
                "status": "BLOCKED",
                "details": "Active kill switch engagement detected.",
                "actionable_remedy": "Clear user or global kill switch once emergency condition is resolved.",
            })
            actionable_blockers.append("Active kill switch engaged.")
        elif unresolved_reconciliations > 0:
            gates.append({
                "gate_name": "kill_switch_and_reconciliation",
                "status": "BLOCKED",
                "details": f"{unresolved_reconciliations} unresolved order reconciliation records pending.",
                "actionable_remedy": "Resolve outstanding reconciliation issues before enabling readiness.",
            })
            actionable_blockers.append(f"{unresolved_reconciliations} unresolved reconciliation records pending.")
        else:
            gates.append({
                "gate_name": "kill_switch_and_reconciliation",
                "status": "READY",
                "details": "Kill switches clear; zero unresolved reconciliation blockers.",
                "actionable_remedy": None,
            })

        has_blocked_gate = any(g["status"] in ("BLOCKED", "UNKNOWN") for g in gates)
        overall_status = "BLOCKED" if has_blocked_gate else "READY_FOR_MONITORING"

        return {
            "overall_status": overall_status,
            "live_dispatch_enabled": False,  # Hardcoded False for Phase 7A
            "environment": environment,
            "broker_user_id_masked": ucc_masked,
            "evaluated_at": now,
            "gates": gates,
            "actionable_blockers": actionable_blockers,
        }
