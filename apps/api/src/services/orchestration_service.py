"""Milestone 6C Phase 2 Orchestration Service.

Implements activation, structured consent binding, lifecycle controls (activate, pause, resume, stop),
prerequisite revalidation, fail-closed transmission gating, and audit event recording.
Zero broker transmission; purely internal mock execution for FIXTURE_REPLAY.
"""
import datetime
import hashlib
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from src.database import UTCDateTime
from src.engine.manifest import get_dataset_entry
from src.engine.orchestration.evidence import (
    config_consent_fingerprint,
    consent_fingerprint,
)
from src.engine.orchestration.fingerprint import (
    canonical_json,
    orchestration_snapshot_v1,
)
from src.engine.orchestration.models import (
    CandleSourceType,
    DatasetProvenance,
    OrchestrationSnapshot,
    ProviderMappingIdentity,
    SeriesRole,
    utc,
)
from src.engine.orchestration.source_policy import (
    _APPROVED,
    freeze_packaged_snapshot,
    freeze_provider_snapshot,
    packaged_alignment,
)
from src.engine.orchestration.transmission_gate import (
    assert_orchestration_execution_is_internal_only,
    external_transmission_allowed,
)
from src.engine.paper.state_machine import (
    InvalidRuntimeTransitionError,
    RuntimeStatus,
    validate_runtime_transition,
)
from src.models import (
    LEGACY_PRINCIPAL_ID,
    ApiIdempotencyRecord,
    KillSwitch,
    PaperAccount,
    ProviderInstrumentMapping,
    RiskPolicy,
    RuntimeEvaluation,
    RuntimeEvent,
    RuntimeOrchestrationConfig,
    Strategy,
    StrategyActionPolicy,
    StrategyRuntime,
    User,
)
from src.schemas import (
    OrchestrationActivationRequest,
    OrchestrationConfigCreateRequest,
)

logger = logging.getLogger("tradepro.orchestration_service")

TIMEFRAME_SECONDS: Dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "1d": 86400,
}


class ResourceNotFoundError(Exception):
    """Raised when an owned resource is not found or belongs to another owner."""
    pass


class PermissionDeniedError(Exception):
    """Raised when the current user lacks required role or is inactive/legacy."""
    pass


class ConflictError(Exception):
    """Raised on invalid lifecycle transitions or conflicting idempotency keys."""
    pass


class AccountBarrierBlockedError(ValueError):
    """Raised when an account turn barrier prevents a candidate from finalizing ahead of an earlier member turn."""
    pass


class RuntimeReplayBehindAccountError(ValueError):
    """Raised when a runtime attempts to execute or activate behind the account's committed replay watermark."""
    pass


class AccountUnderReplayOwnershipError(ValueError):
    """Raised when manual mutations are attempted on an account under active orchestration replay."""
    pass


