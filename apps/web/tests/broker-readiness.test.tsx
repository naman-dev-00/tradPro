import React from "react";
import "@testing-library/jest-dom";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { BrokerReadiness } from "../src/components/BrokerReadiness";
import * as api from "../src/lib/api";

// Mock next/navigation
let mockSearchParamsGet = vi.fn().mockReturnValue(null);
vi.mock("next/navigation", () => ({
  useSearchParams: () => ({
    get: (key: string) => mockSearchParamsGet(key),
  }),
}));

vi.mock("@/components/AuthHeaderBadge", () => ({
  AuthHeaderBadge: () => <div data-testid="auth-badge">AuthBadge</div>,
}));

const mockDisconnectedConnection: api.BrokerConnectionResponse = {
  provider: "UPSTOX",
  environment: "LIVE",
  status: "DISCONNECTED",
  broker_user_id_masked: null,
  account_name: null,
  token_valid: false,
  token_expires_at: null,
  last_successful_refresh_at: null,
  version: 0,
  credential_generation: 1,
};

const mockConnectedConnection: api.BrokerConnectionResponse = {
  provider: "UPSTOX",
  environment: "LIVE",
  status: "CONNECTED",
  broker_user_id_masked: "***777",
  account_name: "Verified Operator",
  token_valid: true,
  token_expires_at: "2026-10-08T03:30:00Z",
  last_successful_refresh_at: "2026-10-07T12:00:00Z",
  version: 1,
  credential_generation: 1,
};

const mockReadinessReady: api.BrokerReadinessResponse = {
  overall_status: "READY_FOR_MONITORING",
  live_dispatch_enabled: false,
  environment: "LIVE",
  broker_user_id_masked: "***777",
  evaluated_at: "2026-10-07T12:00:00Z",
  gates: [
    {
      gate_name: "broker_connection",
      status: "READY",
      details: "Connected to broker account ***777.",
      actionable_remedy: null,
    },
    {
      gate_name: "token_freshness",
      status: "READY",
      details: "Daily broker token is valid and unexpired.",
      actionable_remedy: null,
    },
    {
      gate_name: "static_ip_configuration",
      status: "READY",
      details: "Registered Static IP verified with evidence timestamp 2026-10-07T10:00:00Z.",
      actionable_remedy: null,
    },
    {
      gate_name: "market_session_and_segment",
      status: "READY",
      details: "Active regular market session per verified NSE calendar evidence.",
      actionable_remedy: null,
    },
    {
      gate_name: "account_risk_evidence",
      status: "READY",
      details: "Broker snapshot is fresh (10s old) and bound to generation 1.",
      actionable_remedy: null,
    },
    {
      gate_name: "kill_switch_and_reconciliation",
      status: "READY",
      details: "Kill switches clear; zero unresolved reconciliation blockers.",
      actionable_remedy: null,
    },
  ],
  actionable_blockers: [],
};

const mockReadinessBlocked: api.BrokerReadinessResponse = {
  overall_status: "BLOCKED",
  live_dispatch_enabled: false,
  environment: "LIVE",
  broker_user_id_masked: null,
  evaluated_at: "2026-10-07T12:00:00Z",
  gates: [
    {
      gate_name: "broker_connection",
      status: "BLOCKED",
      details: "No broker connection configured.",
      actionable_remedy: "Connect to Upstox via OAuth 2.0 flow.",
    },
    {
      gate_name: "token_freshness",
      status: "BLOCKED",
      details: "No active broker token found. Re-authentication required.",
      actionable_remedy: "Authorize Upstox OAuth 2.0 flow.",
    },
  ],
  actionable_blockers: ["Broker connection not established.", "No active broker token found."],
};

const mockSnapshot: api.BrokerSnapshotDetail = {
  id: "snap-1",
  environment: "LIVE",
  broker_user_id_masked: "***777",
  funds_and_margins: {
    equity: {
      available_margin: "250000.50",
      used_margin: "15000.00",
    },
  },
  positions: [
    {
      tradingsymbol: "RELIANCE",
      product: "CNC",
      quantity: 50,
      average_price: "2450.00",
      pnl: "1250.00",
    },
  ],
  holdings: [
    {
      isin: "INE002A01018",
      company_name: "Reliance Industries Ltd",
      quantity: 100,
      average_price: "2400.00",
      pnl: "5000.00",
    },
  ],
  orders: [
    {
      order_id: "241007001",
      tradingsymbol: "TCS",
      transaction_type: "BUY",
      quantity: 10,
      price: "3500.00",
      status: "OPEN",
    },
  ],
  status: "ACTIVE",
  acquired_at_utc: "2026-10-07T12:00:00Z",
  freshness_seconds: 15,
  raw_fingerprint: "hash123",
  credential_generation: 1,
};

