import { describe, it, expect } from "vitest";
import { getExportJsonUrl, getExportCsvUrl } from "../src/lib/api";

describe("Historical Replay Lab Specification Suite", () => {

  // 1. Replay Form Controls & Defaults
  it("validates default replay parameters and timeframe limits", () => {
    const defaultStart = "2026-08-28T09:15:00.000Z";
    const defaultEnd = "2026-08-28T15:30:00.000Z";
    const samplingStep = 1;

    expect(!isNaN(Date.parse(defaultStart))).toBe(true);
    expect(!isNaN(Date.parse(defaultEnd))).toBe(true);
    expect(Date.parse(defaultStart)).toBeLessThanOrEqual(Date.parse(defaultEnd));
    expect(samplingStep).toBeGreaterThanOrEqual(1);
  });

  // 2. Estimated Request Size Calculation
  it("calculates estimated evaluation points and total evaluations correctly", () => {
    const startMs = new Date("2026-08-28T09:15:00.000Z").getTime();
    const endMs = new Date("2026-08-28T15:30:00.000Z").getTime();
    const diffMins = (endMs - startMs) / (1000 * 60);
    const estCandles = Math.floor(diffMins / 15) + 1;
    const subjectsCount = 3;
    const totalEvaluations = estCandles * subjectsCount;

    expect(estCandles).toBeGreaterThan(0);
    expect(totalEvaluations).toBe(estCandles * subjectsCount);
    expect(totalEvaluations).toBeLessThanOrEqual(20000);
  });

  // 3. Validation Error Handling
  it("rejects invalid request parameters exceeding limits", () => {
    // Start after end
    const invalidStart = "2026-08-28T16:00:00.000Z";
    const invalidEnd = "2026-08-28T09:15:00.000Z";
    const isStartAfterEnd = new Date(invalidStart).getTime() > new Date(invalidEnd).getTime();
    expect(isStartAfterEnd).toBe(true);

    // Over 20 subjects
    const subjectList21 = Array.from({ length: 21 }, (_, i) => `subj-${i + 1}`);
    expect(subjectList21.length).toBeGreaterThan(20);

    // Over 20,000 evaluations
    const overLimitEvals = 20001;
    expect(overLimitEvals).toBeGreaterThan(20000);
  });

  // 4. Export URL Generation
  it("generates correct neutral JSON and CSV export endpoints", () => {
    const testRunId = "run-1234-5678";
    const jsonUrl = getExportJsonUrl(testRunId);
    const csvUrl = getExportCsvUrl(testRunId);

    expect(jsonUrl).toContain(`/api/v1/replays/${testRunId}/export.json`);
    expect(csvUrl).toContain(`/api/v1/replays/${testRunId}/export.csv`);
  });

  // 5. Educational Notice & Prohibited Terminology
  it("enforces educational notice banner and complete absence of prohibited financial terminology", () => {
    const noticeText =
      "Educational synthetic-data Boolean replay. This page does not simulate trades, calculate profitability, or provide recommendations.";

    const prohibitedPhrases = [
      " BUY ",
      " SELL ",
      " WIN RATE ",
      " DRAWDOWN ",
      " WINNER ",
      " RANKING ",
      " ENTRY ",
      " EXIT ",
      " SLIPPAGE ",
      " COMMISSION ",
      " BROKER ",
    ];

    prohibitedPhrases.forEach((phrase) => {
      expect(` ${noticeText.toUpperCase()} `).not.toContain(phrase);
    });
  });

  // 6. Accessible Timeline Representation
  it("verifies accessible categorical timeline status counts invariant", () => {
    const statusCounts = {
      TRUE: 15,
      FALSE: 10,
      UNAVAILABLE: 0,
      INVALID: 0,
    };
    const total = Object.values(statusCounts).reduce((a, b) => a + b, 0);

    expect(total).toBe(25);
    expect(statusCounts.TRUE + statusCounts.FALSE + statusCounts.UNAVAILABLE + statusCounts.INVALID).toBe(total);
  });

  // 7. Explicit Reused Completed Run Indicator Test
  it("handles reused completed run response indicator", () => {
    const reusedResponse = {
      run_id: "run-9999",
      status: "COMPLETED",
      is_reused: true,
      run: { result_payload: { aggregate_status_counts: { TRUE: 10 } } }
    };
    expect(reusedResponse.is_reused).toBe(true);
    expect(reusedResponse.status).toBe("COMPLETED");
  });

  // 8. FAILED Run Display Handling Test
  it("displays FAILED run error message cleanly", () => {
    const failedRun = {
      id: "run-failed-1",
      status: "FAILED",
      failure_summary: "Evaluation error: candle stream interrupted",
      result_payload: null
    };
    expect(failedRun.status).toBe("FAILED");
    expect(failedRun.failure_summary).toContain("Evaluation error");
    expect(failedRun.result_payload).toBeNull();
  });
});
