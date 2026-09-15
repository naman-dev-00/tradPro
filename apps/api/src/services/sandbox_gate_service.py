import datetime
import logging
import os
from typing import Dict, List, Optional, Tuple, Any
from sqlalchemy.orm import Session
from src.models import (
    KillSwitch,
    ProviderInstrumentMapping,
    StrategyRuntime,
    WorkerHeartbeat,
)

logger = logging.getLogger("tradepro.sandbox_gates")

PROD_ENV_NAMES = {"production", "prod", "staging"}
DEFAULT_HEARTBEAT_TTL_SECONDS = 120


class SandboxGateViolation(Exception):
    """Raised when one or more sandbox safety gates fail."""
    def __init__(self, reasons: List[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


class SandboxGateService:
    """
    Central safety gating service for Upstox Sandbox integration.
    Enforces all 8 safety gates before any external sandbox communication.
    """

    @staticmethod
    def enforce_startup_environment_guard() -> None:
        """
        Invoked during FastAPI application startup.
        If in production or staging, ensures network toggle is strictly disabled.
        """
        app_env = (os.environ.get("APP_ENV") or os.environ.get("ENV") or "local").strip().lower()
        network_enabled = os.environ.get("UPSTOX_SANDBOX_NETWORK_ENABLED", "false").strip().lower() == "true"

        if app_env in PROD_ENV_NAMES and network_enabled:
            msg = (
                f"FATAL: Sandbox network transmission is enabled (UPSTOX_SANDBOX_NETWORK_ENABLED=true) "
                f"while running in '{app_env}' environment. Server startup aborted for safety."
            )
            logger.critical(msg)
            raise RuntimeError(msg)

    @staticmethod
    def evaluate_runtime_readiness(
        db: Session,
        runtime: StrategyRuntime,
        configured_owner_id: Optional[str] = None,
        token: Optional[str] = None,
        now: Optional[datetime.datetime] = None,
    ) -> Dict[str, Any]:
        """
        Pure local check of all gates for a specific runtime.
        Makes zero network requests.
        """
        now = now or datetime.datetime.now(datetime.timezone.utc)
        owner_id = configured_owner_id or os.environ.get("UPSTOX_SANDBOX_OWNER_ID", "")
        access_token = token or os.environ.get("UPSTOX_SANDBOX_ACCESS_TOKEN", "")
        app_env = (os.environ.get("APP_ENV") or os.environ.get("ENV") or "local").strip().lower()
        network_enabled = os.environ.get("UPSTOX_SANDBOX_NETWORK_ENABLED", "false").strip().lower() == "true"

        reasons: List[str] = []

        # 1. Environment Gate
        environment_allowed = app_env not in PROD_ENV_NAMES
        if not environment_allowed:
            reasons.append(f"Environment '{app_env}' strictly forbids sandbox execution")

        # 2. Network Toggle Gate
        if not network_enabled:
            reasons.append("Sandbox external network toggle is disabled (UPSTOX_SANDBOX_NETWORK_ENABLED=false)")

        # 3. Mode Gate
        provider_matches = runtime.trading_mode in ("BROKER_SANDBOX", "BROKER_SANDBOX_RECORDED_FIXTURE")
        if not provider_matches:
            reasons.append(f"Runtime mode '{runtime.trading_mode}' is not a sandbox execution mode")

        # Provider Check
        frozen_mapping = (runtime.instrument_spec_snapshot or {}).get("provider_mapping")
        provider = (frozen_mapping.get("provider") if frozen_mapping else None) or (runtime.instrument_spec_snapshot or {}).get("provider") or "UPSTOX"
        if provider != "UPSTOX":
            provider_matches = False
            reasons.append(f"Provider '{provider}' is not supported for Upstox Sandbox")

        # 4. Owner Gate
        owner_matches = bool(owner_id) and (runtime.owner_id == owner_id)
        if not owner_matches:
            reasons.append(f"Runtime owner does not match configured UPSTOX_SANDBOX_OWNER_ID")

        # 5. Token Gate
        credential_present = bool(access_token and access_token.strip())
        if not credential_present:
            reasons.append("Upstox sandbox access token is missing or empty")

        # 6. Instrument Mapping Gate
        mapping_verified = False
        mapping_unexpired = False
        target_instrument_id = None
        if runtime.instrument_spec_snapshot:
            target_instrument_id = runtime.instrument_spec_snapshot.get("instrument_id")
        if not target_instrument_id and runtime.action_policy_snapshot:
            target_instrument_id = runtime.action_policy_snapshot.get("entry_mapping", {}).get("instrument_id")

        if frozen_mapping:
            mapping_verified = (frozen_mapping.get("verification_status") == "VERIFIED")
            if not mapping_verified:
                reasons.append(f"Instrument mapping verification status is '{frozen_mapping.get('verification_status')}' (must be VERIFIED)")
            exp = frozen_mapping.get("expiry_date")
            if exp:
                if isinstance(exp, str):
                    try:
                        exp_dt = datetime.datetime.fromisoformat(exp.replace("Z", "+00:00"))
                        if exp_dt.tzinfo is None:
                            exp_dt = exp_dt.replace(tzinfo=datetime.timezone.utc)
                        mapping_unexpired = exp_dt > now
                    except Exception:
                        mapping_unexpired = True
                elif isinstance(exp, datetime.datetime):
                    if exp.tzinfo is None:
                        exp = exp.replace(tzinfo=datetime.timezone.utc)
                    mapping_unexpired = exp > now
                else:
                    mapping_unexpired = True
            else:
                mapping_unexpired = True

            if not mapping_unexpired:
                reasons.append(f"Frozen instrument mapping has expired as of {exp}")
        elif target_instrument_id:
            mapping = db.query(ProviderInstrumentMapping).filter(
                ProviderInstrumentMapping.owner_id == runtime.owner_id,
                ProviderInstrumentMapping.tradepro_instrument_id == target_instrument_id,
            ).order_by(ProviderInstrumentMapping.mapping_version.desc()).first()

            if not mapping:
                reasons.append(f"No provider instrument mapping found for '{target_instrument_id}'")
            else:
                mapping_verified = (mapping.verification_status == "VERIFIED")
                if not mapping_verified:
                    reasons.append(f"Instrument mapping verification status is '{mapping.verification_status}' (must be VERIFIED)")

                if mapping.expiry_date:
                    mapping_unexpired = mapping.expiry_date > now
                else:
                    mapping_unexpired = True

                if not mapping_unexpired:
                    reasons.append(f"Instrument mapping has expired as of {mapping.expiry_date}")
        else:
            reasons.append("Runtime has no configured instrument")

        # 7. Kill Switch Gate
        global_ks = db.query(KillSwitch).filter(KillSwitch.scope == "GLOBAL", KillSwitch.is_active == True).first()
        user_ks = db.query(KillSwitch).filter(KillSwitch.scope == "USER", KillSwitch.user_id == runtime.owner_id, KillSwitch.is_active == True).first()

        global_kill_switch_clear = (global_ks is None)
        user_kill_switch_clear = (user_ks is None)

        if not global_kill_switch_clear:
            reasons.append("Global kill switch is active")
        if not user_kill_switch_clear:
            reasons.append("User kill switch is active")

        # 8. Worker Availability Gate
        heartbeat_cutoff = now - datetime.timedelta(seconds=DEFAULT_HEARTBEAT_TTL_SECONDS)
        active_heartbeat = db.query(WorkerHeartbeat).filter(
            WorkerHeartbeat.status == "HEALTHY",
            WorkerHeartbeat.last_heartbeat_at >= heartbeat_cutoff,
        ).first()
        worker_available = (active_heartbeat is not None)
        if not worker_available:
            reasons.append("No active sandbox outbox worker heartbeat found within TTL")

        # Readiness determination
        # Place/Submit requires all gates clear
        ready_for_submission = (
            environment_allowed
            and network_enabled
            and provider_matches
            and owner_matches
            and credential_present
            and mapping_verified
            and mapping_unexpired
            and global_kill_switch_clear
            and user_kill_switch_clear
            and worker_available
        )

        # Cancel is permitted even if kill switch is active!
        cancel_available = (
            environment_allowed
            and network_enabled
            and provider_matches
            and owner_matches
            and credential_present
            and worker_available
        )

        return {
            "runtime_id": runtime.id,
            "environment_allowed": environment_allowed,
            "network_enabled": network_enabled,
            "credential_present": credential_present,
            "owner_matches": owner_matches,
            "provider_matches": provider_matches,
            "mapping_verified": mapping_verified,
            "mapping_unexpired": mapping_unexpired,
            "global_kill_switch_clear": global_kill_switch_clear,
            "user_kill_switch_clear": user_kill_switch_clear,
            "worker_available": worker_available,
            "ready_for_submission": ready_for_submission,
            "cancel_available": cancel_available,
            "reasons": reasons,
        }

    @staticmethod
    def check_outbox_transmission_gates(
        db: Session,
        owner_id: str,
        action_type: str,
        target_instrument_id: Optional[str] = None,
        now: Optional[datetime.datetime] = None,
    ) -> Tuple[bool, List[str]]:
        """
        Invoked immediately before any HTTP transmission in the delivery worker.
        Returns (is_allowed, reasons).
        """
        now = now or datetime.datetime.now(datetime.timezone.utc)
        cfg_owner_id = os.environ.get("UPSTOX_SANDBOX_OWNER_ID", "")
        access_token = os.environ.get("UPSTOX_SANDBOX_ACCESS_TOKEN", "")
        app_env = (os.environ.get("APP_ENV") or os.environ.get("ENV") or "local").strip().lower()
        network_enabled = os.environ.get("UPSTOX_SANDBOX_NETWORK_ENABLED", "false").strip().lower() == "true"

        reasons: List[str] = []

        if app_env in PROD_ENV_NAMES:
            reasons.append(f"Production environment '{app_env}' blocks all sandbox transmission")

        if not network_enabled:
            reasons.append("Network toggle UPSTOX_SANDBOX_NETWORK_ENABLED is false")

        if not cfg_owner_id or owner_id != cfg_owner_id:
            reasons.append(f"Owner '{owner_id}' does not match configured sandbox owner '{cfg_owner_id}'")

        if not access_token or not access_token.strip():
            reasons.append("UPSTOX_SANDBOX_ACCESS_TOKEN is missing")

        # Kill switches: block PLACE, allow CANCEL
        if action_type == "PLACE":
            global_ks = db.query(KillSwitch).filter(KillSwitch.scope == "GLOBAL", KillSwitch.is_active == True).first()
            user_ks = db.query(KillSwitch).filter(KillSwitch.scope == "USER", KillSwitch.user_id == owner_id, KillSwitch.is_active == True).first()
            if global_ks:
                reasons.append("Global kill switch is active")
            if user_ks:
                reasons.append("User kill switch is active")

            # Check instrument mapping for submission
            if target_instrument_id:
                mapping = db.query(ProviderInstrumentMapping).filter(
                    ProviderInstrumentMapping.owner_id == owner_id,
                    ProviderInstrumentMapping.tradepro_instrument_id == target_instrument_id,
                ).order_by(ProviderInstrumentMapping.mapping_version.desc()).first()

                if not mapping or mapping.verification_status != "VERIFIED":
                    reasons.append(f"Instrument '{target_instrument_id}' mapping is not VERIFIED")
                elif mapping.expiry_date and mapping.expiry_date <= now:
                    reasons.append(f"Instrument '{target_instrument_id}' mapping expired at {mapping.expiry_date}")

        return (len(reasons) == 0, reasons)