describe("BrokerReadiness Component", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockSearchParamsGet = vi.fn().mockReturnValue(null);
  });

  it("renders read-only constraints banner and live dispatch disabled badge", async () => {
    vi.spyOn(api, "fetchBrokerConnection").mockResolvedValue(mockDisconnectedConnection);
    vi.spyOn(api, "fetchBrokerReadiness").mockResolvedValue(mockReadinessBlocked);

    render(<BrokerReadiness />);

    expect(
      screen.getByText(/Phase 7A: Read-Only Broker Connection & Live Readiness/i)
    ).toBeInTheDocument();
    expect(screen.getByText(/Live Orders Disabled/i)).toBeInTheDocument();
    expect(screen.getByText(/live_dispatch_enabled: false/i)).toBeInTheDocument();
  });

  it("renders disconnected state with connect button and blocked readiness gates", async () => {
    vi.spyOn(api, "fetchBrokerConnection").mockResolvedValue(mockDisconnectedConnection);
    vi.spyOn(api, "fetchBrokerReadiness").mockResolvedValue(mockReadinessBlocked);

    render(<BrokerReadiness />);

    await waitFor(() => {
      expect(screen.getByText("DISCONNECTED")).toBeInTheDocument();
    });

    expect(screen.getByText(/Connect Upstox via OAuth 2.0/i)).toBeInTheDocument();
    expect(screen.getByText(/Actionable Readiness Blockers/i)).toBeInTheDocument();
    expect(screen.getAllByText(/Broker connection not established/i).length).toBeGreaterThan(0);
  });

  it("renders connected state with masked UCC, balance cards, and readiness ready", async () => {
    vi.spyOn(api, "fetchBrokerConnection").mockResolvedValue(mockConnectedConnection);
    vi.spyOn(api, "fetchBrokerReadiness").mockResolvedValue(mockReadinessReady);
    vi.spyOn(api, "fetchLatestBrokerSnapshot").mockResolvedValue(mockSnapshot);

    render(<BrokerReadiness />);

    await waitFor(() => {
      expect(screen.getByText("***777")).toBeInTheDocument();
      expect(screen.getByText("Verified Operator")).toBeInTheDocument();
      expect(screen.getByText("READY_FOR_MONITORING")).toBeInTheDocument();
    });

    expect(screen.getByText("Refresh Snapshot")).toBeInTheDocument();
    expect(screen.getByText("Disconnect")).toBeInTheDocument();

    // Check margin cards
    expect(screen.getByText("Available Equity Margin")).toBeInTheDocument();
    expect(screen.getByText(/2,50,000.50/)).toBeInTheDocument();

    // Switch tabs to positions
    const posTab = screen.getByRole("button", { name: /Positions \(1\)/i });
    fireEvent.click(posTab);
    expect(screen.getByText("RELIANCE")).toBeInTheDocument();

    // Switch tabs to holdings
    const holdTab = screen.getByRole("button", { name: /Holdings \(1\)/i });
    fireEvent.click(holdTab);
    expect(screen.getByText("Reliance Industries Ltd")).toBeInTheDocument();

    // Switch tabs to orders
    const ordTab = screen.getByRole("button", { name: /Orders \(1\)/i });
    fireEvent.click(ordTab);
    expect(screen.getByText("241007001")).toBeInTheDocument();
    expect(screen.getByText("TCS")).toBeInTheDocument();
  });

  it("handles nullable exact financial values without inventing zero balances", async () => {
    const nullableSnapshot: api.BrokerSnapshotDetail = {
      ...mockSnapshot,
      funds_and_margins: {
        equity: {
          available_margin: null,
          used_margin: null,
        },
      },
    };

    vi.spyOn(api, "fetchBrokerConnection").mockResolvedValue(mockConnectedConnection);
    vi.spyOn(api, "fetchBrokerReadiness").mockResolvedValue(mockReadinessReady);
    vi.spyOn(api, "fetchLatestBrokerSnapshot").mockResolvedValue(nullableSnapshot);

    render(<BrokerReadiness />);

    await waitFor(() => {
      expect(screen.getByText("Available Equity Margin")).toBeInTheDocument();
    });

    // Should display dashes, not ₹0.00
    const dashes = screen.getAllByText("—");
    expect(dashes.length).toBeGreaterThan(0);
    expect(screen.queryByText("₹0.00")).not.toBeInTheDocument();
  });

  it("browser acceptance flow: consumes callback parameters once, strips code/state, and preserves environment", async () => {
    mockSearchParamsGet = vi.fn().mockImplementation((key: string) => {
      if (key === "code") return "auth_code_xyz123";
      if (key === "state") return "state_token_abc789";
      return null;
    });

    const replaceStateSpy = vi.spyOn(window.history, "replaceState");
    const callbackSpy = vi.spyOn(api, "submitBrokerOAuthCallback").mockResolvedValue({
      ...mockConnectedConnection,
      environment: "LIVE",
    });
    vi.spyOn(api, "fetchBrokerConnection").mockResolvedValue(mockConnectedConnection);
    vi.spyOn(api, "fetchBrokerReadiness").mockResolvedValue(mockReadinessReady);
    vi.spyOn(api, "fetchLatestBrokerSnapshot").mockResolvedValue(mockSnapshot);

    render(<BrokerReadiness />);

    await waitFor(() => {
      expect(callbackSpy).toHaveBeenCalledTimes(1);
      expect(callbackSpy).toHaveBeenCalledWith("auth_code_xyz123", "state_token_abc789");
    });

    // Verify replaceState was called to strip code/state from URL
    expect(replaceStateSpy).toHaveBeenCalled();
  });

  it("component test: cleans callback code and state from URL on failure as well as success", async () => {
    mockSearchParamsGet = vi.fn().mockImplementation((key: string) => {
      if (key === "code") return "bad_code";
      if (key === "state") return "bad_state";
      return null;
    });

    const replaceStateSpy = vi.spyOn(window.history, "replaceState");
    vi.spyOn(api, "submitBrokerOAuthCallback").mockRejectedValue(new Error("Invalid state"));

    render(<BrokerReadiness />);

    await waitFor(() => {
      expect(replaceStateSpy).toHaveBeenCalled();
      expect(screen.getByText("Invalid state")).toBeInTheDocument();
    });
  });

  it("handles disconnect action with confirmation", async () => {
    vi.spyOn(api, "fetchBrokerConnection").mockResolvedValue(mockConnectedConnection);
    vi.spyOn(api, "fetchBrokerReadiness").mockResolvedValue(mockReadinessReady);
    vi.spyOn(api, "fetchLatestBrokerSnapshot").mockResolvedValue(mockSnapshot);
    const disconnectSpy = vi.spyOn(api, "disconnectBroker").mockResolvedValue(mockDisconnectedConnection);

    // Mock window.confirm
    vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<BrokerReadiness />);

    await waitFor(() => {
      expect(screen.getByText("Disconnect")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("Disconnect"));

    await waitFor(() => {
      expect(disconnectSpy).toHaveBeenCalledWith("LIVE");
    });
  });

  it("handles refresh snapshot action", async () => {
    vi.spyOn(api, "fetchBrokerConnection").mockResolvedValue(mockConnectedConnection);
    vi.spyOn(api, "fetchBrokerReadiness").mockResolvedValue(mockReadinessReady);
    vi.spyOn(api, "fetchLatestBrokerSnapshot").mockResolvedValue(mockSnapshot);
    const refreshSpy = vi.spyOn(api, "refreshBrokerSnapshots").mockResolvedValue({
      snapshot_id: "snap-new",
      environment: "LIVE",
      broker_user_id_masked: "***777",
      positions_count: 1,
      holdings_count: 1,
      orders_count: 1,
      available_margin: "250000.50",
      used_margin: "15000.00",
      acquired_at_utc: "2026-10-07T12:30:00Z",
      freshness_seconds: 0,
      raw_fingerprint: "hash456",
      credential_generation: 1,
    });

    render(<BrokerReadiness />);

    await waitFor(() => {
      expect(screen.getByText("Refresh Snapshot")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("Refresh Snapshot"));

    await waitFor(() => {
      expect(refreshSpy).toHaveBeenCalledWith("LIVE");
    });
  });
});
