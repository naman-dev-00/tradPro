import React from "react";
import "@testing-library/jest-dom";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MarketDataLab } from "../src/components/MarketDataLab";
import * as api from "../src/lib/api";

vi.mock("@/components/AuthHeaderBadge", () => ({
  AuthHeaderBadge: () => <div data-testid="auth-badge">AuthBadge</div>,
}));

const mockReadinessEnabled: api.MarketDataReadinessResponse = {
  network_enabled: true,
  credential_configured: true,
  operator_configured: true,
  is_authorized_operator: true,
  status: "CONFIGURED_AND_ENABLED",
  base_url: "https://api.upstox.com",
  approved_hosts: ["api.upstox.com"],
  supported_timeframes: ["5m", "15m"],
};

const mockReadinessDisabled: api.MarketDataReadinessResponse = {
  network_enabled: false,
  credential_configured: false,
  operator_configured: false,
  is_authorized_operator: false,
  status: "NETWORK_DISABLED",
  base_url: "https://api.upstox.com",
  approved_hosts: ["api.upstox.com"],
  supported_timeframes: ["5m", "15m"],
};

const mockReadinessOperatorMissing: api.MarketDataReadinessResponse = {
  network_enabled: true,
  credential_configured: true,
  operator_configured: false,
  is_authorized_operator: false,
  status: "OPERATOR_NOT_CONFIGURED",
  base_url: "https://api.upstox.com",
  approved_hosts: ["api.upstox.com"],
  supported_timeframes: ["5m", "15m"],
};

const mockInstruments: api.MarketDataInstrument[] = [
  {
    instrument_key: "NSE_INDEX|Nifty 50",
    tradepro_instrument_id: "NIFTY50_INDEX",
    name: "Nifty 50 Index",
    exchange: "NSE",
    segment: "INDEX",
    lot_size: 25,
    tick_size: "0.05",
    supported_timeframes: ["5m", "15m"],
  },
  {
    instrument_key: "NSE_EQ|INE002A01018",
    tradepro_instrument_id: "RELIANCE_EQ",
    name: "Reliance Industries Ltd",
    exchange: "NSE",
    segment: "EQ",
    lot_size: 1,
    tick_size: "0.05",
    supported_timeframes: ["5m", "15m"],
  },
];

const mockCandlesResponse: api.MarketDataCandlesResponse = {
  instrument_key: "NSE_INDEX|Nifty 50",
  tradepro_instrument_id: "NIFTY50_INDEX",
  timeframe: "5m",
  mode: "intraday",
  candles: [
    {
      timestamp: "2026-10-05T09:15:00+00:00",
      open: "25200.0000",
      high: "25250.0000",
      low: "25180.0000",
      close: "25230.0000",
      open_units: 252000000,
      high_units: 252500000,
      low_units: 251800000,
      close_units: 252300000,
      volume: 75000,
      is_closed: true,
    },
    {
      timestamp: "2026-10-05T09:20:00+00:00",
      open: "25230.0000",
      high: "25270.0000",
      low: "25220.0000",
      close: "25260.0000",
      open_units: 252300000,
      high_units: 252700000,
      low_units: 252200000,
      close_units: 252600000,
      volume: 82000,
      is_closed: true,
    },
  ],
  provenance: {
    provider: "UPSTOX",
    source_type: "PROVIDER_UPSTOX_V3",
    retrieved_at: "2026-10-05T10:00:00Z",
    requested_instrument_key: "NSE_INDEX|Nifty 50",
    timeframe: "5m",
    mode: "intraday",
    date_range: null,
    candle_count: 2,
    content_fingerprint: "a1b2c3d4e5f67890abcdef1234567890abcdef1234567890abcdef1234567890",
    completeness: "COMPLETE",
    is_complete_series: true,
    warnings: [],
  },
};