class OrchestrationService:
    """Service encapsulating orchestration configuration and lifecycle operations."""

    @staticmethod
    def _verify_active_owner(user: Optional[User]) -> None:
        if not user or not user.is_active:
            raise PermissionDeniedError("Authenticated user is inactive or does not exist.")
        if user.id == LEGACY_PRINCIPAL_ID:
            raise PermissionDeniedError("Disabled legacy principal cannot perform orchestration actions.")

    @staticmethod
    def _get_owned_runtime(db: Session, runtime_id: str, owner_id: str, for_update: bool = False) -> StrategyRuntime:
        query = db.query(StrategyRuntime).filter(
            StrategyRuntime.id == runtime_id,
            StrategyRuntime.owner_id == owner_id,
        )
        if for_update:
            query = query.populate_existing().with_for_update()
        runtime = query.first()
        if not runtime:
            raise ResourceNotFoundError(f"Runtime '{runtime_id}' not found.")
        return runtime

    @staticmethod
    def create_orchestration_config(
        db: Session,
        owner_id: str,
        payload: OrchestrationConfigCreateRequest,
        actor_id: str,
    ) -> RuntimeOrchestrationConfig:
        """Create and freeze orchestration configuration with user-submitted structured consent."""
        user = db.query(User).filter(User.id == owner_id).first()
        OrchestrationService._verify_active_owner(user)

        runtime = OrchestrationService._get_owned_runtime(db, payload.runtime_id, owner_id, for_update=True)
        if runtime.status not in ("DRAFT", "READY") and not db.query(RuntimeOrchestrationConfig.id).filter(
            RuntimeOrchestrationConfig.runtime_id == runtime.id,
            RuntimeOrchestrationConfig.owner_id == owner_id,
        ).first():
            raise ConflictError("Configure orchestration before starting the runtime")

        # 1. Authoritative parent resource verification (all owner-scoped, indistinguishable 404)
        account = db.query(PaperAccount).filter(
            PaperAccount.id == runtime.account_id,
            PaperAccount.owner_id == owner_id,
        ).first()
        if not account:
            raise ResourceNotFoundError(f"Linked paper account '{runtime.account_id}' not found.")

        strategy = db.query(Strategy).filter(
            Strategy.id == runtime.strategy_id,
            Strategy.owner_id == owner_id,
        ).first()
        if not strategy:
            raise ResourceNotFoundError(f"Linked strategy '{runtime.strategy_id}' not found.")

        action_policy = db.query(StrategyActionPolicy).filter(
            StrategyActionPolicy.id == runtime.action_policy_id,
            StrategyActionPolicy.owner_id == owner_id,
        ).first()
        if not action_policy:
            raise ResourceNotFoundError(f"Linked action policy '{runtime.action_policy_id}' not found.")

        risk_policy = db.query(RiskPolicy).filter(
            RiskPolicy.id == runtime.risk_policy_id,
            RiskPolicy.owner_id == owner_id,
        ).first()
        if not risk_policy:
            raise ResourceNotFoundError(f"Linked risk policy '{runtime.risk_policy_id}' not found.")

        # 2. Provider Instrument Mapping verification
        now = datetime.datetime.now(datetime.timezone.utc)
        mapping = db.query(ProviderInstrumentMapping).filter(
            ProviderInstrumentMapping.id == payload.provider_mapping_id,
            ProviderInstrumentMapping.owner_id == owner_id,
        ).first()
        if not mapping:
            raise ResourceNotFoundError(f"Provider instrument mapping '{payload.provider_mapping_id}' not found.")
        if mapping.verification_status != "VERIFIED":
            raise ValueError(f"Instrument mapping verification status is '{mapping.verification_status}' (must be VERIFIED)")
        if mapping.expiry_date and mapping.expiry_date <= now:
            raise ValueError(f"Instrument mapping has expired as of {mapping.expiry_date}")
        target_inst = runtime.instrument_id
        if target_inst and mapping.tradepro_instrument_id != target_inst:
            raise ValueError(
                f"Provider instrument mapping '{mapping.id}' instrument '{mapping.tradepro_instrument_id}' "
                f"does not match runtime orderable instrument '{target_inst}'"
            )

        # 3. Kill Switch check
        global_ks = db.query(KillSwitch).filter(KillSwitch.scope == "GLOBAL", KillSwitch.is_active.is_(True)).first()
        user_ks = db.query(KillSwitch).filter(KillSwitch.scope == "USER", KillSwitch.user_id == owner_id, KillSwitch.is_active.is_(True)).first()
        if global_ks or user_ks:
            raise ValueError("Cannot create orchestration configuration while kill switch is active.")

        # 4. Validate Datasets against approved manifest (only required for FIXTURE_REPLAY)
        if payload.timeframe not in ("5m", "15m"):
            raise ValueError(f"Unsupported timeframe '{payload.timeframe}'. Authoritative Phase 1 supported timeframes are: '5m', '15m'.")
        if payload.timeframe != runtime.timeframe:
            raise ValueError(f"Payload timeframe '{payload.timeframe}' does not match runtime timeframe '{runtime.timeframe}'")

        consent = payload.consent
        exec_policy = getattr(payload, "execution_policy", None) or consent.acknowledged_execution_policy
        source_type = getattr(payload, "source_type", None) or consent.acknowledged_source_type

        dataset_dicts: List[Dict[str, Any]] = []
        dataset_ids_set = set()

        if exec_policy == "EXTERNAL_SANDBOX_DISPATCH":
            if source_type not in ("PROVIDER_SANDBOX", "PROVIDER_UPSTOX_V3"):
                raise ValueError(f"Unsupported source type '{source_type}' for EXTERNAL_SANDBOX_DISPATCH")
            if runtime.trading_mode != "BROKER_SANDBOX":
                raise ValueError(f"Runtime trading_mode is '{runtime.trading_mode}' (must be 'BROKER_SANDBOX')")
            # For provider execution, datasets can be empty
            for d in (payload.datasets or []):
                dataset_ids_set.add(d.dataset_id)
                dataset_dicts.append({
                    "dataset_id": d.dataset_id,
                    "checksum": hashlib.sha256(d.dataset_id.encode()).hexdigest(),
                    "instrument_id": mapping.tradepro_instrument_id,
                    "series_role": d.series_role,
                })
        else:
            has_reference = False
            for d in payload.datasets:
                dataset_id = d.dataset_id
                if dataset_id not in _APPROVED:
                    raise ValueError(f"Dataset '{dataset_id}' has no approved source policy")
                policy = packaged_alignment(dataset_id)
                if policy.timeframe != payload.timeframe:
                    raise ValueError(f"Dataset '{dataset_id}' timeframe '{policy.timeframe}' does not match requested timeframe '{payload.timeframe}'")
                entry = get_dataset_entry(dataset_id)
                if not entry:
                    raise ValueError(f"Dataset '{dataset_id}' not found in manifest")

                role = SeriesRole(d.series_role)
                if role == SeriesRole.REFERENCE:
                    has_reference = True
                dataset_ids_set.add(dataset_id)
                dataset_dicts.append({
                    "dataset_id": dataset_id,
                    "checksum": entry.dataset_checksum,
                    "instrument_id": entry.instrument_id,
                    "series_role": role.value,
                })

            if not has_reference:
                raise ValueError("Datasets must contain exactly one REFERENCE series")

        # 5. User-Submitted Structured Consent Validation (Server does not manufacture consent!)
        if exec_policy not in ("INTERNAL_MOCK_ONLY", "INTERNAL_PAPER", "EXTERNAL_SANDBOX_DISPATCH"):
            raise ValueError(f"Unsupported execution policy '{exec_policy}'")

        if exec_policy == "INTERNAL_MOCK_ONLY":
            if consent.consent_version != "fixture_consent_v1":
                raise ValueError("Unsupported consent policy version")
            if consent.acknowledged_execution_policy != "INTERNAL_MOCK_ONLY":
                raise ValueError("Consent must explicitly acknowledge INTERNAL_MOCK_ONLY execution policy")
            if consent.confirm_internal_mock_only is not True:
                raise ValueError("Consent must explicitly confirm internal mock execution")
            if consent.acknowledged_source_type != "FIXTURE_REPLAY":
                raise ValueError("Consent must explicitly acknowledge FIXTURE_REPLAY source type")
        elif exec_policy == "INTERNAL_PAPER":
            if consent.consent_version != "fixture_paper_consent_v1":
                raise ValueError("Unsupported consent policy version for INTERNAL_PAPER. Expected 'fixture_paper_consent_v1'")
            if consent.acknowledged_execution_policy != "INTERNAL_PAPER":
                raise ValueError("Consent must explicitly acknowledge INTERNAL_PAPER execution policy")
            if consent.confirm_internal_paper_execution is not True:
                raise ValueError("Consent must explicitly confirm internal paper execution")
            if consent.acknowledged_source_type != "FIXTURE_REPLAY":
                raise ValueError("Consent must explicitly acknowledge FIXTURE_REPLAY source type")
        elif exec_policy == "EXTERNAL_SANDBOX_DISPATCH":
            if consent.consent_version != "sandbox_consent_v1":
                raise ValueError("Unsupported consent policy version for EXTERNAL_SANDBOX_DISPATCH. Expected 'sandbox_consent_v1'")
            if consent.acknowledged_execution_policy != "EXTERNAL_SANDBOX_DISPATCH":
                raise ValueError("Consent must explicitly acknowledge EXTERNAL_SANDBOX_DISPATCH execution policy")
            if consent.confirm_external_sandbox_dispatch is not True:
                raise ValueError("Consent must explicitly confirm external sandbox dispatch")
            if consent.acknowledged_source_type not in ("PROVIDER_SANDBOX", "PROVIDER_UPSTOX_V3"):
                raise ValueError("Consent must explicitly acknowledge PROVIDER_SANDBOX or PROVIDER_UPSTOX_V3 source type")

        if consent.acknowledged_timeframe != payload.timeframe:
            raise ValueError("Consent acknowledged timeframe does not match requested timeframe")
        if utc(consent.acknowledged_replay_open_at) != utc(payload.replay_open_at) or utc(consent.acknowledged_replay_close_at) != utc(payload.replay_close_at):
            raise ValueError("Consent acknowledged replay bounds do not match requested bounds")
        if set(consent.acknowledged_dataset_ids) != dataset_ids_set:
            raise ValueError("Consent acknowledged datasets do not match requested datasets")
        if consent.confirm_prohibition_of_live_trading is not True:
            raise ValueError("Consent must explicitly confirm prohibition of live trading")

        # 6. Build Snapshot Material
        mapping_identity = ProviderMappingIdentity(
            mapping_id=mapping.id,
            mapping_version=mapping.mapping_version,
            verification_state="VERIFIED",
            expiry_at=utc(mapping.expiry_date) if mapping.expiry_date else None,
        )

        # Snapshot material from verified runtime parents
        strategy_snap = runtime.strategy_snapshot or {
            "name": strategy.name,
            "timeframe": payload.timeframe,
            "candidate_selection_mode": "FIRST_ELIGIBLE",
            "global_conditions": [],
            "candidate_conditions": [],
        }
        action_snap = runtime.action_policy_snapshot or {
            "action": "BUY",
            "type": "ENTRY",
            "entry_mapping": {"instrument_id": mapping.tradepro_instrument_id, "mapping_id": mapping.id},
        }
        risk_snap = runtime.risk_policy_snapshot or {
            "risk_config": {"max_position_size": 1, "stop_loss_pct": 5, "take_profit_pct": 10},
        }
        inst_spec = runtime.instrument_spec_snapshot or {
            "instrument_id": mapping.tradepro_instrument_id,
            "price_scale": 2,
            "lot_size_units": 1,
            "tick_size_units": 5,
        }
        inst_spec = {key: value for key, value in inst_spec.items() if key != "provider_mapping"}

        material = {
            "strategy_version": payload.strategy_version,
            "strategy_snapshot": strategy_snap,
            "action_policy_snapshot": action_snap,
            "risk_policy_snapshot": risk_snap,
            "instrument_specification": inst_spec,
            "provider_mapping": mapping_identity,
            "source_type": CandleSourceType(source_type),
            "datasets": dataset_dicts,
            "timeframe": payload.timeframe,
            "replay_open_at": utc(payload.replay_open_at),
            "replay_close_at": utc(payload.replay_close_at),
            "engine_version": "1.0.0",
            "indicator_engine_version": "1.0.0",
            "execution_policy": exec_policy,
            "external_transmission_allowed": False,
        }

        if exec_policy == "EXTERNAL_SANDBOX_DISPATCH":
            snapshot = freeze_provider_snapshot(confirmed_user=user, runtime=runtime, material=material)
        else:
            snapshot = freeze_packaged_snapshot(confirmed_user=user, runtime=runtime, material=material)
        snap_fingerprint = orchestration_snapshot_v1(snapshot)

        # Check if config already exists for this runtime
        existing = db.query(RuntimeOrchestrationConfig).filter(
            RuntimeOrchestrationConfig.runtime_id == runtime.id,
            RuntimeOrchestrationConfig.owner_id == owner_id,
        ).first()

        if existing:
            if existing.snapshot_fingerprint == snap_fingerprint:
                # Deterministic return for identical canonical configuration
                return existing
            raise ConflictError("An orchestration configuration already exists for this runtime with differing parameters.")

        consent_time = utc(now)
        config_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"tradpro:orch_config:{runtime.id}:{snap_fingerprint}"))

        provider_map = snapshot.provider_mapping.model_dump(mode="python") if hasattr(snapshot.provider_mapping, "model_dump") else snapshot.provider_mapping
        mapping_id = provider_map.get("mapping_id", "") if isinstance(provider_map, dict) else ""
        mapping_ver = provider_map.get("mapping_version", 1) if isinstance(provider_map, dict) else 1

        ordered_dataset_ids = [
            {"dataset_id": d.dataset_id, "series_role": d.series_role.value if hasattr(d.series_role, "value") else str(d.series_role)}
            for d in snapshot.datasets
        ]
        dataset_provenance = [
            {"dataset_id": d.dataset_id, "checksum": d.checksum}
            for d in snapshot.datasets
        ]

        c_fp = consent_fingerprint(
            consent_schema_version=consent.consent_version,
            actor_user_id=actor_id,
            owner_id=owner_id,
            runtime_id=runtime.id,
            orchestration_config_id=config_id,
            snapshot_fingerprint=snap_fingerprint,
            mapping_identity=mapping_id,
            mapping_version=mapping_ver,
            ordered_dataset_identities=ordered_dataset_ids,
            dataset_provenance_or_revision=dataset_provenance,
            timeframe=snapshot.timeframe,
            alignment_offset_seconds=snapshot.alignment_offset_seconds,
            replay_open_at=snapshot.replay_open_at,
            replay_close_at=snapshot.replay_close_at,
            source_type=snapshot.source_type.value,
            execution_policy=snapshot.execution_policy,
            explicit_live_trading_prohibition=True,
            explicit_internal_mock_confirmation=True,
            explicit_internal_paper_confirmation=True,
        )

        config = RuntimeOrchestrationConfig(
            id=config_id,
            owner_id=owner_id,
            runtime_id=runtime.id,
            source_type=snapshot.source_type.value,
            source_namespace=snapshot.source_namespace,
            execution_policy=snapshot.execution_policy,
            snapshot_fingerprint=snap_fingerprint,
            snapshot_json=canonical_json(snapshot.model_dump(mode="python")),
            consent_at=consent_time,
            consent_policy_version=consent.consent_version,
            consent_fingerprint=c_fp,
            source_policy_version=snapshot.source_policy_version,
            alignment_offset_seconds=snapshot.alignment_offset_seconds,
            timeframe=snapshot.timeframe,
            replay_open_at=snapshot.replay_open_at,
            replay_close_at=snapshot.replay_close_at,
            fencing_generation=1,
            retry_count=0,
            created_at=consent_time,
            updated_at=consent_time,
        )

        db.add(config)

        # Log creation event
        seq = db.query(func.coalesce(func.max(RuntimeEvent.sequence_number), 0)).filter(
            RuntimeEvent.runtime_id == runtime.id
        ).scalar() + 1
        event = RuntimeEvent(
            runtime_id=runtime.id,
            sequence_number=seq,
            previous_status=runtime.status,
            new_status=runtime.status,
            actor=actor_id,
            reason_code="ORCHESTRATION_CONFIG_CREATED",
            metadata_json={"config_id": config.id, "snapshot_fingerprint": snap_fingerprint},
            created_at=consent_time,
        )
        db.add(event)
        db.commit()
        db.refresh(config)
        return config

    @staticmethod
    def get_orchestration_config(db: Session, runtime_id: str, owner_id: str) -> RuntimeOrchestrationConfig:
        """Read-only retrieval with strict owner isolation."""
        OrchestrationService._get_owned_runtime(db, runtime_id, owner_id)
        config = db.query(RuntimeOrchestrationConfig).filter(
            RuntimeOrchestrationConfig.runtime_id == runtime_id,
            RuntimeOrchestrationConfig.owner_id == owner_id,
        ).first()
        if not config:
            raise ResourceNotFoundError(f"Orchestration configuration for runtime '{runtime_id}' not found.")
        return config

    @staticmethod
    def evaluate_activation_readiness(
        db: Session,
        runtime_id: str,
        owner_id: str,
        now: Optional[datetime.datetime] = None,
        target_action: str = "ACTIVATE",
    ) -> Dict[str, Any]:
        """Evaluate all activation gates for a specific runtime locally without network calls."""
        now = now or datetime.datetime.now(datetime.timezone.utc)
        reasons: List[str] = []
        gates: Dict[str, bool] = {}

        # 1. Runtime & Owner Gate
        runtime = db.query(StrategyRuntime).filter(
            StrategyRuntime.id == runtime_id,
            StrategyRuntime.owner_id == owner_id,
        ).first()
        if not runtime:
            raise ResourceNotFoundError(f"Runtime '{runtime_id}' not found.")

        user = db.query(User).filter(User.id == owner_id).first()
        user_valid = bool(user and user.is_active and user.id != LEGACY_PRINCIPAL_ID)
        gates["active_owner_gate"] = user_valid
        if not user_valid:
            reasons.append("Owner is inactive, missing, or the disabled legacy principal")

        # 2. Orchestration Configuration Gate
        config = db.query(RuntimeOrchestrationConfig).filter(
            RuntimeOrchestrationConfig.runtime_id == runtime_id,
            RuntimeOrchestrationConfig.owner_id == owner_id,
        ).first()
        config_valid = config is not None
        gates["configuration_gate"] = config_valid
        if not config_valid:
            reasons.append("Orchestration configuration has not been created")

        # 3. Source and Policy Gate
        if config:
            if config.execution_policy == "EXTERNAL_SANDBOX_DISPATCH":
                source_valid = (
                    config.source_type in ("PROVIDER_SANDBOX", "PROVIDER_UPSTOX_V3")
                    and runtime.trading_mode == "BROKER_SANDBOX"
                )
                if not source_valid:
                    reasons.append("Orchestration source must be PROVIDER_SANDBOX or PROVIDER_UPSTOX_V3 with trading_mode BROKER_SANDBOX")
                expected_c_ver = "sandbox_consent_v1"
            else:
                source_valid = (
                    config.source_type == "FIXTURE_REPLAY"
                    and config.execution_policy in ("INTERNAL_MOCK_ONLY", "INTERNAL_PAPER")
                )
                if not source_valid:
                    reasons.append("Orchestration source must be FIXTURE_REPLAY and execution policy INTERNAL_MOCK_ONLY or INTERNAL_PAPER")
                expected_c_ver = "fixture_paper_consent_v1" if config.execution_policy == "INTERNAL_PAPER" else "fixture_consent_v1"

            gates["source_policy_gate"] = source_valid

            # Validate consent binding
            expected_c_fp = config_consent_fingerprint(config)
            consent_valid = (config.consent_fingerprint == expected_c_fp and config.consent_policy_version == expected_c_ver)
            gates["consent_binding_gate"] = consent_valid
            if not consent_valid:
                reasons.append("Consent binding or fingerprint does not match configuration")
        else:
            gates["source_policy_gate"] = False
            gates["consent_binding_gate"] = False

        # 4. Account Gate
        account = db.query(PaperAccount).filter(
            PaperAccount.id == runtime.account_id,
            PaperAccount.owner_id == owner_id,
        ).first()
        account_valid = bool(account and account.total_cash_units > 0)
        gates["account_gate"] = account_valid
        if not account_valid:
            reasons.append("Linked paper account is missing or has zero/negative balance")

        # 5. Mapping Gate
        mapping_valid = False
        mapping_failure_reason = None
        if config:
            try:
                snap_dict = json.loads(config.snapshot_json)
                provider_map = snap_dict.get("provider_mapping", {})
                map_id = provider_map.get("mapping_id")
                map_ver = provider_map.get("mapping_version")
                mapping = db.query(ProviderInstrumentMapping).filter(
                    ProviderInstrumentMapping.id == map_id,
                    ProviderInstrumentMapping.owner_id == owner_id,
                    ProviderInstrumentMapping.mapping_version == map_ver,
                ).first()
                if mapping and mapping.verification_status == "VERIFIED":
                    if not mapping.expiry_date or mapping.expiry_date > now:
                        target_inst = runtime.instrument_id
                        if not target_inst or mapping.tradepro_instrument_id == target_inst:
                            mapping_valid = True
                        else:
                            mapping_failure_reason = (
                                f"Provider mapping instrument '{mapping.tradepro_instrument_id}' "
                                f"does not match runtime orderable instrument '{target_inst}'"
                            )
            except Exception:
                mapping_valid = False
        gates["mapping_gate"] = mapping_valid
        if not mapping_valid:
            reasons.append(mapping_failure_reason or "Frozen provider mapping is missing, unverified, or expired")

        # 6. Kill Switch Gate
        global_ks = db.query(KillSwitch).filter(KillSwitch.scope == "GLOBAL", KillSwitch.is_active.is_(True)).first()
        user_ks = db.query(KillSwitch).filter(KillSwitch.scope == "USER", KillSwitch.user_id == owner_id, KillSwitch.is_active.is_(True)).first()
        kill_switch_safe = not bool(global_ks or user_ks)
        gates["kill_switch_gate"] = kill_switch_safe
        if not kill_switch_safe:
            reasons.append("Kill switch is active")

        # 7. Transmission Prohibition Gate
        trans_safe = False
        if config:
            try:
                if config.execution_policy == "EXTERNAL_SANDBOX_DISPATCH":
                    if config.source_type not in ("PROVIDER_SANDBOX", "PROVIDER_UPSTOX_V3"):
                        raise ValueError(f"Invalid source_type '{config.source_type}' for sandbox dispatch")
                    if runtime.trading_mode != "BROKER_SANDBOX":
                        raise ValueError(f"Runtime trading_mode '{runtime.trading_mode}' is not BROKER_SANDBOX")
                    snap_dict = json.loads(config.snapshot_json) if isinstance(config.snapshot_json, str) else config.snapshot_json
                    if snap_dict.get("external_transmission_allowed") is True:
                        raise ValueError("Live external broker transmission is strictly prohibited")
                    trans_safe = True
                else:
                    assert_orchestration_execution_is_internal_only(config)
                    trans_safe = True
            except Exception as e:
                reasons.append(f"Transmission prohibition check failed: {e}")
        gates["transmission_prohibition_gate"] = trans_safe

        # 8. Runtime Lifecycle Status Gate
        if target_action == "RESUME":
            status_valid = (runtime.status == RuntimeStatus.PAUSED.value)
            if not status_valid:
                reasons.append(f"Runtime status is '{runtime.status}' (must be PAUSED to resume)")
        elif target_action == "ACTIVATE":
            status_valid = (runtime.status == RuntimeStatus.READY.value)
            if not status_valid:
                reasons.append(f"Runtime status is '{runtime.status}' (must be READY to activate)")
        elif target_action == "EXECUTE":
            status_valid = (runtime.status == RuntimeStatus.RUNNING.value)
            if not status_valid:
                reasons.append(f"Runtime status is '{runtime.status}' (must be RUNNING to execute)")
        else:
            status_valid = (runtime.status in (RuntimeStatus.READY.value, RuntimeStatus.PAUSED.value, RuntimeStatus.RUNNING.value))
            if not status_valid:
                reasons.append(f"Runtime status is '{runtime.status}' (must be READY, PAUSED, or RUNNING)")
        gates["status_gate"] = status_valid

        all_ready = len(reasons) == 0 and all(gates.values())
        return {
            "runtime_id": runtime_id,
            "ready": all_ready,
            "reasons": reasons,
            "gates": gates,
        }

    @staticmethod
    def _execute_atomic_status_update(
        db: Session,
        runtime: StrategyRuntime,
        new_status: RuntimeStatus,
        actor: str,
        reason_code: str,
        metadata_json: Optional[Dict[str, Any]] = None,
    ) -> StrategyRuntime:
        """Execute safe atomic status update handling SQLite and PostgreSQL concurrency."""
        dialect = db.bind.dialect.name if db.bind else ""
        prev_status_str = runtime.status
        prev_version = runtime.version
        now = datetime.datetime.now(datetime.timezone.utc)

        # Retry loop for transient SQLite lock contention
        max_retries = 5
        for attempt in range(max_retries):
            try:
                if dialect == "sqlite":
                    stmt = (
                        update(StrategyRuntime)
                        .where(
                            StrategyRuntime.id == runtime.id,
                            StrategyRuntime.owner_id == runtime.owner_id,
                            StrategyRuntime.status == prev_status_str,
                            StrategyRuntime.version == prev_version,
                        )
                        .values(
                            status=new_status.value,
                            version=prev_version + 1,
                            updated_at=now,
                        )
                    )
                    res = db.execute(stmt)
                    if res.rowcount == 0:
                        # Row changed concurrently; reload and raise ConflictError
                        db.refresh(runtime)
                        raise ConflictError(f"Concurrent state modification detected on runtime '{runtime.id}'.")
                else:
                    # PostgreSQL row lock
                    db.query(StrategyRuntime).filter(
                        StrategyRuntime.id == runtime.id,
                        StrategyRuntime.owner_id == runtime.owner_id,
                    ).with_for_update().first()
                    runtime.status = new_status.value
                    runtime.version = prev_version + 1
                    runtime.updated_at = now

                # Record RuntimeEvent
                seq = (
                    db.query(func.coalesce(func.max(RuntimeEvent.sequence_number), 0))
                    .filter(RuntimeEvent.runtime_id == runtime.id)
                    .scalar()
                    + 1
                )
                event = RuntimeEvent(
                    runtime_id=runtime.id,
                    sequence_number=seq,
                    previous_status=prev_status_str,
                    new_status=new_status.value,
                    actor=actor,
                    reason_code=reason_code,
                    metadata_json=metadata_json,
                    created_at=now,
                )
                db.add(event)
                db.commit()
                db.refresh(runtime)
                return runtime

            except OperationalError as oe:
                db.rollback()
                if "database is locked" in str(oe).lower() and attempt < max_retries - 1:
                    time.sleep(0.05 * (2 ** attempt))
                    continue
                raise
            except Exception:
                db.rollback()
                raise

        raise ConflictError("Unable to acquire database lock for lifecycle transition.")

    @staticmethod
    def activate_orchestration(
        db: Session,
        runtime_id: str,
        owner_id: str,
        payload: OrchestrationActivationRequest,
        actor_id: str,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Activate orchestration for an eligible runtime, strictly revalidating all prerequisites."""
        # 1. Follow universal lock hierarchy: L1 StrategyRuntime -> L2 RuntimeOrchestrationConfig -> L3 PaperAccount
        user = db.query(User).filter(User.id == owner_id).first()
        OrchestrationService._verify_active_owner(user)
        runtime = OrchestrationService._get_owned_runtime(db, runtime_id, owner_id, for_update=True)

        # 2. Check configuration exists and lock with FOR UPDATE (Level 2)
        config = db.query(RuntimeOrchestrationConfig).filter(
            RuntimeOrchestrationConfig.runtime_id == runtime_id,
            RuntimeOrchestrationConfig.owner_id == owner_id,
        ).with_for_update().first()
        if not config:
            raise ResourceNotFoundError(f"Orchestration configuration for runtime '{runtime_id}' not found.")

        if isinstance(payload, dict):
            c_ver = payload.get("consent_version") or payload.get("policy_version") or (
                "sandbox_consent_v1" if config.execution_policy == "EXTERNAL_SANDBOX_DISPATCH"
                else ("fixture_paper_consent_v1" if config.execution_policy == "INTERNAL_PAPER" else "fixture_consent_v1")
            )
            ack_pol = payload.get("acknowledged_execution_policy", config.execution_policy)
            conf_mock = payload.get("confirm_internal_mock_only")
            conf_paper = payload.get("confirm_internal_paper_execution")
            conf_sandbox = payload.get("confirm_external_sandbox_dispatch")
            payload = OrchestrationActivationRequest(
                consent_version=c_ver,
                acknowledged_execution_policy=ack_pol,
                confirm_internal_mock_only=conf_mock,
                confirm_internal_paper_execution=conf_paper,
                confirm_external_sandbox_dispatch=conf_sandbox,
            )

        # 3. Canonical request hash covering all authoritative dimensions:
        # owner, operation, runtime, configuration, consent fingerprint, expected source state, target state, payload
        canonical_req = canonical_json({
            "owner_id": owner_id,
            "operation": "activate",
            "runtime_id": runtime_id,
            "configuration_id": config.id,
            "consent_fingerprint": config.consent_fingerprint,
            "expected_source_status": "READY",
            "target_status": "RUNNING",
            "payload": {
                "consent_version": payload.consent_version,
                "acknowledged_execution_policy": payload.acknowledged_execution_policy,
                "confirm_internal_mock_only": bool(payload.confirm_internal_mock_only),
                "confirm_internal_paper_execution": bool(payload.confirm_internal_paper_execution),
                "confirm_external_sandbox_dispatch": bool(payload.confirm_external_sandbox_dispatch),
            },
        })
        req_hash = hashlib.sha256(canonical_req.encode("utf-8")).hexdigest()

        if idempotency_key:
            scoped_key = idempotency_key if len(idempotency_key) <= 64 else hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
            existing_idem = db.query(ApiIdempotencyRecord).filter(
                ApiIdempotencyRecord.owner_id == owner_id,
                ApiIdempotencyRecord.key == scoped_key,
            ).first()
            if existing_idem:
                if existing_idem.request_hash == req_hash:
                    return existing_idem.response_body
                raise ConflictError("Idempotency conflict: key reused with different canonical request.")

        # 4. Revalidate structured consent confirmation
        if config.execution_policy == "INTERNAL_MOCK_ONLY":
            if payload.consent_version != "fixture_consent_v1":
                raise ValueError("Invalid consent policy version for INTERNAL_MOCK_ONLY.")
            if payload.acknowledged_execution_policy != "INTERNAL_MOCK_ONLY":
                raise ValueError("Activation requires explicit acknowledgement of INTERNAL_MOCK_ONLY policy.")
            if payload.confirm_internal_mock_only is not True:
                raise ValueError("Explicit confirmation of internal mock execution is required.")
        elif config.execution_policy == "INTERNAL_PAPER":
            if payload.consent_version != "fixture_paper_consent_v1":
                raise ValueError("Invalid consent policy version for INTERNAL_PAPER. Expected 'fixture_paper_consent_v1'.")
            if payload.acknowledged_execution_policy != "INTERNAL_PAPER":
                raise ValueError("Activation requires explicit acknowledgement of INTERNAL_PAPER policy.")
            if payload.confirm_internal_paper_execution is not True:
                raise ValueError("Explicit confirmation of internal paper execution is required.")
        elif config.execution_policy == "EXTERNAL_SANDBOX_DISPATCH":
            if payload.consent_version != "sandbox_consent_v1":
                raise ValueError("Invalid consent policy version for EXTERNAL_SANDBOX_DISPATCH. Expected 'sandbox_consent_v1'.")
            if payload.acknowledged_execution_policy != "EXTERNAL_SANDBOX_DISPATCH":
                raise ValueError("Activation requires explicit acknowledgement of EXTERNAL_SANDBOX_DISPATCH policy.")
            if payload.confirm_external_sandbox_dispatch is not True:
                raise ValueError("Explicit confirmation of external sandbox dispatch is required.")
        else:
            raise ValueError(f"Unknown execution policy '{config.execution_policy}'")

        # 5. Lifecycle status check (idempotent if already RUNNING, ConflictError if not READY)
        curr_status = RuntimeStatus(runtime.status)
        if curr_status == RuntimeStatus.RUNNING:
            # Idempotent success if already running
            result = {
                "runtime_id": runtime.id,
                "status": runtime.status,
                "previous_status": runtime.status,
                "action": "ACTIVATE",
                "message": "Runtime is already running.",
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
            return result

        if curr_status != RuntimeStatus.READY:
            raise ConflictError(f"Cannot activate runtime in '{curr_status.value}' status. Must be in READY state.")

        # 6. Full readiness revalidation
        readiness = OrchestrationService.evaluate_activation_readiness(db, runtime_id, owner_id)
        if not readiness["ready"]:
            if not readiness["gates"].get("status_gate", True):
                raise ConflictError(f"Activation conflict: {'; '.join(readiness['reasons'])}")
            raise ValueError(f"Activation prerequisites failed: {'; '.join(readiness['reasons'])}")

        # 7. Transmission Prohibition Check
        if config.execution_policy == "EXTERNAL_SANDBOX_DISPATCH":
            if config.source_type not in ("PROVIDER_SANDBOX", "PROVIDER_UPSTOX_V3"):
                raise ValueError(f"Invalid source_type '{config.source_type}' for sandbox dispatch")
            if runtime.trading_mode != "BROKER_SANDBOX":
                raise ValueError(f"Runtime trading_mode '{runtime.trading_mode}' is not BROKER_SANDBOX")
            snap_dict = json.loads(config.snapshot_json) if isinstance(config.snapshot_json, str) else config.snapshot_json
            if snap_dict.get("external_transmission_allowed") is True:
                raise ValueError("Live external broker transmission is strictly prohibited")
        else:
            assert_orchestration_execution_is_internal_only(config)

        # 7b. Paper Account Watermark & Lock Hierarchy Check (if INTERNAL_PAPER)
        if config.execution_policy == "INTERNAL_PAPER":
            account = db.query(PaperAccount).filter(
                PaperAccount.id == runtime.account_id,
                PaperAccount.owner_id == owner_id,
            ).with_for_update().first()
            if not account:
                raise ResourceNotFoundError(f"Linked paper account '{runtime.account_id}' not found.")
            if not account.is_active:
                raise ValueError("Linked paper account is inactive.")
            tf_sec = TIMEFRAME_SECONDS.get(config.timeframe, 900)
            rt_target_start = (config.checkpoint_close_at + datetime.timedelta(seconds=tf_sec)) if config.checkpoint_close_at else (config.replay_open_at + datetime.timedelta(seconds=tf_sec))
            watermark = account.committed_replay_watermark
            if watermark is not None:
                if rt_target_start < watermark:
                    raise RuntimeReplayBehindAccountError(
                        f"Runtime '{runtime.id}' start boundary {rt_target_start} is behind account committed replay watermark {watermark}."
                    )
                elif rt_target_start == watermark:
                    later_committed = (
                        db.query(RuntimeEvaluation.runtime_id)
                        .join(StrategyRuntime, RuntimeEvaluation.runtime_id == StrategyRuntime.id)
                        .join(RuntimeOrchestrationConfig, RuntimeEvaluation.config_id == RuntimeOrchestrationConfig.id)
                        .filter(
                            StrategyRuntime.account_id == account.id,
                            StrategyRuntime.owner_id == account.owner_id,
                            RuntimeEvaluation.owner_id == account.owner_id,
                            RuntimeEvaluation.close_at == watermark,
                            RuntimeEvaluation.runtime_id >= runtime.id,
                            RuntimeOrchestrationConfig.execution_policy == "INTERNAL_PAPER",
                        )
                        .first()
                    )
                    if later_committed:
                        raise RuntimeReplayBehindAccountError(
                            f"Cannot activate runtime '{runtime.id}' at boundary {rt_target_start}: member '{later_committed[0]}' already committed at or after this tie priority."
                        )

        validate_runtime_transition(curr_status, RuntimeStatus.RUNNING, actor=actor_id, reason_code="ORCHESTRATION_ACTIVATED")

        # 8. Execute atomic transition
        updated_runtime = OrchestrationService._execute_atomic_status_update(
            db=db,
            runtime=runtime,
            new_status=RuntimeStatus.RUNNING,
            actor=actor_id,
            reason_code="ORCHESTRATION_ACTIVATED",
            metadata_json={"config_id": config.id, "snapshot_fingerprint": config.snapshot_fingerprint},
        )

        response_body = {
            "runtime_id": updated_runtime.id,
            "status": updated_runtime.status,
            "previous_status": curr_status.value,
            "action": "ACTIVATE",
            "message": "Orchestration activated successfully for internal mock execution.",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

        # 9. Store Idempotency Record if key provided
        if idempotency_key:
            scoped_key = idempotency_key if len(idempotency_key) <= 64 else hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
            try:
                idem_rec = ApiIdempotencyRecord(
                    owner_id=owner_id,
                    key=scoped_key,
                    request_hash=req_hash,
                    response_status=200,
                    response_body=response_body,
                    created_at=datetime.datetime.now(datetime.timezone.utc),
                )
                db.add(idem_rec)
                db.commit()
            except IntegrityError:
                db.rollback()
                # Conflict winner reload
                winning = db.query(ApiIdempotencyRecord).filter(
                    ApiIdempotencyRecord.owner_id == owner_id,
                    ApiIdempotencyRecord.key == scoped_key,
                ).first()
                if winning and winning.request_hash == req_hash:
                    return winning.response_body
                raise ConflictError("Idempotency race conflict: key registered concurrently with different payload.")

        return response_body

    @staticmethod
    def pause_orchestration(db: Session, runtime_id: str, owner_id: str, actor_id: str) -> Dict[str, Any]:
        """Pause orchestration evaluations. Does NOT cancel open orders or release reservations."""
        user = db.query(User).filter(User.id == owner_id).first()
        OrchestrationService._verify_active_owner(user)
        runtime = OrchestrationService._get_owned_runtime(db, runtime_id, owner_id, for_update=True)

        curr_status = RuntimeStatus(runtime.status)
        if curr_status == RuntimeStatus.PAUSED:
            return {
                "runtime_id": runtime.id,
                "status": runtime.status,
                "previous_status": runtime.status,
                "action": "PAUSE",
                "message": "Runtime is already paused.",
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }

        if curr_status != RuntimeStatus.RUNNING:
            raise ConflictError(f"Cannot pause runtime in '{curr_status.value}' status. Must be RUNNING.")

        validate_runtime_transition(curr_status, RuntimeStatus.PAUSED, actor=actor_id, reason_code="ORCHESTRATION_PAUSED")

        # Clear lease and bump fencing generation to revoke existing worker leases
        config = db.query(RuntimeOrchestrationConfig).filter(
            RuntimeOrchestrationConfig.runtime_id == runtime_id,
            RuntimeOrchestrationConfig.owner_id == owner_id,
        ).with_for_update().first()
        if config:
            db.refresh(config)
            config.lease_owner = None
            config.lease_expires_at = None
            config.fencing_generation = config.fencing_generation + 1
            config.last_reason_code = "ORCHESTRATION_PAUSED"
            config.updated_at = datetime.datetime.now(datetime.timezone.utc)

            # Follow universal lock hierarchy: L1 StrategyRuntime -> L2 RuntimeOrchestrationConfig -> L3 PaperAccount
            if config.execution_policy == "INTERNAL_PAPER" and runtime.account_id:
                db.query(PaperAccount).filter(
                    PaperAccount.id == runtime.account_id,
                    PaperAccount.owner_id == owner_id,
                ).with_for_update().first()

        updated = OrchestrationService._execute_atomic_status_update(
            db=db,
            runtime=runtime,
            new_status=RuntimeStatus.PAUSED,
            actor=actor_id,
            reason_code="ORCHESTRATION_PAUSED",
        )

        return {
            "runtime_id": updated.id,
            "status": updated.status,
            "previous_status": curr_status.value,
            "action": "PAUSE",
            "message": "Orchestration paused. New evaluations blocked; existing orders preserved.",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

    @staticmethod
    def resume_orchestration(db: Session, runtime_id: str, owner_id: str, actor_id: str) -> Dict[str, Any]:
        """Resume orchestration. Revalidates ALL prerequisites again (kill switches, mapping expiry, etc.)."""
        user = db.query(User).filter(User.id == owner_id).first()
        OrchestrationService._verify_active_owner(user)
        runtime = OrchestrationService._get_owned_runtime(db, runtime_id, owner_id, for_update=True)

        curr_status = RuntimeStatus(runtime.status)
        if curr_status == RuntimeStatus.RUNNING:
            return {
                "runtime_id": runtime.id,
                "status": runtime.status,
                "previous_status": runtime.status,
                "action": "RESUME",
                "message": "Runtime is already running.",
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }

        if curr_status != RuntimeStatus.PAUSED:
            raise ConflictError(f"Cannot resume runtime in '{curr_status.value}' status. Must be PAUSED.")

        # Revalidate all activation prerequisites again!
        readiness = OrchestrationService.evaluate_activation_readiness(db, runtime_id, owner_id, target_action="RESUME")
        if not readiness["ready"]:
            if not readiness["gates"].get("status_gate", True):
                raise ConflictError(f"Cannot resume runtime. Status conflict: {'; '.join(readiness['reasons'])}")
            raise ValueError(f"Cannot resume runtime. Prerequisites failed: {'; '.join(readiness['reasons'])}")

        config = db.query(RuntimeOrchestrationConfig).filter(
            RuntimeOrchestrationConfig.runtime_id == runtime_id,
            RuntimeOrchestrationConfig.owner_id == owner_id,
        ).with_for_update().first()
        if not config:
            raise ResourceNotFoundError(f"Configuration for runtime '{runtime_id}' not found.")
        if config.execution_policy == "EXTERNAL_SANDBOX_DISPATCH":
            if config.source_type not in ("PROVIDER_SANDBOX", "PROVIDER_UPSTOX_V3"):
                raise ValueError(f"Invalid source_type '{config.source_type}' for sandbox dispatch")
            if runtime.trading_mode != "BROKER_SANDBOX":
                raise ValueError(f"Runtime trading_mode '{runtime.trading_mode}' is not BROKER_SANDBOX")
            snap_dict = json.loads(config.snapshot_json) if isinstance(config.snapshot_json, str) else config.snapshot_json
            if snap_dict.get("external_transmission_allowed") is True:
                raise ValueError("Live external broker transmission is strictly prohibited")
        else:
            assert_orchestration_execution_is_internal_only(config)

        # Paper Account Watermark & Lock Hierarchy Check on Resume
        if config.execution_policy == "INTERNAL_PAPER":
            account = db.query(PaperAccount).filter(
                PaperAccount.id == runtime.account_id,
                PaperAccount.owner_id == owner_id,
            ).with_for_update().first()
            if not account:
                raise ResourceNotFoundError(f"Linked paper account '{runtime.account_id}' not found.")
            if not account.is_active:
                raise ValueError("Linked paper account is inactive.")
            tf_sec = TIMEFRAME_SECONDS.get(config.timeframe, 900)
            rt_next = (config.checkpoint_close_at + datetime.timedelta(seconds=tf_sec)) if config.checkpoint_close_at else (config.replay_open_at + datetime.timedelta(seconds=tf_sec))
            watermark = account.committed_replay_watermark
            if watermark is not None:
                if rt_next < watermark:
                    raise RuntimeReplayBehindAccountError(
                        f"Cannot resume runtime '{runtime.id}': next boundary {rt_next} is behind account committed replay watermark {watermark}."
                    )
                elif rt_next == watermark:
                    later_committed = (
                        db.query(RuntimeEvaluation.runtime_id)
                        .join(StrategyRuntime, RuntimeEvaluation.runtime_id == StrategyRuntime.id)
                        .join(RuntimeOrchestrationConfig, RuntimeEvaluation.config_id == RuntimeOrchestrationConfig.id)
                        .filter(
                            StrategyRuntime.account_id == account.id,
                            StrategyRuntime.owner_id == account.owner_id,
                            RuntimeEvaluation.owner_id == account.owner_id,
                            RuntimeEvaluation.close_at == watermark,
                            RuntimeEvaluation.runtime_id >= runtime.id,
                            RuntimeOrchestrationConfig.execution_policy == "INTERNAL_PAPER",
                        )
                        .first()
                    )
                    if later_committed:
                        raise RuntimeReplayBehindAccountError(
                            f"Cannot resume runtime '{runtime.id}' at boundary {rt_next}: member '{later_committed[0]}' already committed at or after this tie priority."
                        )

        validate_runtime_transition(curr_status, RuntimeStatus.RUNNING, actor=actor_id, reason_code="ORCHESTRATION_RESUMED")

        updated = OrchestrationService._execute_atomic_status_update(
            db=db,
            runtime=runtime,
            new_status=RuntimeStatus.RUNNING,
            actor=actor_id,
            reason_code="ORCHESTRATION_RESUMED",
        )

        # Explicit operator recovery from quarantine on resume; bump fencing generation to revoke prior leases
        now_ts = datetime.datetime.now(datetime.timezone.utc)
        config.retry_count = 0
        config.next_attempt_at = now_ts
        config.fencing_generation = config.fencing_generation + 1
        config.last_reason_code = "ORCHESTRATION_RESUMED"
        config.updated_at = now_ts
        db.flush()

        return {
            "runtime_id": updated.id,
            "status": updated.status,
            "previous_status": curr_status.value,
            "action": "RESUME",
            "message": "Orchestration resumed successfully.",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

    @staticmethod
    def stop_orchestration(db: Session, runtime_id: str, owner_id: str, actor_id: str) -> Dict[str, Any]:
        """Permanently stop orchestration. Does NOT cancel open orders or release reservations."""
        user = db.query(User).filter(User.id == owner_id).first()
        OrchestrationService._verify_active_owner(user)
        runtime = OrchestrationService._get_owned_runtime(db, runtime_id, owner_id, for_update=True)

        curr_status = RuntimeStatus(runtime.status)
        if curr_status == RuntimeStatus.STOPPED:
            return {
                "runtime_id": runtime.id,
                "status": runtime.status,
                "previous_status": runtime.status,
                "action": "STOP",
                "message": "Runtime is already stopped.",
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }

        # Terminal states check
        if curr_status in (RuntimeStatus.COMPLETED, RuntimeStatus.ERROR):
            raise ConflictError(f"Cannot stop runtime in terminal '{curr_status.value}' status.")

        validate_runtime_transition(curr_status, RuntimeStatus.STOPPED, actor=actor_id, reason_code="ORCHESTRATION_STOPPED")

        # Clear lease and bump fencing generation to revoke existing worker leases
        config = db.query(RuntimeOrchestrationConfig).filter(
            RuntimeOrchestrationConfig.runtime_id == runtime_id,
            RuntimeOrchestrationConfig.owner_id == owner_id,
        ).with_for_update().first()
        if config:
            db.refresh(config)
            config.lease_owner = None
            config.lease_expires_at = None
            config.fencing_generation = config.fencing_generation + 1
            config.last_reason_code = "ORCHESTRATION_STOPPED"
            config.updated_at = datetime.datetime.now(datetime.timezone.utc)

            # Follow universal lock hierarchy: L1 StrategyRuntime -> L2 RuntimeOrchestrationConfig -> L3 PaperAccount
            if config.execution_policy == "INTERNAL_PAPER" and runtime.account_id:
                db.query(PaperAccount).filter(
                    PaperAccount.id == runtime.account_id,
                    PaperAccount.owner_id == owner_id,
                ).with_for_update().first()

        updated = OrchestrationService._execute_atomic_status_update(
            db=db,
            runtime=runtime,
            new_status=RuntimeStatus.STOPPED,
            actor=actor_id,
            reason_code="ORCHESTRATION_STOPPED",
        )

        return {
            "runtime_id": updated.id,
            "status": updated.status,
            "previous_status": curr_status.value,
            "action": "STOP",
            "message": "Orchestration permanently stopped. Existing orders, outbox, and audit history preserved.",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

    @staticmethod
    def list_evaluations(
        db: Session,
        runtime_id: str,
        owner_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[RuntimeEvaluation], int]:
        """List evaluations for an owned runtime with bounded pagination and deterministic ordering."""
        OrchestrationService._get_owned_runtime(db, runtime_id, owner_id)
        bounded_limit = max(1, min(limit, 100))
        bounded_offset = max(0, offset)

        query = db.query(RuntimeEvaluation).filter(
            RuntimeEvaluation.runtime_id == runtime_id,
            RuntimeEvaluation.owner_id == owner_id,
        )
        total = query.count()
        evaluations = (
            query.order_by(
                RuntimeEvaluation.close_at.desc(),
                RuntimeEvaluation.id.desc(),
            )
            .offset(bounded_offset)
            .limit(bounded_limit)
            .all()
        )
        return evaluations, total

    @staticmethod
    def get_evaluation(
        db: Session,
        runtime_id: str,
        evaluation_id: str,
        owner_id: str,
    ) -> RuntimeEvaluation:
        """Retrieve a specific evaluation with owner-scoped 404 isolation."""
        OrchestrationService._get_owned_runtime(db, runtime_id, owner_id)
        evaluation = db.query(RuntimeEvaluation).filter(
            RuntimeEvaluation.id == evaluation_id,
            RuntimeEvaluation.runtime_id == runtime_id,
            RuntimeEvaluation.owner_id == owner_id,
        ).first()
        if not evaluation:
            raise ResourceNotFoundError(f"Evaluation '{evaluation_id}' not found.")
        return evaluation

    @staticmethod
    def get_latest_evaluation(
        db: Session,
        runtime_id: str,
        owner_id: str,
    ) -> RuntimeEvaluation:
        """Retrieve the latest finalized evaluation for an owned runtime."""
        OrchestrationService._get_owned_runtime(db, runtime_id, owner_id)
        evaluation = (
            db.query(RuntimeEvaluation)
            .filter(
                RuntimeEvaluation.runtime_id == runtime_id,
                RuntimeEvaluation.owner_id == owner_id,
            )
            .order_by(
                RuntimeEvaluation.close_at.desc(),
                RuntimeEvaluation.id.desc(),
            )
            .first()
        )
        if not evaluation:
            raise ResourceNotFoundError(f"No evaluations found for runtime '{runtime_id}'.")
        return evaluation
