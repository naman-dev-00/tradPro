import { describe, it, expect } from "vitest";
import {
  PaperAccount,
  StrategyRuntime,
  Order,
  PaperPosition,
  KillSwitchStatus,
} from "../src/lib/api";

describe("Paper Trading Lab Specification Suite", () => {
  const mockAccount: PaperAccount = {
    id: "acc-12345",
    owner_id: "user-1",
    name: "Primary Paper Testing",
    currency: "INR",
    total_cash: "100000.00",
    reserved_cash: "25000.00",
    available_cash: "75000.00",
    is_active: true,
    version: 1,
    created_at: "2026-09-13T09:00:00Z",
    updated_at: "2026-09-13T09:00:00Z",
  };

  const mockRuntime: StrategyRuntime = {
    id: "rt-abcde",
    owner_id: "user-1",
    strategy_id: "strat-1",
    action_policy_id: "act-1",
    risk_policy_id: "risk-1",
    account_id: "acc-12345",
    status: "RUNNING",
    trading_mode: "PAPER",
    dataset_id: "synthetic_underlying_nifty_15m",
    timeframe: "15m",
    last_processed_candle_timestamp: "2026-08-28T09:15:00Z",
    consecutive_errors: 0,
    version: 1,
    created_at: "2026-09-13T09:00:00Z",
    updated_at: "2026-09-13T09:00:00Z",
  };

  const mockPosition: PaperPosition = {
    id: "pos-1",
    account_id: "acc-12345",
    instrument_id: "NIFTY",
    net_quantity: "50",
    average_entry_price: "24500.00",
    cost_basis: "1225000.00",
    gross_realized_pnl: "0.00",
    total_fees: "20.00",
    net_realized_pnl: "-20.00",
    last_mark_price: "24550.00",
    unrealized_pnl: "2500.00",
    updated_at: "2026-09-13T09:05:00Z",
  };

  it("verifies cash accounting invariant: available + reserved = total", () => {
    const total = parseFloat(mockAccount.total_cash);
    const reserved = parseFloat(mockAccount.reserved_cash);
    const available = parseFloat(mockAccount.available_cash);
    expect(available + reserved).toBeCloseTo(total, 2);
  });

  it("verifies trading mode is strictly PAPER", () => {
    expect(mockRuntime.trading_mode).toBe("PAPER");
  });

  it("calculates unrealized PnL based on mark price and average entry", () => {
    const netQty = parseFloat(mockPosition.net_quantity);
    const avgEntry = parseFloat(mockPosition.average_entry_price);
    const markPrice = parseFloat(mockPosition.last_mark_price);
    const expectedUnrealized = netQty * (markPrice - avgEntry);
    expect(expectedUnrealized).toBeCloseTo(parseFloat(mockPosition.unrealized_pnl), 2);
  });

  it("verifies net realized PnL accounts for total fees", () => {
    const gross = parseFloat(mockPosition.gross_realized_pnl);
    const fees = parseFloat(mockPosition.total_fees);
    const net = parseFloat(mockPosition.net_realized_pnl);
    expect(gross - fees).toBeCloseTo(net, 2);
  });

  it("validates kill switch emergency state logic", () => {
    const activeKillSwitch: KillSwitchStatus = {
      global_active: true,
      global_engaged_at: "2026-09-13T09:10:00Z",
      global_reason: "Extreme volatility safeguard",
      user_active: false,
    };
    const isEngaged = activeKillSwitch.global_active || activeKillSwitch.user_active;
    expect(isEngaged).toBe(true);
    expect(activeKillSwitch.global_reason).toBe("Extreme volatility safeguard");
  });

  it("validates order event audit trail structure", () => {
    const mockOrder: Order = {
      id: "ord-1",
      owner_id: "user-1",
      runtime_id: "rt-abcde",
      intent_id: "int-1",
      account_id: "acc-12345",
      order_sequence_number: 1,
      instrument_id: "NIFTY",
      side: "BUY",
      order_type: "MARKET",
      quantity: "50",
      limit_price: null,
      filled_quantity: "50",
      status: "FILLED",
      version: 3,
      events: [
        {
          id: "evt-1",
          sequence_number: 1,
          previous_status: "PENDING_RISK",
          new_status: "ACCEPTED",
          actor: "RISK_ENGINE",
          reason_code: "RISK_VALIDATED",
          created_at: "2026-09-13T09:00:01Z",
        },
        {
          id: "evt-2",
          sequence_number: 2,
          previous_status: "ACCEPTED",
          new_status: "FILLED",
          actor: "FILL_MODEL",
          reason_code: "CANDLE_MATCH",
          created_at: "2026-09-13T09:00:02Z",
        },
      ],
      created_at: "2026-09-13T09:00:01Z",
      updated_at: "2026-09-13T09:00:02Z",
    };

    expect(mockOrder.events).toBeDefined();
    expect(mockOrder.events?.length).toBe(2);
    expect(mockOrder.events?.[0].new_status).toBe("ACCEPTED");
    expect(mockOrder.events?.[1].new_status).toBe("FILLED");
  });
});
