import datetime
import hashlib
import json
import logging
import os
from decimal import Decimal
from typing import Any, Dict, List, Optional
from sqlalchemy.orm import Session
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from src.models import (
    AccountLedgerEntry,
    ExternalOrderLink,
    Order,
    OrderEvent,
    PaperAccount,
    ProviderConnection,
    ProviderInstrumentMapping,
    ReconciliationRecord,
    StrategyRuntime,
    SubmissionOutbox,
    User,
)
from src.engine.paper.models import (
    LedgerEntryType,
    OrderSide,
    OrderStatus,
    TradingMode,
)
from src.engine.paper.state_machine import validate_order_transition
from src.engine.paper.units import decimal_to_units, units_to_decimal
from src.services.sandbox_gate_service import SandboxGateService

logger = logging.getLogger("tradepro.sandbox_service")


from src.services.paper_service import ResourceNotFoundError, ConflictError


class PermissionDeniedError(Exception):
    pass


class InvalidOperationError(Exception):
    pass


class SandboxService:
    """
    Business service managing Upstox Sandbox connections, instrument mappings,
    outbox queues, and manual reconciliation resolutions.
    """

    @staticmethod
    def get_runtime_readiness(db: Session, runtime_id: str, owner_id: str) -> Dict[str, Any]:
        """
        Retrieves runtime-specific readiness.
        Enforces strict ownership: cross-owner or non-existent returns ResourceNotFoundError (404).
        """
        runtime = db.query(StrategyRuntime).filter(
            StrategyRuntime.id == runtime_id,
            StrategyRuntime.owner_id == owner_id,
        ).first()
        if not runtime:
            raise ResourceNotFoundError(f"Runtime '{runtime_id}' not found.")

        return SandboxGateService.evaluate_runtime_readiness(db, runtime)

    @staticmethod
    def get_connection_read_only(db: Session, owner_id: str) -> Optional[ProviderConnection]:
        """
        Pure read-only lookup of sandbox connection metadata record.
        Performs zero DB mutations, additions, flushes, or commits.
        """
        configured_owner = os.environ.get("UPSTOX_SANDBOX_OWNER_ID", "")
        if owner_id != configured_owner:
            raise ResourceNotFoundError("No sandbox connection available for this user.")

        return db.query(ProviderConnection).filter(
            ProviderConnection.owner_id == owner_id,
            ProviderConnection.provider_name == "UPSTOX",
            ProviderConnection.environment == "SANDBOX",
        ).first()

    @staticmethod
    def get_or_create_connection(db: Session, owner_id: str) -> ProviderConnection:
        """
        Gets or initializes the sandbox connection metadata record for the configured owner.
        Persisted states are strictly CONFIGURED, DISABLED, ERROR.
        """
        configured_owner = os.environ.get("UPSTOX_SANDBOX_OWNER_ID", "")
        if owner_id != configured_owner:
            raise ResourceNotFoundError("No sandbox connection available for this user.")

        conn = db.query(ProviderConnection).filter(
            ProviderConnection.owner_id == owner_id,
            ProviderConnection.provider_name == "UPSTOX",
            ProviderConnection.environment == "SANDBOX",
        ).first()

        if not conn:
            conn = ProviderConnection(
                owner_id=owner_id,
                provider_name="UPSTOX",
                environment="SANDBOX",
                credential_reference="ENV_UPSTOX_SANDBOX_ACCESS_TOKEN",
                credential_version="v1",
                status="CONFIGURED",
            )
            db.add(conn)
            db.commit()
            db.refresh(conn)

        return conn

    # --- Instrument Mappings ---

    @staticmethod
    def list_instrument_mappings(db: Session, owner_id: str) -> List[ProviderInstrumentMapping]:
        return db.query(ProviderInstrumentMapping).filter(
            ProviderInstrumentMapping.owner_id == owner_id
        ).order_by(ProviderInstrumentMapping.created_at.desc()).all()

    @staticmethod
    def create_instrument_mapping(
        db: Session,
        owner_id: str,
        tradepro_instrument_id: str,
        provider_instrument_token: str,
        exchange: str,
        segment: str,
        symbol: str,
        expiry_date: Optional[datetime.datetime] = None,
        strike_price: Optional[Decimal] = None,
        option_type: Optional[str] = None,
        lot_size: int = 1,
        tick_size: Decimal = Decimal("0.05"),
        freeze_quantity: int = 1800,
    ) -> ProviderInstrumentMapping:
        """
        EDITOR/ADMIN creates an UNVERIFIED mapping for their own account.
        """
        latest = db.query(ProviderInstrumentMapping).filter(
            ProviderInstrumentMapping.owner_id == owner_id,
            ProviderInstrumentMapping.tradepro_instrument_id == tradepro_instrument_id,
        ).order_by(ProviderInstrumentMapping.mapping_version.desc()).first()

        next_version = (latest.mapping_version + 1) if latest else 1

        strike_units = decimal_to_units(strike_price, 2) if strike_price is not None else None
        tick_units = decimal_to_units(tick_size, 2)

        mapping = ProviderInstrumentMapping(
            owner_id=owner_id,
            tradepro_instrument_id=tradepro_instrument_id,
            provider_instrument_token=provider_instrument_token,
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            expiry_date=expiry_date,
            strike_price_units=strike_units,
            option_type=option_type,
            lot_size_units=lot_size,
            tick_size_units=tick_units,
            freeze_quantity_units=freeze_quantity,
            verification_status="UNVERIFIED",
            mapping_version=next_version,
        )
        db.add(mapping)
        db.commit()
        db.refresh(mapping)
        return mapping

    @staticmethod
    def verify_instrument_mapping(
        db: Session,
        mapping_id: str,
        actor_user: User,
        new_status: str,
        reason: str,
    ) -> ProviderInstrumentMapping:
        """
        ADMIN may verify or reject a mapping belonging to the configured UPSTOX_SANDBOX_OWNER_ID.
        Mapping states remain distinct: UNVERIFIED, VERIFIED, REJECTED, DISABLED.
        Rejection stores REJECTED; explicit disable stores DISABLED.
        The audit event's new_status exactly matches the persisted database status.
        Invalid transitions return 409 (ConflictError).
        A rejected mapping cannot silently become verified; requires a new mapping version.
        """
        if actor_user.role != "ADMIN":
            raise PermissionDeniedError("Only ADMIN can verify or reject instrument mappings.")

        if new_status not in ("VERIFIED", "REJECTED"):
            raise ConflictError(f"Invalid verification decision '{new_status}'. Permitted decisions: 'VERIFIED', 'REJECTED'.")

        configured_owner = os.environ.get("UPSTOX_SANDBOX_OWNER_ID", "")
        mapping = db.query(ProviderInstrumentMapping).filter(
            ProviderInstrumentMapping.id == mapping_id
        ).first()

        if not mapping:
            raise ResourceNotFoundError(f"Mapping '{mapping_id}' not found.")

        # If mapping does not belong to actor, it MUST belong to the configured UPSTOX_SANDBOX_OWNER_ID
        if mapping.owner_id != actor_user.id:
            if not configured_owner or mapping.owner_id != configured_owner:
                raise ResourceNotFoundError(f"Mapping '{mapping_id}' not found.")

        # Transition rules:
        if mapping.verification_status == "DISABLED":
            raise ConflictError(f"Cannot transition mapping '{mapping_id}' from 'DISABLED' to '{new_status}'.")

        if mapping.verification_status == "REJECTED":
            raise ConflictError("A rejected mapping cannot be verified directly. A new mapping version is required.")

        if mapping.verification_status == new_status:
            raise ConflictError(f"Mapping '{mapping_id}' is already in status '{new_status}'.")

        now = datetime.datetime.now(datetime.timezone.utc)
        audit_record = {
            "actor_id": actor_user.id,
            "actor_username": actor_user.username,
            "actor_role": actor_user.role,
            "target_owner_id": mapping.owner_id,
            "mapping_id": mapping.id,
            "previous_status": mapping.verification_status,
            "new_status": new_status,
            "reason": reason,
            "timestamp": now.isoformat(),
            "source": "ADMIN_VERIFICATION_ENDPOINT",
        }

        mapping.verification_status = new_status
        mapping.verified_by = actor_user.id
        mapping.verified_at = now
        mapping.verification_audit_json = audit_record

        db.commit()
        db.refresh(mapping)
        return mapping

    @staticmethod
    def disable_instrument_mapping(
        db: Session,
        mapping_id: str,
        actor_user: User,
    ) -> ProviderInstrumentMapping:
        """
        Explicit disable operation stores DISABLED.
        Invalid transitions (e.g. already disabled) return 409 ConflictError.
        """
        mapping = db.query(ProviderInstrumentMapping).filter(
            ProviderInstrumentMapping.id == mapping_id
        ).first()
        if not mapping:
            raise ResourceNotFoundError(f"Mapping '{mapping_id}' not found.")
        configured_owner = os.environ.get("UPSTOX_SANDBOX_OWNER_ID", "")
        if mapping.owner_id != actor_user.id:
            if actor_user.role != "ADMIN" or not configured_owner or mapping.owner_id != configured_owner:
                raise ResourceNotFoundError(f"Mapping '{mapping_id}' not found.")

        if mapping.verification_status == "DISABLED":
            raise ConflictError(f"Mapping '{mapping_id}' is already disabled.")

        now = datetime.datetime.now(datetime.timezone.utc)
        audit_record = {
            "actor_id": actor_user.id,
            "actor_username": actor_user.username,
            "actor_role": actor_user.role,
            "target_owner_id": mapping.owner_id,
            "mapping_id": mapping.id,
            "previous_status": mapping.verification_status,
            "new_status": "DISABLED",
            "action": "DISABLE",
            "timestamp": now.isoformat(),
            "source": "DISABLE_ENDPOINT",
        }

        mapping.verification_status = "DISABLED"
        mapping.verification_audit_json = audit_record
        db.commit()
        db.refresh(mapping)
        return mapping

    # --- Outbox ---

    @staticmethod
    def list_outbox(db: Session, owner_id: str) -> List[SubmissionOutbox]:
        return db.query(SubmissionOutbox).filter(
            SubmissionOutbox.owner_id == owner_id
        ).order_by(SubmissionOutbox.created_at.desc()).limit(100).all()

    # --- Reconciliation ---

    @staticmethod
    def list_reconciliation_records(
        db: Session,
        owner_id: str,
        status_filter: Optional[str] = None,
    ) -> List[ReconciliationRecord]:
        q = db.query(ReconciliationRecord).filter(
            ReconciliationRecord.owner_id == owner_id
        )
        if status_filter:
            q = q.filter(ReconciliationRecord.status == status_filter)
        return q.order_by(ReconciliationRecord.created_at.desc()).limit(100).all()

    @staticmethod
    def resolve_reconciliation(
        db: Session,
        actor_user: User,
        resolution_type: str,
        notes: str,
        provider_order_reference: Optional[str] = None,
        order_id: Optional[str] = None,
        outbox_id: Optional[str] = None,
        identifier: Optional[str] = None,
        reconciliation_id: Optional[str] = None,
    ) -> ReconciliationRecord:
        """
        Operation-scoped manual reconciliation resolution.
        Resolves an existing OPEN reconciliation case.
        1. Load by reconciliation ID and owner.
        2. Return identical 404 for missing and cross-owner cases.
        3. Lock the reconciliation case, order, outbox, account and reservation rows.
        4. Require case status OPEN.
        5. Require outbox status RECONCILIATION_REQUIRED.
        6. Validate resolution type against PLACE or CANCEL (409 on mismatch).
        7. Apply the order/link/ledger result.
        8. Set the case to RESOLVED.
        9. Set resolution actor, timestamp, type, provider reference and sanitized notes.
        10. Commit atomically.
        11. Return 409 if already resolved.
        Outbox remains permanently RECONCILIATION_REQUIRED (never set to DELIVERED).
        """
        valid_resolutions = {"PLACE_CONFIRMED", "PLACE_REJECTED", "CANCEL_CONFIRMED", "CANCEL_NOT_CONFIRMED"}
        if resolution_type not in valid_resolutions:
            raise InvalidOperationError(f"Unknown resolution type '{resolution_type}'")

        if not notes or len(notes.strip()) < 5:
            raise InvalidOperationError("Notes must be provided (min 5 characters).")

        sanitized_notes = notes.strip()[:1000]
        trimmed_ref = provider_order_reference.strip()[:100] if provider_order_reference else None

        rec: Optional[ReconciliationRecord] = None
        target_rec_id = reconciliation_id or identifier

        # 1. If reconciliation_id / identifier provided, look up by reconciliation ID
        if target_rec_id:
            cand = db.query(ReconciliationRecord).filter(ReconciliationRecord.id == target_rec_id).first()
            if cand:
                if cand.owner_id != actor_user.id:
                    raise ResourceNotFoundError(f"Reconciliation case '{target_rec_id}' not found.")
                rec = cand
            else:
                # Target ID could be outbox_id or order_id
                cand_outbox = db.query(SubmissionOutbox).filter(SubmissionOutbox.id == target_rec_id).first()
                if cand_outbox:
                    if cand_outbox.owner_id != actor_user.id:
                        raise ResourceNotFoundError(f"Reconciliation case '{target_rec_id}' not found.")
                    rec = db.query(ReconciliationRecord).filter(
                        ReconciliationRecord.outbox_id == cand_outbox.id,
                        ReconciliationRecord.owner_id == actor_user.id,
                    ).first()
                else:
                    cand_order = db.query(Order).filter(Order.id == target_rec_id).first()
                    if cand_order:
                        if cand_order.owner_id != actor_user.id:
                            raise ResourceNotFoundError(f"Reconciliation case '{target_rec_id}' not found.")
                        rec = db.query(ReconciliationRecord).filter(
                            ReconciliationRecord.order_id == cand_order.id,
                            ReconciliationRecord.owner_id == actor_user.id,
                        ).order_by(ReconciliationRecord.created_at.desc()).first()

        # 2. If not found yet and outbox_id / order_id kwargs provided:
        if not rec:
            if outbox_id:
                cand_ob = db.query(SubmissionOutbox).filter(SubmissionOutbox.id == outbox_id).first()
                if not cand_ob or cand_ob.owner_id != actor_user.id:
                    raise ResourceNotFoundError(f"Outbox operation '{outbox_id}' not found.")
                rec = db.query(ReconciliationRecord).filter(
                    ReconciliationRecord.outbox_id == outbox_id,
                    ReconciliationRecord.owner_id == actor_user.id,
                ).first()
            elif order_id:
                cand_ord = db.query(Order).filter(Order.id == order_id).first()
                if not cand_ord or cand_ord.owner_id != actor_user.id:
                    raise ResourceNotFoundError(f"Order '{order_id}' not found.")
                rec = db.query(ReconciliationRecord).filter(
                    ReconciliationRecord.order_id == order_id,
                    ReconciliationRecord.owner_id == actor_user.id,
                ).order_by(ReconciliationRecord.created_at.desc()).first()

        # If still no rec found, check if outbox exists with RECONCILIATION_REQUIRED
        if not rec:
            target_outbox = None
            if outbox_id:
                target_outbox = db.query(SubmissionOutbox).filter(SubmissionOutbox.id == outbox_id, SubmissionOutbox.owner_id == actor_user.id).first()
            elif order_id:
                target_outbox = db.query(SubmissionOutbox).filter(
                    SubmissionOutbox.order_id == order_id,
                    SubmissionOutbox.owner_id == actor_user.id,
                    SubmissionOutbox.status == "RECONCILIATION_REQUIRED",
                ).order_by(SubmissionOutbox.created_at.desc()).first()
                if not target_outbox:
                    target_outbox = db.query(SubmissionOutbox).filter(
                        SubmissionOutbox.order_id == order_id,
                        SubmissionOutbox.owner_id == actor_user.id,
                    ).order_by(SubmissionOutbox.created_at.desc()).first()
            elif target_rec_id:
                target_outbox = db.query(SubmissionOutbox).filter(SubmissionOutbox.id == target_rec_id, SubmissionOutbox.owner_id == actor_user.id).first()
                if not target_outbox:
                    target_outbox = db.query(SubmissionOutbox).filter(SubmissionOutbox.order_id == target_rec_id, SubmissionOutbox.owner_id == actor_user.id).order_by(SubmissionOutbox.created_at.desc()).first()

            if target_outbox and target_outbox.status == "RECONCILIATION_REQUIRED":
                rec = ReconciliationRecord(
                    owner_id=actor_user.id,
                    order_id=target_outbox.order_id,
                    outbox_id=target_outbox.id,
                    status="OPEN",
                )
                db.add(rec)
                db.flush()
            else:
                raise ResourceNotFoundError(f"Reconciliation case '{target_rec_id or outbox_id or order_id}' not found.")

        # 3. Check if already resolved -> 409 Conflict
        if rec.status == "RESOLVED":
            raise ConflictError(f"Reconciliation case '{rec.id}' is already resolved.")

        if rec.status != "OPEN":
            raise InvalidOperationError(f"Reconciliation case '{rec.id}' status is '{rec.status}', expected 'OPEN'.")

        # 4. Lock rows: reconciliation case, order, outbox, account, reservations
        rec = db.query(ReconciliationRecord).filter(
            ReconciliationRecord.id == rec.id
        ).with_for_update().first()
        if not rec:
            raise ResourceNotFoundError(f"Reconciliation case '{target_rec_id or outbox_id or order_id}' not found.")

        # Re-check status under lock
        if rec.status == "RESOLVED":
            raise ConflictError(f"Reconciliation case '{rec.id}' is already resolved.")
        if rec.status != "OPEN":
            raise InvalidOperationError(f"Reconciliation case '{rec.id}' status is '{rec.status}', expected 'OPEN'.")

        outbox = db.query(SubmissionOutbox).filter(
            SubmissionOutbox.id == rec.outbox_id,
            SubmissionOutbox.owner_id == actor_user.id,
        ).with_for_update().first()
        if not outbox:
            raise ResourceNotFoundError(f"Outbox operation '{rec.outbox_id}' not found.")

        order = db.query(Order).filter(
            Order.id == rec.order_id,
            Order.owner_id == actor_user.id,
        ).with_for_update().first()
        if not order:
            raise ResourceNotFoundError(f"Order '{rec.order_id}' not found.")
        if order.status != OrderStatus.RECONCILIATION_REQUIRED.value:
            raise ConflictError(f"Order '{order.id}' is already in status '{order.status}', expected 'RECONCILIATION_REQUIRED'.")

        acct = db.query(PaperAccount).filter(
            PaperAccount.id == order.account_id,
            PaperAccount.owner_id == actor_user.id,
        ).with_for_update().first()

        db.query(AccountLedgerEntry).filter(
            AccountLedgerEntry.order_id == order.id,
            AccountLedgerEntry.owner_id == actor_user.id,
        ).with_for_update().all()

        # 5. Require outbox status RECONCILIATION_REQUIRED
        if outbox.status != "RECONCILIATION_REQUIRED":
            raise InvalidOperationError(
                f"Outbox operation status is '{outbox.status}', expected 'RECONCILIATION_REQUIRED'."
            )

        # 6. Validate resolution type against PLACE or CANCEL (Reject invalid with 409 Conflict)
        if resolution_type in ("PLACE_CONFIRMED", "PLACE_REJECTED"):
            if outbox.action_type != "PLACE":
                raise ConflictError(
                    f"Resolution '{resolution_type}' is valid only for 'PLACE' operations, but outbox action is '{outbox.action_type}'."
                )
            if resolution_type == "PLACE_CONFIRMED" and not trimmed_ref:
                raise InvalidOperationError("PLACE_CONFIRMED requires a non-empty provider_order_reference.")
        elif resolution_type in ("CANCEL_CONFIRMED", "CANCEL_NOT_CONFIRMED"):
            if outbox.action_type != "CANCEL":
                raise ConflictError(
                    f"Resolution '{resolution_type}' is valid only for 'CANCEL' operations, but outbox action is '{outbox.action_type}'."
                )

        now = datetime.datetime.now(datetime.timezone.utc)
        curr_status = OrderStatus(order.status)

        # 8 & 9. Atomically set the case to RESOLVED, actor, timestamp, type, provider reference and sanitized notes.
        # This atomic conditional update guarantees concurrency safety across threads and processes.
        # Winning claim occurs strictly before any order transition, reservation credit, ledger insertion, or link creation.
        rows_updated = db.query(ReconciliationRecord).filter(
            ReconciliationRecord.id == rec.id,
            ReconciliationRecord.status == "OPEN",
        ).update(
            {
                ReconciliationRecord.status: "RESOLVED",
                ReconciliationRecord.resolution_type: resolution_type,
                ReconciliationRecord.resolved_by: actor_user.id,
                ReconciliationRecord.provider_order_reference: trimmed_ref,
                ReconciliationRecord.notes: sanitized_notes,
                ReconciliationRecord.resolved_at: now,
            },
            synchronize_session="fetch",
        )
        if rows_updated == 0:
            raise ConflictError(f"Reconciliation case '{rec.id}' is already resolved.")

        # 7. Apply the order/link/ledger result
        if resolution_type == "PLACE_CONFIRMED":
            # Create owner-scoped external_order_link atomically
            ext_link = db.query(ExternalOrderLink).filter(
                ExternalOrderLink.owner_id == actor_user.id,
                ExternalOrderLink.order_id == order.id,
            ).first()
            if not ext_link:
                ext_link = ExternalOrderLink(
                    owner_id=actor_user.id,
                    order_id=order.id,
                    provider_name="UPSTOX",
                    provider_order_id=trimmed_ref,
                    submitted_at=now,
                )
                db.add(ext_link)

            validate_order_transition(curr_status, OrderStatus.ACKNOWLEDGED, actor=actor_user.id, reason_code="PLACE_CONFIRMED")
            order.status = OrderStatus.ACKNOWLEDGED.value
            seq = db.query(func.coalesce(func.max(OrderEvent.sequence_number), 0)).filter(OrderEvent.order_id == order.id).scalar() + 1
            db.add(OrderEvent(
                order_id=order.id,
                sequence_number=seq,
                previous_status=curr_status.value,
                new_status=OrderStatus.ACKNOWLEDGED.value,
                actor=actor_user.id,
                reason_code="PLACE_CONFIRMED",
                metadata_json={"notes": sanitized_notes, "provider_ref": trimmed_ref},
            ))
            # Retains reserved cash. Creates no fill, fee or settled ledger entry.

        elif resolution_type == "PLACE_REJECTED":
            validate_order_transition(curr_status, OrderStatus.PROVIDER_REJECTED, actor=actor_user.id, reason_code="PLACE_REJECTED")
            order.status = OrderStatus.PROVIDER_REJECTED.value
            seq = db.query(func.coalesce(func.max(OrderEvent.sequence_number), 0)).filter(OrderEvent.order_id == order.id).scalar() + 1
            db.add(OrderEvent(
                order_id=order.id,
                sequence_number=seq,
                previous_status=curr_status.value,
                new_status=OrderStatus.PROVIDER_REJECTED.value,
                actor=actor_user.id,
                reason_code="PLACE_REJECTED",
                metadata_json={"notes": sanitized_notes, "provider_ref": trimmed_ref},
            ))
            # Releases remaining reservation exactly once
            SandboxService._release_order_cash_reservation(db, order, reason="PLACE_REJECTED")

        elif resolution_type == "CANCEL_CONFIRMED":
            validate_order_transition(curr_status, OrderStatus.CANCELLED, actor=actor_user.id, reason_code="CANCEL_CONFIRMED")
            order.status = OrderStatus.CANCELLED.value
            seq = db.query(func.coalesce(func.max(OrderEvent.sequence_number), 0)).filter(OrderEvent.order_id == order.id).scalar() + 1
            db.add(OrderEvent(
                order_id=order.id,
                sequence_number=seq,
                previous_status=curr_status.value,
                new_status=OrderStatus.CANCELLED.value,
                actor=actor_user.id,
                reason_code="CANCEL_CONFIRMED",
                metadata_json={"notes": sanitized_notes, "provider_ref": trimmed_ref},
            ))
            # Releases remaining reservation exactly once
            SandboxService._release_order_cash_reservation(db, order, reason="CANCEL_CONFIRMED")

        elif resolution_type == "CANCEL_NOT_CONFIRMED":
            validate_order_transition(curr_status, OrderStatus.ACKNOWLEDGED, actor=actor_user.id, reason_code="CANCEL_NOT_CONFIRMED")
            order.status = OrderStatus.ACKNOWLEDGED.value
            seq = db.query(func.coalesce(func.max(OrderEvent.sequence_number), 0)).filter(OrderEvent.order_id == order.id).scalar() + 1
            db.add(OrderEvent(
                order_id=order.id,
                sequence_number=seq,
                previous_status=curr_status.value,
                new_status=OrderStatus.ACKNOWLEDGED.value,
                actor=actor_user.id,
                reason_code="CANCEL_NOT_CONFIRMED",
                metadata_json={"notes": sanitized_notes, "provider_ref": trimmed_ref},
            ))
            # Retains reserved cash. Allows later new CANCEL operation.

        # Manual resolution must never set outbox.status = "DELIVERED"
        # The outbox must remain RECONCILIATION_REQUIRED permanently
        outbox.last_error_message = f"Manually resolved: {resolution_type}"

        # 10. Commit atomically
        try:
            db.commit()
            db.refresh(rec)
            return rec
        except IntegrityError:
            db.rollback()
            reloaded_rec = db.query(ReconciliationRecord).filter(ReconciliationRecord.id == rec.id).first()
            if reloaded_rec and reloaded_rec.status == "RESOLVED":
                raise ConflictError(f"Reconciliation case '{rec.id}' is already resolved.")
            raise

    @staticmethod
    def _release_order_cash_reservation(db: Session, order: Order, reason: str) -> None:
        """
        Releases reserved cash idempotently on cancel/rejection for BUY orders.
        """
        if order.side != OrderSide.BUY.value:
            return

        acct = db.query(PaperAccount).filter(PaperAccount.id == order.account_id).with_for_update().first()
        if not acct:
            return

        idem_key = f"order_release:{order.id}"
        existing_release = db.query(AccountLedgerEntry).filter(
            AccountLedgerEntry.owner_id == order.owner_id,
            AccountLedgerEntry.idempotency_key == idem_key,
        ).first()
        if existing_release:
            return

        remaining_qty = order.quantity_units - order.filled_quantity_units

        res_entry = db.query(AccountLedgerEntry).filter(
            AccountLedgerEntry.account_id == acct.id,
            AccountLedgerEntry.order_id == order.id,
            AccountLedgerEntry.entry_type == LedgerEntryType.CASH_RESERVATION.value,
        ).first()

        if res_entry:
            total_reserved = res_entry.amount_units
            already_released = db.query(func.coalesce(func.sum(AccountLedgerEntry.amount_units), 0)).filter(
                AccountLedgerEntry.account_id == acct.id,
                AccountLedgerEntry.order_id == order.id,
                AccountLedgerEntry.entry_type == LedgerEntryType.RESERVATION_RELEASE.value,
            ).scalar()
            unreleased = max(0, total_reserved - already_released)
            if order.quantity_units > 0 and remaining_qty < order.quantity_units:
                release_amount = (unreleased * remaining_qty) // order.quantity_units
            else:
                release_amount = unreleased
        else:
            price = order.limit_price_units or 0
            fee = (remaining_qty * price * 5) // 10000
            release_amount = (remaining_qty * price) + fee

        release_amount = min(release_amount, acct.reserved_cash_units)

        if release_amount <= 0:
            return

        acct.reserved_cash_units = max(0, acct.reserved_cash_units - release_amount)

        l_seq = db.query(func.coalesce(func.max(AccountLedgerEntry.sequence_number), 0)).filter(AccountLedgerEntry.account_id == acct.id).scalar() + 1
        ledger = AccountLedgerEntry(
            account_id=acct.id,
            owner_id=order.owner_id,
            sequence_number=l_seq,
            entry_type=LedgerEntryType.RESERVATION_RELEASE.value,
            amount_units=release_amount,
            balance_after_units=acct.total_cash_units,
            settled_cash_delta_units=0,
            reserved_cash_delta_units=-release_amount,
            settled_cash_after_units=acct.total_cash_units,
            reserved_cash_after_units=acct.reserved_cash_units,
            order_id=order.id,
            reason_code=f"ORDER_RECONCILED:{reason}",
            idempotency_key=idem_key,
        )
        db.add(ledger)
        db.flush()
