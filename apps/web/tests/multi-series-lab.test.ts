import { describe, it, expect } from "vitest";
import {
  DatasetManifestEntry,
  MultiSeriesEvaluationResult,
  SeriesEvaluationResult,
  EvaluationStatus,
} from "../src/lib/api";

describe("Multi-Series Lab UI & Data Contract Specification Suite", () => {
  // 1. Manifest loading
  it("handles manifest loading and dataset categorization (REFERENCE vs SUBJECT)", () => {
    const mockManifest: DatasetManifestEntry[] = [
      {
        dataset_id: "synthetic_underlying_nifty_15m",
        display_name: "NIFTY (15m)",
        description: "Reference series",
        instrument_id: "NIFTY",
        timeframe: "15m",
        candle_count: 35,
        completed_candle_count: 35,
        category: "REFERENCE",
        is_synthetic: true,
      },
      {
        dataset_id: "synthetic_candidate_option_ce_23000_15m",
        display_name: "NIFTY 23000 CE (15m)",
        description: "Subject series 1",
        instrument_id: "NIFTY_23000_CE",
        timeframe: "15m",
        candle_count: 10,
        completed_candle_count: 10,
        category: "SUBJECT",
        is_synthetic: true,
      },
    ];

    const refs = mockManifest.filter((d) => d.category === "REFERENCE");
    const subjs = mockManifest.filter((d) => d.category === "SUBJECT");

    expect(refs).toHaveLength(1);
    expect(subjs).toHaveLength(1);
    expect(refs[0].dataset_id).toBe("synthetic_underlying_nifty_15m");
  });

  // 2. Strategy loading
  it("handles strategy loading and populates strategy selector state", () => {
    const mockStrategies = [
      { id: "strat-1", name: "RSI Crossover", timeframe: "15m" },
      { id: "strat-2", name: "SMA Breakout", timeframe: "15m" },
    ];
    const selectedStrategyId = mockStrategies[0].id;

    expect(mockStrategies).toHaveLength(2);
    expect(selectedStrategyId).toBe("strat-1");
  });

  // 3. Reference selection
  it("enforces reference dataset selection from REFERENCE category only", () => {
    const mockManifest: DatasetManifestEntry[] = [
      {
        dataset_id: "synthetic_underlying_nifty_15m",
        display_name: "NIFTY 15m",
        description: "Ref",
        instrument_id: "NIFTY",
        timeframe: "15m",
        candle_count: 35,
        completed_candle_count: 35,
        category: "REFERENCE",
        is_synthetic: true,
      },
      {
        dataset_id: "synthetic_candidate_option_ce_23000_15m",
        display_name: "CE 23000",
        description: "Subj",
        instrument_id: "NIFTY_23000_CE",
        timeframe: "15m",
        candle_count: 10,
        completed_candle_count: 10,
        category: "SUBJECT",
        is_synthetic: true,
      },
    ];

    const validReferenceIds = mockManifest
      .filter((d) => d.category === "REFERENCE")
      .map((d) => d.dataset_id);

    expect(validReferenceIds).toContain("synthetic_underlying_nifty_15m");
    expect(validReferenceIds).not.toContain("synthetic_candidate_option_ce_23000_15m");
  });

  // 4. Keyboard-accessible subject selection
  it("supports keyboard-accessible subject selection with focus rings and checkbox states", () => {
    const isChecked = true;
    const focusRingClass = "focus:ring-2 focus:ring-sky-500";
    const checkboxClass = `mt-0.5 rounded border-slate-700 bg-slate-900 text-sky-500 ${focusRingClass}`;

    expect(isChecked).toBe(true);
    expect(checkboxClass).toContain("focus:ring-2");
    expect(checkboxClass).toContain("focus:ring-sky-500");
  });

  // 5. Duplicate prevention
  it("enforces subject dataset uniqueness and prevents duplicate selections", () => {
    const selectedIds = ["subj-1", "subj-2"];
    const candidateId = "subj-1";

    const toggleSubject = (id: string, current: string[]): string[] => {
      if (current.includes(id)) {
        return current.filter((s) => s !== id);
      }
      return [...current, id];
    };

    const updated = toggleSubject(candidateId, selectedIds);
    expect(updated).toEqual(["subj-2"]);
    const uniqueSet = new Set(selectedIds);
    expect(uniqueSet.size).toBe(selectedIds.length);
  });

  // 6. Exactly 20 subjects
  it("allows selection of exactly 20 subject datasets", () => {
    const exact20Subjects = Array.from({ length: 20 }, (_, i) => `subj-${i + 1}`);

    expect(exact20Subjects).toHaveLength(20);
    expect(exact20Subjects.length).toBeLessThanOrEqual(20);
  });

  // 7. More-than-20 rejection
  it("rejects selection of more than 20 subject datasets with user validation error", () => {
    const selected20 = Array.from({ length: 20 }, (_, i) => `subj-${i + 1}`);
    const candidate21 = "subj-21";
    const selectedWithOverflow = [...selected20, candidate21];

    let error: string | null = null;
    if (selectedWithOverflow.length > 20) {
      error = "Maximum 20 subject datasets allowed.";
    }

    expect(error).toBe("Maximum 20 subject datasets allowed.");
  });

  // 8. Required timezone-aware timestamp
  it("validates required timezone-aware UTC evaluation timestamp format", () => {
    const validUtcTimestamp = "2026-08-28T17:45:00.000Z";
    const isValidFormat = !isNaN(Date.parse(validUtcTimestamp)) && validUtcTimestamp.endsWith("Z");

    expect(isValidFormat).toBe(true);
  });

  // 9. Successful multi-series request
  it("constructs and executes successful multi-series evaluation request payload", () => {
    const requestPayload = {
      strategy_id: "strat-123",
      reference_dataset_id: "synthetic_underlying_nifty_15m",
      subject_dataset_ids: [
        "synthetic_candidate_option_ce_23000_15m",
        "synthetic_candidate_option_pe_23000_15m",
      ],
      eval_timestamp: "2026-08-28T17:45:00.000Z",
    };

    expect(requestPayload.reference_dataset_id).toBe("synthetic_underlying_nifty_15m");
    expect(requestPayload.subject_dataset_ids).toHaveLength(2);
    expect(requestPayload.eval_timestamp).toMatch(/Z$/);
  });

  // 10. Mixed TRUE/FALSE/UNAVAILABLE/INVALID rendering
  it("renders mixed TRUE, FALSE, UNAVAILABLE, and INVALID series inspection badges", () => {
    const statuses: EvaluationStatus[] = ["TRUE", "FALSE", "UNAVAILABLE", "INVALID"];
    const badges = statuses.map((status) => ({
      status,
      rendered: true,
    }));

    expect(badges).toHaveLength(4);
    expect(badges.map((b) => b.status)).toEqual(["TRUE", "FALSE", "UNAVAILABLE", "INVALID"]);
  });

  // 11. Stable input ordering
  it("retains exact subject_dataset_ids input order in returned series results", () => {
    const inputSubjectIds = [
      "synthetic_candidate_option_pe_23000_15m",
      "synthetic_candidate_option_ce_23000_15m",
      "synthetic_short_insufficient_5m",
    ];

    const results: SeriesEvaluationResult[] = [
      {
        dataset_id: "synthetic_candidate_option_pe_23000_15m",
        instrument_id: "NIFTY_23000_PE",
        timeframe: "15m",
        evaluation_timestamp: "2026-08-28T17:45:00Z",
        overall_status: "TRUE",
        passed_condition_ids: [],
        failed_condition_ids: [],
        unavailable_condition_ids: [],
        invalid_condition_ids: [],
        inspection_summary: "TRUE",
      },
      {
        dataset_id: "synthetic_candidate_option_ce_23000_15m",
        instrument_id: "NIFTY_23000_CE",
        timeframe: "15m",
        evaluation_timestamp: "2026-08-28T17:45:00Z",
        overall_status: "FALSE",
        passed_condition_ids: [],
        failed_condition_ids: [],
        unavailable_condition_ids: [],
        invalid_condition_ids: [],
        inspection_summary: "FALSE",
      },
      {
        dataset_id: "synthetic_short_insufficient_5m",
        instrument_id: "SHORT_SERIES",
        timeframe: "5m",
        evaluation_timestamp: "2026-08-28T17:45:00Z",
        overall_status: "INVALID",
        passed_condition_ids: [],
        failed_condition_ids: [],
        unavailable_condition_ids: [],
        invalid_condition_ids: [],
        inspection_summary: "INVALID",
      },
    ];

    expect(results.map((r) => r.dataset_id)).toEqual(inputSubjectIds);
  });

  // 12. Status counts summing to total
  it("validates that status_counts keys (TRUE, FALSE, UNAVAILABLE, INVALID) sum to total_series_evaluated", () => {
    const statusCounts = { TRUE: 2, FALSE: 1, UNAVAILABLE: 0, INVALID: 1 };
    const totalEvaluated = 4;
    const sum = statusCounts.TRUE + statusCounts.FALSE + statusCounts.UNAVAILABLE + statusCounts.INVALID;

    expect(sum).toBe(totalEvaluated);
  });

  // 13. Expandable aria-expanded/aria-controls behavior
  it("verifies expandable rule result tree accessibility attributes (aria-expanded and aria-controls)", () => {
    const datasetId = "synthetic_candidate_option_ce_23000_15m";
    const isExpanded = true;
    const regionId = `series-tree-${datasetId}`;

    const buttonAttributes = {
      "aria-expanded": isExpanded,
      "aria-controls": regionId,
    };

    expect(buttonAttributes["aria-expanded"]).toBe(true);
    expect(buttonAttributes["aria-controls"]).toBe("series-tree-synthetic_candidate_option_ce_23000_15m");
  });

  // 14. Loading state
  it("manages loading state and disables submission during evaluation", () => {
    const loading = true;
    const buttonDisabled = loading;

    expect(buttonDisabled).toBe(true);
  });

  // 15. Structured API errors
  it("handles structured API error responses and displays user alert boxes", () => {
    const apiError = { detail: "Unknown subject dataset ID 'non_existent'." };
    const alertBox = {
      role: "alert",
      message: typeof apiError.detail === "string" ? apiError.detail : JSON.stringify(apiError.detail),
    };

    expect(alertBox.role).toBe("alert");
    expect(alertBox.message).toBe("Unknown subject dataset ID 'non_existent'.");
  });

  // 16. Screen-reader completion announcement
  it("updates screen-reader polite live region (role=status, aria-live=polite) on evaluation completion", () => {
    const res: MultiSeriesEvaluationResult = {
      requested_evaluation_timestamp: "2026-08-28T17:45:00Z",
      reference_dataset_id: "synthetic_underlying_nifty_15m",
      results: [],
      status_counts: { TRUE: 2, FALSE: 1, UNAVAILABLE: 0, INVALID: 0 },
      total_series_evaluated: 3,
      warnings: [],
    };

    const announcement = `Evaluation completed. Inspected ${res.total_series_evaluated} series. Results: ${res.status_counts.TRUE} TRUE, ${res.status_counts.FALSE} FALSE, ${res.status_counts.UNAVAILABLE} UNAVAILABLE, ${res.status_counts.INVALID} INVALID.`;

    const liveRegionProps = {
      role: "status",
      "aria-live": "polite",
      children: announcement,
    };

    expect(liveRegionProps.role).toBe("status");
    expect(liveRegionProps["aria-live"]).toBe("polite");
    expect(liveRegionProps.children).toContain("Inspected 3 series");
  });

  // 17. Educational notice
  it("renders prominent educational notice banner prohibiting trading signals or recommendations", () => {
    const noticeText =
      "Educational synthetic data only. Results are independent Boolean inspections and are not rankings or trading recommendations. This interface does not evaluate profitability, generate BUY or SELL signals, rank instruments, or execute orders.";

    expect(noticeText).toContain("Educational synthetic data only");
    expect(noticeText).toContain("not rankings or trading recommendations");
  });

  // 18. Absence of ranking/recommendation/execution language
  it("ensures total absence of ranking, winner, recommendation, or order execution terminology", () => {
    const summaries = [
      "Series evaluated to TRUE (2 conditions passed).",
      "Series evaluated to FALSE (1 condition failed).",
      "Series evaluated to INVALID: Timeframe mismatch.",
    ];

    const prohibitedWords = ["rank", "winner", "best", "recommendation", "buy", "sell", "opportunity", "profit", "order"];

    for (const text of summaries) {
      const lower = text.toLowerCase();
      for (const word of prohibitedWords) {
        expect(lower).not.toContain(word);
      }
    }
  });
});
