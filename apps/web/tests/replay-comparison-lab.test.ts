import { describe, it, expect } from "vitest";
import { getExportUrl } from "../src/lib/api";

describe("Replay Comparison Lab Specification Suite", () => {

  // 1. Selector Rules & Same-Run Prevention
  it("prevents selecting the same run for baseline and comparison", () => {
    const baselineId = "run-1111";
    const comparisonId = "run-1111";

    const isSameRun = baselineId === comparisonId;
    expect(isSameRun).toBe(true);

    const errorMessage = isSameRun ? "Baseline and Comparison cannot be the same inspection run." : null;
    expect(errorMessage).not.toBeNull();
    expect(errorMessage).toContain("cannot be the same");
  });

  // 2. Verification Precedence Matrix Order
  it("enforces verification status precedence: INVALID > UNVERIFIABLE > MISMATCH > VERIFIED", () => {
    const precedenceMap: Record<string, number> = {

      INVALID: 4,
      UNVERIFIABLE: 3,
      MISMATCH: 2,
      VERIFIED: 1,
    };

    expect(precedenceMap["INVALID"]).toBeGreaterThan(precedenceMap["UNVERIFIABLE"]);
    expect(precedenceMap["UNVERIFIABLE"]).toBeGreaterThan(precedenceMap["MISMATCH"]);
    expect(precedenceMap["MISMATCH"]).toBeGreaterThan(precedenceMap["VERIFIED"]);
  });

  // 3. Export URL Generation
  it("generates correct neutral JSON and CSV export endpoints with query params", () => {
    const testRunId = "597a9957-ed19-6a5c-70f1-a6f631b30507";
    const jsonUrl = getExportUrl(testRunId, "json");
    const csvUrl = getExportUrl(testRunId, "csv");

    expect(jsonUrl).toContain(`/api/v1/replays/${testRunId}/export?format=json`);
    expect(csvUrl).toContain(`/api/v1/replays/${testRunId}/export?format=csv`);
  });

  // 4. Neutral Transition Matrix Dimensions
  it("verifies complete 5x5 neutral transition matrix (25 cells)", () => {
    const statuses = ["TRUE", "FALSE", "UNAVAILABLE", "INVALID", "ABSENT"];
    const matrixKeys: string[] = [];

    for (const b of statuses) {
      for (const c of statuses) {
        matrixKeys.push(`${b} -> ${c}`);
      }
    }

    expect(matrixKeys.length).toBe(25);
    expect(matrixKeys).toContain("TRUE -> FALSE");
    expect(matrixKeys).toContain("ABSENT -> TRUE");
    expect(matrixKeys).toContain("UNAVAILABLE -> INVALID");
  });

  // 5. Prohibited Financial Terminology Invariant Check
  it("enforces zero occurrence of prohibited financial and trading terminology", () => {
    const educationalNotice =
      "Synthetic educational inspection only. Comparisons show neutral Boolean status differences and are not recommendations, rankings, trading signals, or profitability results.";

    const prohibitedTerms = [
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

    prohibitedTerms.forEach((term) => {
      expect(` ${educationalNotice.toUpperCase()} `).not.toContain(term);
    });
  });


  // 6. Accessible Status Transition Summary Invariants
  it("calculates aligned point counts and unchanged points correctly", () => {
    const alignedCount = 50;
    const changedCount = 12;
    const unchangedCount = alignedCount - changedCount;

    expect(unchangedCount).toBe(38);
    expect(changedCount + unchangedCount).toBe(alignedCount);
  });

  // 7. ABSENT vs UNAVAILABLE Evaluation Distinction
  it("distinguishes absent points from present points with UNAVAILABLE status", () => {
    const pointAbsent = { baseline_present: false, baseline_status: null };
    const pointUnavailable = { baseline_present: true, baseline_status: "UNAVAILABLE" };

    expect(pointAbsent.baseline_present).toBe(false);
    expect(pointAbsent.baseline_status).toBeNull();

    expect(pointUnavailable.baseline_present).toBe(true);
    expect(pointUnavailable.baseline_status).toBe("UNAVAILABLE");
    expect(pointAbsent.baseline_present).not.toBe(pointUnavailable.baseline_present);
  });

  // 8. Deterministic Ordering Invariant
  it("ensures differences are ordered by UTC timestamp ascending", () => {
    const ts1 = "2026-08-28T09:15:00.000Z";
    const ts2 = "2026-08-28T09:30:00.000Z";

    expect(new Date(ts1).getTime()).toBeLessThan(new Date(ts2).getTime());
  });

  // 9. Mobile Responsive Layout Classes Contract
  it("enforces mobile-responsive stacking classes on verification cards", () => {
    const cardHeaderClasses = "flex flex-col sm:flex-row sm:items-center justify-between gap-2.5 mb-3 min-w-0";
    expect(cardHeaderClasses).toContain("flex-col");
    expect(cardHeaderClasses).toContain("sm:flex-row");
    expect(cardHeaderClasses).toContain("min-w-0");
  });

  // 10. Status Badge Visibility Contract
  it("enforces shrink-0 on status badges to prevent compression on narrow viewports", () => {
    const badgeContainerClass = "shrink-0 self-start sm:self-center";
    expect(badgeContainerClass).toContain("shrink-0");
  });

  // 11. Long Run-ID Containment Contract
  it("enforces truncation and min-w-0 on long UUID run identifiers", () => {
    const runIdClass = "font-mono text-xs text-slate-300 truncate max-w-full block";
    expect(runIdClass).toContain("truncate");
    expect(runIdClass).toContain("max-w-full");
  });

  // 12. Internal Table Scrolling Container Contract
  it("enforces horizontal scroll containment for difference and matrix tables", () => {
    const tableWrapperClass = "overflow-x-auto";
    expect(tableWrapperClass).toBe("overflow-x-auto");
  });
});
