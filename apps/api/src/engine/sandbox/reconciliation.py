"""
Fail-closed Sandbox Order Reconciliation Engine.
Handles:
- Partial and complete fill reconciliation against authoritative broker status
- Exact filled and remaining quantity tracking
- Order-scoped cash reservation calculation strictly from the order's own reservation ledger
- Fail-closed handling when reservation history is missing or inconsistent (never substitute account-wide cash)
- Accurate settled cash delta and fee accounting
- Atomic position updates via AccountingEngine
- Mandatory stable broker trade/event ID with strict bounds validation (reject missing IDs; reject conflicting details)
- Concurrent fill serialization with row-level locks
- Full remaining cash reservation release upon cancellation of partially filled orders
- Strict rejection of order modifications (unsupported by outbox architecture)
- Preservation of OPEN ReconciliationRecord on partial fills while unfilled remainder outcome is ambiguous
"""
import datetime
import hashlib
import logging
from typing import Any, Dict, List, Optional, Tuple
import uuid
from sqlalchemy.orm import Session
from sqlalchemy import func, and_
from sqlalchemy.exc import IntegrityError

from src.models import (
    AccountLedgerEntry,
    ExternalOrderLink,
    Fill,
    Order,
    OrderEvent,
    PaperAccount,
    PaperPosition,
    ReconciliationRecord,
    SubmissionOutbox,
)
from src.engine.paper.accounting import AccountingEngine
from src.engine.paper.models import (
    LedgerEntryType,
    OrderSide,
    OrderStatus,
)
from src.engine.paper.state_machine import validate_order_transition

logger = logging.getLogger("tradepro.sandbox_reconciliation")


def compute_canonical_fill_idempotency_key(
    owner_id: str,
    order_id: str,
    provider_name: str,
    broker_trade_id: str,
) -> str:
    """
    Derives bounded 64-character fill idempotency key from complete owner ID,
    order ID, provider identity, and broker trade/event ID using canonical SHA-256.
    NEVER truncates raw broker trade ID.
    """
    canonical_payload = (
        f"owner={owner_id.strip()}|"
        f"order={order_id.strip()}|"
        f"provider={provider_name.strip()}|"
        f"trade={broker_trade_id.strip()}"
    )
    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()


def compute_canonical_ledger_idempotency_key(
    owner_id: str,
    order_id: str,
    provider_name: str,
    broker_trade_id: str,
    action: str,
) -> str:
    """
    Derives bounded 64-character ledger idempotency key using canonical SHA-256.
    NEVER truncates raw broker trade ID.
    """
    canonical_payload = (
        f"owner={owner_id.strip()}|"
        f"order={order_id.strip()}|"
        f"provider={provider_name.strip()}|"
        f"trade={broker_trade_id.strip()}|"
        f"action={action.strip()}"
    )
    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()


class ReconciliationError(Exception):
    """Base error for reconciliation failures."""
    pass


class OverfillError(ReconciliationError):
    """Raised when an incoming fill would cause cumulative filled quantity to exceed order quantity."""
    pass


class OrderModificationNotSupportedError(ReconciliationError):
    """Raised when order modification is attempted."""
    pass


