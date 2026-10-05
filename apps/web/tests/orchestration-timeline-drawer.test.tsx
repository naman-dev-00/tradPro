import React from "react";
import "@testing-library/jest-dom";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { OrchestrationTimelineDrawer } from "../src/components/PaperTradingLab/OrchestrationTimelineDrawer";
import * as api from "../src/lib/api";

vi.mock("../src/lib/api", async () => {
  const actual = await vi.importActual<typeof import("../src/lib/api")>("../src/lib/api");
  return {
    ...actual,
    fetchEvaluationHistory: vi.fn(),
    fetchEvaluationDetail: vi.fn(),
  };
});

describe("OrchestrationTimelineDrawer Specification Suite", () => {
  const onClose = vi.fn();

  const mockEvaluations: api.RuntimeEvaluationSummaryResponse[] = [
    {
      id: "eval-1-true",
      runtime_id: "rt-123",
      config_id: "cfg-123",
      snapshot_fingerprint: "snap-fp-1",
      evaluation_fingerprint: "eval-fp-1",
      timeframe: "15m",
      close_at: "2026-08-28T09:30:00Z",
      reference_candle_id: "candle-1",
      subject_candle_id: null,
      evaluation_status: "TRUE",
      action_outcome: "ACCEPTED_INTERNAL",
      risk_outcome: "PASSED",
      no_order_reason: null,
      finalized_at: "2026-08-28T09:30:05Z",
    },
    {
      id: "eval-2-false",
      runtime_id: "rt-123",
      config_id: "cfg-123",
      snapshot_fingerprint: "snap-fp-2",
      evaluation_fingerprint: "eval-fp-2",
      timeframe: "15m",
      close_at: "2026-08-28T09:45:00Z",
      reference_candle_id: "candle-2",
      subject_candle_id: null,
      evaluation_status: "FALSE",
      action_outcome: "NO_ACTION",
      risk_outcome: "SKIPPED",
      no_order_reason: "RULE_FALSE",
      finalized_at: "2026-08-28T09:45:05Z",
    },
    {
      id: "eval-3-unavail",
      runtime_id: "rt-123",
      config_id: "cfg-123",
      snapshot_fingerprint: "snap-fp-3",
      evaluation_fingerprint: "eval-fp-3",
      timeframe: "15m",
      close_at: "2026-08-28T10:00:00Z",
      reference_candle_id: "candle-3",
      subject_candle_id: null,
      evaluation_status: "UNAVAILABLE",
      action_outcome: "NO_ACTION",
      risk_outcome: "SKIPPED",
      no_order_reason: "WARMUP_INCOMPLETE",
      finalized_at: "2026-08-28T10:00:05Z",
    },
    {
      id: "eval-4-invalid",
      runtime_id: "rt-123",
      config_id: "cfg-123",
      snapshot_fingerprint: "snap-fp-4",
      evaluation_fingerprint: "eval-fp-4",
      timeframe: "15m",
      close_at: "2026-08-28T10:15:00Z",
      reference_candle_id: "candle-4",
      subject_candle_id: null,
      evaluation_status: "INVALID",
      action_outcome: "REJECTED",
      risk_outcome: "FAILED",
      no_order_reason: "GAP_EXCEEDED",
      finalized_at: "2026-08-28T10:15:05Z",
    },
  ];

  const mockDetail: api.RuntimeEvaluationDetailResponse = {
    ...mockEvaluations[0],
    required_candles_json: JSON.stringify([{ candle_id: "c-1", boundary: "2026-08-28T09:30:00Z" }]),
    audit_json: JSON.stringify({
      result: "TRUE",
      condition_ids: ["COND_RSI_OVERSOLD", "COND_VOL_SPIKE"],
    }),
    risk_summary_json: JSON.stringify({
      outcome: "PASSED",
      reason_codes: ["PASSED"],
      reserved_amount: "50000.00",
    }),
  };

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("preserves TRUE, FALSE, UNAVAILABLE, and INVALID distinctions with dedicated badges", async () => {
    const mockHistory = vi.mocked(api.fetchEvaluationHistory);
    const mockFetchDetail = vi.mocked(api.fetchEvaluationDetail);

    mockHistory.mockResolvedValueOnce({
      runtime_id: "rt-123",
      total: 4,
      limit: 50,
      offset: 0,
      evaluations: mockEvaluations,
    });
    mockFetchDetail.mockResolvedValueOnce(mockDetail);

    render(
      <OrchestrationTimelineDrawer
        runtimeId="rt-123"
        isOpen={true}
        onClose={onClose}
      />
    );

    await waitFor(() => {
      expect(screen.getAllByTestId("status-badge-true").length).toBeGreaterThanOrEqual(1);
      expect(screen.getByTestId("status-badge-false")).toBeInTheDocument();
      expect(screen.getByTestId("status-badge-unavailable")).toBeInTheDocument();
      expect(screen.getByTestId("status-badge-invalid")).toBeInTheDocument();
    });

    // Check specific class signatures
    expect(screen.getAllByTestId("status-badge-true")[0]).toHaveClass("text-emerald-300");
    expect(screen.getByTestId("status-badge-false")).toHaveClass("text-slate-300");
    expect(screen.getByTestId("status-badge-unavailable")).toHaveClass("text-amber-300");
    expect(screen.getByTestId("status-badge-invalid")).toHaveClass("text-rose-300");
  });

  it("displays actual linked execution evidence including rule audit and risk summary JSON", async () => {
    const mockHistory = vi.mocked(api.fetchEvaluationHistory);
    const mockFetchDetail = vi.mocked(api.fetchEvaluationDetail);

    mockHistory.mockResolvedValueOnce({
      runtime_id: "rt-123",
      total: 4,
      limit: 50,
      offset: 0,
      evaluations: mockEvaluations,
    });
    mockFetchDetail.mockResolvedValueOnce(mockDetail);

    render(
      <OrchestrationTimelineDrawer
        runtimeId="rt-123"
        isOpen={true}
        onClose={onClose}
      />
    );

    await waitFor(() => {
      expect(screen.getByTestId("audit-json-evidence")).toBeInTheDocument();
      expect(screen.getByTestId("risk-summary-evidence")).toBeInTheDocument();
    });

    expect(screen.getByTestId("audit-json-evidence")).toHaveTextContent("COND_RSI_OVERSOLD");
    expect(screen.getByTestId("risk-summary-evidence")).toHaveTextContent("50000.00");
  });

  it("closes on Escape key press and supports accessible drawer structure", async () => {
    const mockHistory = vi.mocked(api.fetchEvaluationHistory);
    mockHistory.mockResolvedValueOnce({
      runtime_id: "rt-123",
      total: 0,
      limit: 50,
      offset: 0,
      evaluations: [],
    });

    render(
      <OrchestrationTimelineDrawer
        runtimeId="rt-123"
        isOpen={true}
        onClose={onClose}
      />
    );

    const dialog = screen.getByRole("dialog", { name: "Orchestration Timeline Drawer" });
    expect(dialog).toBeInTheDocument();
    expect(dialog).toHaveAttribute("aria-modal", "true");

    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);

    await waitFor(() => {
      expect(mockHistory).toHaveBeenCalled();
    });
  });
});
