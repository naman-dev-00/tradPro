import React from "react";
import "@testing-library/jest-dom";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { OrchestrationActivationModal } from "../src/components/PaperTradingLab/OrchestrationActivationModal";
import * as api from "../src/lib/api";

const mockRuntime: api.StrategyRuntime = {
  id: "rt-test-123",
  owner_id: "test-user",
  strategy_id: "strat-1",
  action_policy_id: "act-1",
  risk_policy_id: "risk-1",
  account_id: "acct-1",
  dataset_id: "synthetic_candidate_option_pe_23000_15m",
  instrument_id: "synthetic_candidate_option_pe_23000_15m",
  timeframe: "15m",
  status: "READY",
  trading_mode: "PAPER",
  consecutive_errors: 0,
  version: 1,
  created_at: "2026-08-28T09:00:00Z",
  updated_at: "2026-08-28T09:00:00Z",
};

const mockMappings: api.ProviderInstrumentMappingResponse[] = [
  {
    id: "map-123",
    owner_id: "test-user",
    tradepro_instrument_id: "synthetic_candidate_option_pe_23000_15m",
    provider_instrument_token: "tok-1",
    exchange: "NSE",
    segment: "OPTION",
    symbol: "TEST_OPT",
    expiry_date: null,
    strike_price: null,
    option_type: null,
    lot_size: 50,
    tick_size: "0.05",
    freeze_quantity: 1800,
    verification_status: "VERIFIED",
    verified_by: "system",
    verified_at: "2026-08-28T09:00:00Z",
    verification_audit_json: null,
    mapping_version: 1,
    created_at: "2026-08-28T09:00:00Z",
    updated_at: "2026-08-28T09:00:00Z",
  },
];

const mockDatasets: api.DatasetManifestEntry[] = [
  {
    dataset_id: "synthetic_underlying_nifty_15m",
    display_name: "NIFTY 15m",
    description: "NIFTY reference dataset",
    timeframe: "15m",
    category: "REFERENCE",
    instrument_id: "NIFTY",
    candle_count: 100,
    completed_candle_count: 100,
    is_synthetic: true,
  },
  {
    dataset_id: "synthetic_candidate_option_pe_23000_15m",
    display_name: "PE 23000 15m",
    description: "Option PE candidate dataset",
    timeframe: "15m",
    category: "SUBJECT",
    instrument_id: "PE23000",
    candle_count: 100,
    completed_candle_count: 100,
    is_synthetic: true,
  },
];

const mockConfig: api.OrchestrationConfigResponse = {
  id: "cfg-123",
  runtime_id: "rt-test-123",
  owner_id: "test-user",
  source_type: "FIXTURE_REPLAY",
  source_namespace: "NSE",
  source_policy_version: "v1",
  timeframe: "15m",
  alignment_offset_seconds: 0,
  snapshot_fingerprint: "snap-123",
  consent_fingerprint: "consent-123",
  consent_policy_version: "fixture_paper_consent_v1",
  execution_policy: "INTERNAL_PAPER",
  replay_open_at: "2026-08-28T09:15:00.000Z",
  replay_close_at: "2026-08-28T10:00:00.000Z",
  checkpoint_close_at: null,
  fencing_generation: 1,
  retry_count: 0,
  next_attempt_at: null,
  lease_owner: null,
  lease_expires_at: null,
  last_reason_code: null,
  created_at: "2026-08-28T09:15:00.000Z",
  updated_at: "2026-08-28T09:15:00.000Z",
};

vi.mock("../src/lib/api", async () => {
  const actual = await vi.importActual<typeof import("../src/lib/api")>("../src/lib/api");
  return {
    ...actual,
    activateOrchestration: vi.fn(),
    createOrchestrationConfig: vi.fn(),
    fetchOrchestrationConfig: vi.fn(),
    fetchOrchestrationReadiness: vi.fn(),
    fetchPaperRuntime: vi.fn(),
    fetchSandboxMappings: vi.fn(),
    getDatasetManifest: vi.fn(),
    validatePaperRuntime: vi.fn(),
  };
});