class SandboxReconciliationEngine:
    """
    Authoritative reconciliation engine for Upstox Broker Sandbox order execution.
    """

    @staticmethod
    def reject_order_modification(order_id: str, modification_payload: Optional[Dict[str, Any]] = None) -> None:
        """
        Enforces explicit architectural rejection of in-flight order modifications.
        Under the transactional outbox model (ck_submission_outbox_action_type),
        only PLACE and CANCEL actions are supported. Order modification introduces
        asynchronous race conditions with fills; callers must use cancel-and-replace.
        """
        raise OrderModificationNotSupportedError(
            f"Order modification is not supported for order '{order_id}'. "
            "The transactional outbox architecture strictly enforces atomic PLACE and CANCEL actions. "
            "To modify an open order, cancel the existing order and submit a new replacement order."
        )

    @staticmethod
    def reconcile_fill(
        db: Session,
        order_id: str,
        owner_id: str,
        fill_qty_units: int,
        fill_price_units: int,
        fee_units: int = 0,
        fill_timestamp: Optional[datetime.datetime] = None,
        fill_idempotency_key: Optional[str] = None,
        actor: str = "SANDBOX_RECONCILIATION",
        provider_trade_id: Optional[str] = None,
        notes: Optional[str] = None,
        provider_name: str = "UPSTOX",
    ) -> Fill:
        """
        Reconciles an executed fill (partial or complete) fail-closed.
        Serializes concurrent updates using with_for_update() locks.
        Requires an authoritative, order-scoped broker trade/event ID.
        Derives bounded idempotency keys via canonical SHA-256 without truncation.
        """
        if fill_qty_units <= 0:
            raise ValueError(f"Fill quantity must be strictly positive (got {fill_qty_units}).")
        if fill_price_units <= 0:
            raise ValueError(f"Fill price must be strictly positive (got {fill_price_units}).")
        if fee_units < 0:
            raise ValueError(f"Fee must be non-negative (got {fee_units}).")

        # Require a stable, order-scoped broker trade/event ID
        raw_trade_id = provider_trade_id or fill_idempotency_key
        if not raw_trade_id or not str(raw_trade_id).strip():
            raise ValueError("Authoritative broker trade/event ID is required for fill ingestion.")
        
        clean_trade_id = str(raw_trade_id).strip()
        effective_provider = provider_name or "UPSTOX"
        
        # Derive bounded 64-character fill idempotency key from complete owner ID, order ID,
        # provider identity, and broker trade/event ID using canonical SHA-256. Never truncate raw ID!
        effective_idem_key = compute_canonical_fill_idempotency_key(
            owner_id=owner_id,
            order_id=order_id,
            provider_name=effective_provider,
            broker_trade_id=clean_trade_id,
        )

        now = datetime.datetime.now(datetime.timezone.utc)
        candle_ts = fill_timestamp or now

        # 1. Lock Order and PaperAccount
        order = (
            db.query(Order)
            .filter(Order.id == order_id, Order.owner_id == owner_id)
            .with_for_update()
            .first()
        )
        if not order:
            raise ReconciliationError(f"Order '{order_id}' not found for owner '{owner_id}'.")

        acct = (
            db.query(PaperAccount)
            .filter(PaperAccount.id == order.account_id, PaperAccount.owner_id == owner_id)
            .with_for_update()
            .first()
        )
        if not acct:
            raise ReconciliationError(f"Account '{order.account_id}' not found for order '{order_id}'.")

        # Verify order eligibility for fill reconciliation
        eligible_fill_statuses = {
            OrderStatus.PENDING_SUBMISSION.value,
            OrderStatus.ACCEPTED.value,
            OrderStatus.ACKNOWLEDGED.value,
            OrderStatus.PARTIALLY_FILLED.value,
            OrderStatus.CANCEL_PENDING.value,
            OrderStatus.RECONCILIATION_REQUIRED.value,
        }
        if order.status not in eligible_fill_statuses:
            raise ReconciliationError(
                f"Cannot reconcile fill for order '{order_id}' in non-executable status '{order.status}'."
            )

        # 2. Check duplicate fill idempotency & conflict detection
        existing_fill = (
            db.query(Fill)
            .filter(
                Fill.fill_idempotency_key == effective_idem_key,
                Fill.owner_id == owner_id,
                Fill.order_id == order.id,
            )
            .first()
        )
        if existing_fill:
            if (
                existing_fill.quantity_units != fill_qty_units
                or existing_fill.price_units != fill_price_units
                or existing_fill.fee_units != fee_units
            ):
                raise ReconciliationError(
                    f"Conflict: Broker trade ID '{clean_trade_id}' already reconciled with different details: "
                    f"existing=(qty={existing_fill.quantity_units}, price={existing_fill.price_units}, fee={existing_fill.fee_units}), "
                    f"incoming=(qty={fill_qty_units}, price={fill_price_units}, fee={fee_units})."
                )
            logger.info("Duplicate fill event ignored idempotently: key=%s", effective_idem_key)
            return existing_fill

        # 3. Check cumulative fill quantity bounds (Fail-closed overfill check)
        new_filled_total = order.filled_quantity_units + fill_qty_units
        if new_filled_total > order.quantity_units:
            raise OverfillError(
                f"Overfill rejected: Order '{order.id}' has total quantity {order.quantity_units}, "
                f"already filled {order.filled_quantity_units}, incoming fill {fill_qty_units} "
                f"would result in {new_filled_total} (exceeds order quantity)."
            )

        # 4. Lock or create PaperPosition
        pos = (
            db.query(PaperPosition)
            .filter(
                PaperPosition.account_id == acct.id,
                PaperPosition.instrument_id == order.instrument_id,
            )
            .with_for_update()
            .first()
        )
        if not pos:
            pos = PaperPosition(
                id=str(uuid.uuid4()),
                owner_id=owner_id,
                account_id=acct.id,
                instrument_id=order.instrument_id,
                net_quantity_units=0,
                average_entry_price_units=0,
                cost_basis_units=0,
                gross_realized_pnl_units=0,
                total_fees_units=0,
                net_realized_pnl_units=0,
                last_mark_price_units=fill_price_units,
                unrealized_pnl_units=0,
                updated_at=now,
            )
            db.add(pos)
            db.flush()

        # 5. Apply fill to position accounting
        pos_res = AccountingEngine.apply_fill_to_position(
            current_net_qty_units=pos.net_quantity_units,
            current_avg_price_units=pos.average_entry_price_units,
            fill_side=OrderSide(order.side),
            fill_qty_units=fill_qty_units,
            fill_price_units=fill_price_units,
            fill_fee_units=fee_units,
            allow_short=False,
        )

        pos.net_quantity_units = pos_res.new_net_quantity_units
        pos.average_entry_price_units = pos_res.new_average_entry_price_units
        pos.cost_basis_units = pos_res.new_cost_basis_units
        pos.gross_realized_pnl_units += pos_res.incremental_gross_realized_pnl_units
        pos.total_fees_units += pos_res.incremental_fees_units
        pos.net_realized_pnl_units += pos_res.incremental_net_realized_pnl_units
        pos.last_mark_price_units = fill_price_units
        pos.updated_at = now

        # 6. Cash and Reservation Accounting (Blocker 3: Order-scoped ledger calculation)
        fill_cost = fill_qty_units * fill_price_units
        max_seq = (
            db.query(func.coalesce(func.max(AccountLedgerEntry.sequence_number), 0))
            .filter(AccountLedgerEntry.account_id == acct.id)
            .scalar()
        )
        l_seq = max_seq + 1

        is_full = (new_filled_total == order.quantity_units)

        if order.side == OrderSide.BUY.value:
            # Deduct settled cash
            total_charge = fill_cost + fee_units
            acct.total_cash_units -= total_charge

            # Calculate order's remaining reservation strictly from its own ledger history
            order_res_entries = (
                db.query(AccountLedgerEntry)
                .filter(
                    AccountLedgerEntry.account_id == acct.id,
                    AccountLedgerEntry.order_id == order.id,
                )
                .order_by(AccountLedgerEntry.sequence_number.asc())
                .all()
            )
            res_entry = next((e for e in order_res_entries if e.entry_type == LedgerEntryType.CASH_RESERVATION.value), None)
            if not res_entry or res_entry.amount_units <= 0:
                raise ReconciliationError(
                    f"Missing or invalid authoritative cash reservation ledger entry for BUY order '{order.id}'. Fail-closed."
                )
            
            initial_order_reservation = res_entry.amount_units
            prior_releases = sum(
                abs(e.reserved_cash_delta_units)
                for e in order_res_entries
                if e.reserved_cash_delta_units < 0
            )
            if prior_releases > initial_order_reservation:
                raise ReconciliationError(
                    f"Inconsistent ledger history for order '{order.id}': prior releases ({prior_releases}) "
                    f"exceed initial reservation ({initial_order_reservation}). Fail-closed."
                )
            
            order_remaining_reservation = initial_order_reservation - prior_releases

            if is_full:
                # Final fill: cleanly sweep all remaining reservation allocated to this order
                reserved_released = order_remaining_reservation
            else:
                proportional_release = (initial_order_reservation * fill_qty_units) // order.quantity_units
                reserved_released = min(proportional_release, order_remaining_reservation)

            if reserved_released > acct.reserved_cash_units:
                raise ReconciliationError(
                    f"Account reserved cash ({acct.reserved_cash_units}) is less than order '{order.id}' "
                    f"release amount ({reserved_released}). Inconsistent ledger state. Fail-closed."
                )

            acct.reserved_cash_units -= reserved_released

            db.add(
                AccountLedgerEntry(
                    account_id=acct.id,
                    owner_id=owner_id,
                    sequence_number=l_seq,
                    entry_type=LedgerEntryType.BUY_FILL.value,
                    amount_units=-total_charge,
                    balance_after_units=acct.total_cash_units,
                    settled_cash_delta_units=-total_charge,
                    reserved_cash_delta_units=-reserved_released,
                    settled_cash_after_units=acct.total_cash_units,
                    reserved_cash_after_units=acct.reserved_cash_units,
                    order_id=order.id,
                    reason_code="SANDBOX_BUY_FILL",
                    idempotency_key=compute_canonical_ledger_idempotency_key(
                        owner_id=owner_id,
                        order_id=order.id,
                        provider_name=effective_provider,
                        broker_trade_id=clean_trade_id,
                        action="BUY_FILL_LEDGER",
                    ),
                )
            )

        else:
            # SELL order: proceeds are credited
            net_proceeds = fill_cost - fee_units
            acct.total_cash_units += net_proceeds

            db.add(
                AccountLedgerEntry(
                    account_id=acct.id,
                    owner_id=owner_id,
                    sequence_number=l_seq,
                    entry_type=LedgerEntryType.SELL_FILL.value,
                    amount_units=net_proceeds,
                    balance_after_units=acct.total_cash_units,
                    settled_cash_delta_units=net_proceeds,
                    reserved_cash_delta_units=0,
                    settled_cash_after_units=acct.total_cash_units,
                    reserved_cash_after_units=acct.reserved_cash_units,
                    order_id=order.id,
                    reason_code="SANDBOX_SELL_FILL",
                    idempotency_key=compute_canonical_ledger_idempotency_key(
                        owner_id=owner_id,
                        order_id=order.id,
                        provider_name=effective_provider,
                        broker_trade_id=clean_trade_id,
                        action="SELL_FILL_LEDGER",
                    ),
                )
            )

        # 7. Create Fill Record
        db_fill = Fill(
            id=str(uuid.uuid4()),
            owner_id=owner_id,
            order_id=order.id,
            account_id=acct.id,
            instrument_id=order.instrument_id,
            side=order.side,
            quantity_units=fill_qty_units,
            price_units=fill_price_units,
            fee_units=fee_units,
            candle_timestamp=candle_ts,
            fill_idempotency_key=effective_idem_key,
            created_at=now,
        )
        db.add(db_fill)

        # 8. Update Order status and append OrderEvent
        new_status = OrderStatus.FILLED.value if is_full else OrderStatus.PARTIALLY_FILLED.value
        curr_status = OrderStatus(order.status)

        validate_order_transition(
            current_status=curr_status,
            new_status=OrderStatus(new_status),
            actor=actor,
            reason_code="FILL_RECONCILED",
        )

        order.filled_quantity_units = new_filled_total
        order.status = new_status

        evt_seq = (
            db.query(func.coalesce(func.max(OrderEvent.sequence_number), 0))
            .filter(OrderEvent.order_id == order.id)
            .scalar()
            + 1
        )
        db.add(
            OrderEvent(
                order_id=order.id,
                sequence_number=evt_seq,
                previous_status=curr_status.value,
                new_status=new_status,
                actor=actor,
                reason_code="FILL_RECONCILED",
                metadata_json={
                    "fill_qty_units": fill_qty_units,
                    "fill_price_units": fill_price_units,
                    "fee_units": fee_units,
                    "cumulative_filled": new_filled_total,
                    "remaining_qty": order.quantity_units - new_filled_total,
                    "provider_trade_id": clean_trade_id,
                    "notes": notes,
                },
            )
        )

        # 9. Update open ReconciliationRecord (Blocker 4: partial fill keeps OPEN)
        open_recon = (
            db.query(ReconciliationRecord)
            .filter(
                ReconciliationRecord.order_id == order.id,
                ReconciliationRecord.owner_id == owner_id,
                ReconciliationRecord.status == "OPEN",
            )
            .first()
        )
        if open_recon:
            if is_full:
                open_recon.status = "RESOLVED"
                open_recon.resolution_type = "PLACE_CONFIRMED"
                open_recon.resolved_by = owner_id
                open_recon.resolved_at = now
                open_recon.notes = (
                    f"Resolved via complete fill execution: {new_filled_total}/{order.quantity_units} units filled. "
                    f"Order status is FILLED."
                )
                open_recon.provider_order_reference = clean_trade_id
            else:
                # Partial fill must NOT resolve OPEN record while remainder is ambiguous
                open_recon.status = "OPEN"
                open_recon.notes = (
                    f"Partial fill received: {fill_qty_units} units @ {fill_price_units} "
                    f"(cumulative {new_filled_total}/{order.quantity_units}). "
                    f"Unfilled remainder ({order.quantity_units - new_filled_total}) remains ambiguous and open for reconciliation."
                )

        db.flush()
        return db_fill

    @staticmethod
    def reconcile_cancel(
        db: Session,
        order_id: str,
        owner_id: str,
        actor: str = "SANDBOX_RECONCILIATION",
        reason: str = "BROKER_CANCEL_CONFIRMED",
        provider_name: str = "UPSTOX",
    ) -> None:
        """
        Reconciles cancellation of an open or partially filled order.
        Releases all remaining unreleased cash reservations strictly from this order's own reservation ledger.
        Resolves open ReconciliationRecord on verified cancellation.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        order = (
            db.query(Order)
            .filter(Order.id == order_id, Order.owner_id == owner_id)
            .with_for_update()
            .first()
        )
        if not order:
            raise ReconciliationError(f"Order '{order_id}' not found for owner '{owner_id}'.")

        eligible_cancel_statuses = {
            OrderStatus.PENDING_SUBMISSION.value,
            OrderStatus.ACCEPTED.value,
            OrderStatus.ACKNOWLEDGED.value,
            OrderStatus.PARTIALLY_FILLED.value,
            OrderStatus.CANCEL_PENDING.value,
            OrderStatus.RECONCILIATION_REQUIRED.value,
        }
        if order.status not in eligible_cancel_statuses:
            raise ReconciliationError(
                f"Cannot reconcile cancel for order '{order_id}' in non-cancellable status '{order.status}'."
            )

        curr_status = OrderStatus(order.status)
        validate_order_transition(
            current_status=curr_status,
            new_status=OrderStatus.CANCELLED,
            actor=actor,
            reason_code=reason,
        )

        acct = (
            db.query(PaperAccount)
            .filter(PaperAccount.id == order.account_id, PaperAccount.owner_id == owner_id)
            .with_for_update()
            .first()
        )

        # Release remaining reserved cash for BUY orders strictly from this order's ledger (Blocker 3)
        if order.side == OrderSide.BUY.value and acct:
            remaining_qty = order.quantity_units - order.filled_quantity_units
            if remaining_qty > 0 and acct.reserved_cash_units > 0:
                order_res_entries = (
                    db.query(AccountLedgerEntry)
                    .filter(
                        AccountLedgerEntry.account_id == acct.id,
                        AccountLedgerEntry.order_id == order.id,
                    )
                    .order_by(AccountLedgerEntry.sequence_number.asc())
                    .all()
                )
                res_entry = next((e for e in order_res_entries if e.entry_type == LedgerEntryType.CASH_RESERVATION.value), None)
                if not res_entry or res_entry.amount_units <= 0:
                    raise ReconciliationError(
                        f"Missing or invalid authoritative cash reservation ledger entry for BUY order '{order.id}'. Fail-closed."
                    )
                initial_order_reservation = res_entry.amount_units
                prior_releases = sum(
                    abs(e.reserved_cash_delta_units)
                    for e in order_res_entries
                    if e.reserved_cash_delta_units < 0
                )
                if prior_releases > initial_order_reservation:
                    raise ReconciliationError(
                        f"Inconsistent ledger history for order '{order.id}': prior releases ({prior_releases}) "
                        f"exceed initial reservation ({initial_order_reservation}). Fail-closed."
                    )
                order_remaining_reservation = initial_order_reservation - prior_releases
                release_amount = order_remaining_reservation

                if release_amount > acct.reserved_cash_units:
                    raise ReconciliationError(
                        f"Account reserved cash ({acct.reserved_cash_units}) is less than order '{order.id}' "
                        f"cancel release amount ({release_amount}). Inconsistent ledger state. Fail-closed."
                    )

                if release_amount > 0:
                    acct.reserved_cash_units -= release_amount
                    max_seq = (
                        db.query(func.coalesce(func.max(AccountLedgerEntry.sequence_number), 0))
                        .filter(AccountLedgerEntry.account_id == acct.id)
                        .scalar()
                    )
                    l_seq = max_seq + 1
                    db.add(
                        AccountLedgerEntry(
                            account_id=acct.id,
                            owner_id=owner_id,
                            sequence_number=l_seq,
                            entry_type=LedgerEntryType.RESERVATION_RELEASE.value,
                            amount_units=release_amount,
                            balance_after_units=acct.total_cash_units,
                            settled_cash_delta_units=0,
                            reserved_cash_delta_units=-release_amount,
                            settled_cash_after_units=acct.total_cash_units,
                            reserved_cash_after_units=acct.reserved_cash_units,
                            order_id=order.id,
                            reason_code=f"CANCEL_RELEASE:{reason}",
                            idempotency_key=compute_canonical_ledger_idempotency_key(
                                owner_id=owner_id,
                                order_id=order.id,
                                provider_name=provider_name or "UPSTOX",
                                broker_trade_id="CANCEL",
                                action="CANCEL_RELEASE",
                            ),
                        )
                    )

        order.status = OrderStatus.CANCELLED.value

        evt_seq = (
            db.query(func.coalesce(func.max(OrderEvent.sequence_number), 0))
            .filter(OrderEvent.order_id == order.id)
            .scalar()
            + 1
        )
        db.add(
            OrderEvent(
                order_id=order.id,
                sequence_number=evt_seq,
                previous_status=curr_status.value,
                new_status=OrderStatus.CANCELLED.value,
                actor=actor,
                reason_code=reason,
                metadata_json={"cancelled_remaining_quantity": order.quantity_units - order.filled_quantity_units},
            )
        )

        # Resolve open ReconciliationRecord on authoritative whole-order cancellation (Blocker 4)
        open_recon = (
            db.query(ReconciliationRecord)
            .filter(
                ReconciliationRecord.order_id == order.id,
                ReconciliationRecord.owner_id == owner_id,
                ReconciliationRecord.status == "OPEN",
            )
            .first()
        )
        if open_recon:
            open_recon.status = "RESOLVED"
            open_recon.resolution_type = "CANCEL_CONFIRMED"
            open_recon.resolved_by = owner_id
            open_recon.resolved_at = now
            open_recon.notes = f"Resolved via broker cancellation: {reason}."

        db.flush()