describe("MarketDataLab Component Specification", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders connection readiness, approved hosts, and instruments", async () => {
    vi.spyOn(api, "fetchMarketDataReadiness").mockResolvedValue(mockReadinessEnabled);
    vi.spyOn(api, "fetchMarketDataInstruments").mockResolvedValue(mockInstruments);

    render(<MarketDataLab />);

    expect(screen.getByText(/Market Data & Provider Inspection Lab/i)).toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByText(/Configured & Enabled/i)).toBeInTheDocument();
      expect(screen.getByText("api.upstox.com")).toBeInTheDocument();
      expect(screen.getByText(/5m, 15m Supported/i)).toBeInTheDocument();
    });

    const select = screen.getByLabelText(/Instrument/i) as HTMLSelectElement;
    expect(select.options.length).toBe(2);
    expect(screen.getByText(/Nifty 50 Index \(NSE - INDEX\)/i)).toBeInTheDocument();
  });

  it("displays Network Disabled state and disables fetch button when offline", async () => {
    vi.spyOn(api, "fetchMarketDataReadiness").mockResolvedValue(mockReadinessDisabled);
    vi.spyOn(api, "fetchMarketDataInstruments").mockResolvedValue(mockInstruments);

    render(<MarketDataLab />);

    await waitFor(() => {
      expect(screen.getByText(/Network Disabled/i)).toBeInTheDocument();
    });

    const btn = screen.getByRole("button", { name: /Acquire Completed Candles/i });
    expect(btn).toBeDisabled();
    expect(screen.getByText(/External network calls are disabled by server policy/i)).toBeInTheDocument();
  });

  it("disables fetch button when operator is not configured", async () => {
    vi.spyOn(api, "fetchMarketDataReadiness").mockResolvedValue(mockReadinessOperatorMissing);
    vi.spyOn(api, "fetchMarketDataInstruments").mockResolvedValue(mockInstruments);

    render(<MarketDataLab />);

    await waitFor(() => {
      expect(screen.getByText(/Operator Not Configured/i)).toBeInTheDocument();
    });

    const btn = screen.getByRole("button", { name: /Acquire Completed Candles/i });
    expect(btn).toBeDisabled();
    expect(screen.getByText(/UPSTOX_MARKET_DATA_OWNER_ID\) is not configured/i)).toBeInTheDocument();
  });

  it("surfaces readiness verification failure if readiness API throws", async () => {
    vi.spyOn(api, "fetchMarketDataReadiness").mockRejectedValue(new Error("Network connection refused to API"));
    vi.spyOn(api, "fetchMarketDataInstruments").mockResolvedValue([]);

    render(<MarketDataLab />);

    await waitFor(() => {
      expect(screen.getByText(/Readiness Verification Failure:/i)).toBeInTheDocument();
      expect(screen.getByText(/Network connection refused to API/i)).toBeInTheDocument();
    });

    const btn = screen.getByRole("button", { name: /Acquire Completed Candles/i });
    expect(btn).toBeDisabled();
  });

  it("successfully fetches and displays completed candles and dataset provenance", async () => {
    vi.spyOn(api, "fetchMarketDataReadiness").mockResolvedValue(mockReadinessEnabled);
    vi.spyOn(api, "fetchMarketDataInstruments").mockResolvedValue(mockInstruments);
    vi.spyOn(api, "fetchMarketDataCandles").mockResolvedValue(mockCandlesResponse);

    render(<MarketDataLab />);

    await waitFor(() => {
      expect(screen.getByText(/Configured & Enabled/i)).toBeInTheDocument();
    });

    const fetchBtn = screen.getByRole("button", { name: /Acquire Completed Candles/i });
    expect(fetchBtn).not.toBeDisabled();
    fireEvent.click(fetchBtn);

    await waitFor(() => {
      expect(screen.getByText(/Dataset Provenance \(SHA-256 Digest\)/i)).toBeInTheDocument();
      expect(screen.getByText("PROVIDER_UPSTOX_V3")).toBeInTheDocument();
      expect(screen.getByText("Complete Series")).toBeInTheDocument();
      expect(screen.getByText(/a1b2c3d4e5f67890/)).toBeInTheDocument();

      // Check candles table
      expect(screen.getByText("25,200.00")).toBeInTheDocument();
      expect(screen.getByText("25,260.00")).toBeInTheDocument();
      expect(screen.getAllByText("Completed").length).toBe(2);
    });
  });

  it("preserves loaded metadata labeling when form dropdown changes without re-fetching", async () => {
    vi.spyOn(api, "fetchMarketDataReadiness").mockResolvedValue(mockReadinessEnabled);
    vi.spyOn(api, "fetchMarketDataInstruments").mockResolvedValue(mockInstruments);
    vi.spyOn(api, "fetchMarketDataCandles").mockResolvedValue(mockCandlesResponse);

    render(<MarketDataLab />);

    await waitFor(() => {
      expect(screen.getByText(/Configured & Enabled/i)).toBeInTheDocument();
    });

    const fetchBtn = screen.getByRole("button", { name: /Acquire Completed Candles/i });
    fireEvent.click(fetchBtn);

    await waitFor(() => {
      expect(screen.getByText(/Loaded Timeframe:/i)).toBeInTheDocument();
      expect(screen.getByText("5m")).toBeInTheDocument();
    });

    // Change timeframe dropdown from 5m to 15m
    const timeframeSelect = screen.getByLabelText(/Timeframe/i);
    fireEvent.change(timeframeSelect, { target: { value: "15m" } });

    // Table header must still display Loaded Timeframe: 5m for existing loaded candles
    expect(screen.getByText(/Loaded Timeframe:/i)).toBeInTheDocument();
    expect(screen.getByText("5m")).toBeInTheDocument();
  });

  it("switches to historical mode and renders date pickers", async () => {
    vi.spyOn(api, "fetchMarketDataReadiness").mockResolvedValue(mockReadinessEnabled);
    vi.spyOn(api, "fetchMarketDataInstruments").mockResolvedValue(mockInstruments);

    render(<MarketDataLab />);

    await waitFor(() => {
      expect(screen.getByText(/Configured & Enabled/i)).toBeInTheDocument();
    });

    const modeSelect = screen.getByLabelText(/Acquisition Mode/i);
    fireEvent.change(modeSelect, { target: { value: "historical" } });

    expect(screen.getByLabelText(/From Date/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/To Date/i)).toBeInTheDocument();
  });

  it("displays sanitized error alert when fetch fails", async () => {
    vi.spyOn(api, "fetchMarketDataReadiness").mockResolvedValue(mockReadinessEnabled);
    vi.spyOn(api, "fetchMarketDataInstruments").mockResolvedValue(mockInstruments);
    vi.spyOn(api, "fetchMarketDataCandles").mockRejectedValue(new Error("Rate limit exceeded [UDAPI100050]"));

    render(<MarketDataLab />);

    await waitFor(() => {
      expect(screen.getByText(/Configured & Enabled/i)).toBeInTheDocument();
    });

    const fetchBtn = screen.getByRole("button", { name: /Acquire Completed Candles/i });
    fireEvent.click(fetchBtn);

    await waitFor(() => {
      expect(screen.getByText(/Rate limit exceeded \[UDAPI100050\]/i)).toBeInTheDocument();
    });
  });

  it("disables acquisition button and displays invalid endpoint badge when configuration is invalid", async () => {
    const mockInvalidEndpoint: api.MarketDataReadinessResponse = {
      ...mockReadinessEnabled,
      status: "INVALID_ENDPOINT_CONFIGURATION",
    };
    vi.spyOn(api, "fetchMarketDataReadiness").mockResolvedValue(mockInvalidEndpoint);
    vi.spyOn(api, "fetchMarketDataInstruments").mockResolvedValue(mockInstruments);

    render(<MarketDataLab />);

    await waitFor(() => {
      expect(screen.getByText(/Invalid Endpoint/i)).toBeInTheDocument();
    });

    const fetchBtn = screen.getByRole("button", { name: /Acquire Completed Candles/i });
    expect(fetchBtn).toBeDisabled();
  });
});