describe("OrchestrationActivationModal Specification Suite", () => {
  const onClose = vi.fn();
  const onActivated = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.fetchOrchestrationConfig).mockResolvedValue(null);
    vi.mocked(api.fetchPaperRuntime).mockResolvedValue(mockRuntime);
    vi.mocked(api.fetchSandboxMappings).mockResolvedValue(mockMappings);
    vi.mocked(api.getDatasetManifest).mockResolvedValue(mockDatasets);
    vi.mocked(api.fetchOrchestrationReadiness).mockResolvedValue({ ready: true, reasons: [], gates: {} });
    vi.mocked(api.createOrchestrationConfig).mockResolvedValue(mockConfig);
    vi.mocked(api.activateOrchestration).mockResolvedValue({
      runtime_id: "rt-test-123",
      status: "RUNNING",
      previous_status: "READY",
      action: "ACTIVATE",
      message: "Orchestration activated.",
      timestamp: "2026-09-22T12:00:00Z",
    });
  });

  it("offers configuration for a runtime without an orchestration configuration", async () => {
    render(<OrchestrationActivationModal runtimeId="new-runtime" isOpen={true} onClose={onClose} onActivated={onActivated} />);
    expect(await screen.findByLabelText(/Replay start.*UTC/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Verified instrument mapping/i)).toBeInTheDocument();
    expect(api.activateOrchestration).not.toHaveBeenCalled();
  });

  it("renders dialog with proper accessibility attributes and default INTERNAL_MOCK_ONLY policy", () => {
    render(
      <OrchestrationActivationModal
        runtimeId="rt-test-123"
        isOpen={true}
        onClose={onClose}
        onActivated={onActivated}
      />
    );

    const dialog = screen.getByRole("dialog");
    expect(dialog).toBeInTheDocument();
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(screen.getByText("Activate Orchestration Runtime")).toBeInTheDocument();
    expect(screen.getByText("Runtime ID: rt-test-123")).toBeInTheDocument();

    const mockRadio = screen.getByDisplayValue("INTERNAL_MOCK_ONLY") as HTMLInputElement;
    const paperRadio = screen.getByDisplayValue("INTERNAL_PAPER") as HTMLInputElement;
    expect(mockRadio.checked).toBe(true);
    expect(paperRadio.checked).toBe(false);

    // Submit button should be disabled initially
    const submitBtn = screen.getByRole("button", { name: /Activate \(INTERNAL_MOCK_ONLY\)/i });
    expect(submitBtn).toBeDisabled();
  });

  it("does not silently opt into paper execution and completes supported setup, readiness, consent, and activation flow", async () => {
    const mockCreateConfig = vi.mocked(api.createOrchestrationConfig);
    const mockActivate = vi.mocked(api.activateOrchestration);

    render(
      <OrchestrationActivationModal
        runtimeId="rt-test-123"
        isOpen={true}
        onClose={onClose}
        onActivated={onActivated}
      />
    );

    // Wait for setup inputs to render
    expect(await screen.findByLabelText(/Reference dataset/i)).toBeInTheDocument();

    // Fill in required setup fields for unconfigured runtime
    fireEvent.change(screen.getByLabelText(/Reference dataset/i), { target: { value: "synthetic_underlying_nifty_15m" } });
    fireEvent.change(screen.getByLabelText(/Verified instrument mapping/i), { target: { value: "map-123" } });
    fireEvent.change(screen.getByLabelText(/Replay start.*UTC/i), { target: { value: "2026-08-28T09:15" } });
    fireEvent.change(screen.getByLabelText(/Replay end.*UTC/i), { target: { value: "2026-08-28T10:00" } });

    // Switch to INTERNAL_PAPER explicitly
    const paperRadio = screen.getByDisplayValue("INTERNAL_PAPER");
    fireEvent.click(paperRadio);

    // Submit button reflects INTERNAL_PAPER but remains disabled without consents
    const submitBtn = screen.getByRole("button", { name: /Activate \(INTERNAL_PAPER\)/i });
    expect(submitBtn).toBeDisabled();

    // Check only 3 of 4 boxes
    const cb1 = screen.getByLabelText(/zero network calls/i);
    const cb2 = screen.getByLabelText(/deterministic fixture candle boundaries/i);
    const cb3 = screen.getByLabelText(/operator authorization/i);
    const cb4 = screen.getByLabelText(/explicitly consent to internal paper order generation/i);

    fireEvent.click(cb1);
    fireEvent.click(cb2);
    fireEvent.click(cb3);
    expect(submitBtn).toBeDisabled();

    // Check 4th box - now enabled
    fireEvent.click(cb4);
    expect(submitBtn).not.toBeDisabled();

    // Submit
    fireEvent.click(submitBtn);

    await waitFor(() => {
      expect(mockCreateConfig).toHaveBeenCalledWith(expect.objectContaining({
        runtime_id: "rt-test-123",
        execution_policy: "INTERNAL_PAPER",
        provider_mapping_id: "map-123",
      }));
      expect(api.fetchOrchestrationReadiness).toHaveBeenCalledWith("rt-test-123");
      expect(mockActivate).toHaveBeenCalledWith("rt-test-123", {
        consent_version: "fixture_paper_consent_v1",
        acknowledged_execution_policy: "INTERNAL_PAPER",
        confirm_internal_mock_only: false,
        confirm_internal_paper_execution: true,
      });
      expect(onActivated).toHaveBeenCalled();
      expect(onClose).toHaveBeenCalled();
    });
  });

  it("activates an existing orchestration configuration requiring only operator consent", async () => {
    vi.mocked(api.fetchOrchestrationConfig).mockResolvedValueOnce(mockConfig);

    render(
      <OrchestrationActivationModal
        runtimeId="rt-test-123"
        isOpen={true}
        onClose={onClose}
        onActivated={onActivated}
      />
    );

    // Existing immutable configuration notice is displayed
    expect(await screen.findByText(/Existing configuration is immutable: INTERNAL_PAPER/i)).toBeInTheDocument();

    const submitBtn = screen.getByRole("button", { name: /Activate \(INTERNAL_PAPER\)/i });
    expect(submitBtn).toBeDisabled();

    fireEvent.click(screen.getByLabelText(/zero network calls/i));
    fireEvent.click(screen.getByLabelText(/deterministic fixture candle boundaries/i));
    fireEvent.click(screen.getByLabelText(/operator authorization/i));
    fireEvent.click(screen.getByLabelText(/explicitly consent to internal paper order generation/i));

    expect(submitBtn).not.toBeDisabled();
    fireEvent.click(submitBtn);

    await waitFor(() => {
      expect(api.createOrchestrationConfig).not.toHaveBeenCalled();
      expect(api.activateOrchestration).toHaveBeenCalledWith("rt-test-123", {
        consent_version: "fixture_paper_consent_v1",
        acknowledged_execution_policy: "INTERNAL_PAPER",
        confirm_internal_mock_only: false,
        confirm_internal_paper_execution: true,
      });
      expect(onActivated).toHaveBeenCalled();
      expect(onClose).toHaveBeenCalled();
    });
  });

  it("handles Escape key to close dialog", () => {
    render(
      <OrchestrationActivationModal
        runtimeId="rt-test-123"
        isOpen={true}
        onClose={onClose}
        onActivated={onActivated}
      />
    );

    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("handles activation API failure and displays error message", async () => {
    vi.mocked(api.fetchOrchestrationConfig).mockResolvedValueOnce(mockConfig);
    const mockActivate = vi.mocked(api.activateOrchestration);
    mockActivate.mockRejectedValueOnce(new Error("Missing active paper account"));

    render(
      <OrchestrationActivationModal
        runtimeId="rt-test-123"
        isOpen={true}
        onClose={onClose}
        onActivated={onActivated}
      />
    );

    expect(await screen.findByText(/Existing configuration is immutable/i)).toBeInTheDocument();

    // Check all boxes for paper mode
    fireEvent.click(screen.getByLabelText(/zero network calls/i));
    fireEvent.click(screen.getByLabelText(/deterministic fixture candle boundaries/i));
    fireEvent.click(screen.getByLabelText(/operator authorization/i));
    fireEvent.click(screen.getByLabelText(/explicitly consent to internal paper order generation/i));

    const submitBtn = screen.getByRole("button", { name: /Activate \(INTERNAL_PAPER\)/i });
    expect(submitBtn).not.toBeDisabled();
    fireEvent.click(submitBtn);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent("Missing active paper account");
      expect(onActivated).not.toHaveBeenCalled();
      expect(onClose).not.toHaveBeenCalled();
    });
  });

  it("shows configuration error and prevents mapping selection and activation when runtime instrument_id is absent", async () => {
    vi.mocked(api.fetchPaperRuntime).mockResolvedValueOnce({
      ...mockRuntime,
      instrument_id: null,
    });

    render(
      <OrchestrationActivationModal
        runtimeId="rt-test-123"
        isOpen={true}
        onClose={onClose}
        onActivated={onActivated}
      />
    );

    // Error alert is displayed
    expect(await screen.findByRole("alert")).toHaveTextContent("Runtime is missing an authoritative orderable instrument identity.");

    // Readiness blocker is listed
    expect(screen.getByRole("list", { name: "Readiness blockers" })).toHaveTextContent("Runtime is missing an authoritative orderable instrument identity.");

    // Mapping dropdown has no verified mappings available
    expect(screen.getByText("Select verified mapping")).toBeInTheDocument();
    expect(screen.queryByText(/TEST_OPT/i)).not.toBeInTheDocument();

    // Fill other fields and check consent checkboxes
    fireEvent.change(screen.getByLabelText(/Reference dataset/i), { target: { value: "synthetic_underlying_nifty_15m" } });
    fireEvent.change(screen.getByLabelText(/Replay start.*UTC/i), { target: { value: "2026-08-28T09:15" } });
    fireEvent.change(screen.getByLabelText(/Replay end.*UTC/i), { target: { value: "2026-08-28T10:00" } });
    fireEvent.click(screen.getByLabelText(/zero network calls/i));
    fireEvent.click(screen.getByLabelText(/deterministic fixture candle boundaries/i));
    fireEvent.click(screen.getByLabelText(/operator authorization/i));
    fireEvent.click(screen.getByLabelText(/confirm evaluation-only mock policy/i));

    // Submit button remains strictly disabled
    const submitBtn = screen.getByRole("button", { name: /Activate/i });
    expect(submitBtn).toBeDisabled();
    expect(api.createOrchestrationConfig).not.toHaveBeenCalled();
    expect(api.activateOrchestration).not.toHaveBeenCalled();
  });
});
