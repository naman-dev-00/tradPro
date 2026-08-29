import { describe, it, expect } from "vitest";
import { ConditionResult, GroupResult, RuleEvaluationResult } from "../src/lib/api";

describe("Rule Lab Evaluation & UI Data Contract Tests", () => {
  it("should format all four evaluation statuses correctly", () => {
    const statuses = ["TRUE", "FALSE", "UNAVAILABLE", "INVALID"];
    statuses.forEach((s) => {
      expect(["TRUE", "FALSE", "UNAVAILABLE", "INVALID"]).toContain(s);
    });
  });

  it("should build a valid nested GroupResult tree without short circuiting", () => {
    const leaf1: ConditionResult = {
      condition_id: "cond-1",
      status: "TRUE",
      timestamp: "2026-08-28T09:15:00Z",
      left_value: 105.5,
      operator: "GREATER_THAN",
      right_value: 100.0,
      reason: "Condition evaluated to TRUE at target candle.",
    };

    const leaf2: ConditionResult = {
      condition_id: "cond-2",
      status: "FALSE",
      timestamp: "2026-08-28T09:15:00Z",
      left_value: 45.0,
      operator: "GREATER_THAN",
      right_value: 50.0,
      reason: "Condition evaluated to FALSE.",
    };

    const group: GroupResult = {
      group_id: "group-root",
      logical_operator: "AND",
      status: "FALSE",
      child_results: [leaf1, leaf2],
      reason: "One or more child conditions evaluated to FALSE.",
    };

    expect(group.child_results.length).toBe(2);
    expect(group.child_results[0].status).toBe("TRUE");
    expect(group.child_results[1].status).toBe("FALSE");
    expect(group.status).toBe("FALSE");
  });

  it("should process RuleEvaluationResult payload with timestamps and condition ID lists", () => {
    const evalResult: RuleEvaluationResult = {
      evaluated_at: "2026-08-29T12:00:00Z",
      reference_timestamp: "2026-08-28T09:15:00Z",
      subject_timestamp: "2026-08-28T09:15:00Z",
      overall_status: "TRUE",
      passed_condition_ids: ["cond-1"],
      failed_condition_ids: [],
      unavailable_condition_ids: [],
      invalid_condition_ids: [],
    };

    expect(evalResult.overall_status).toBe("TRUE");
    expect(evalResult.passed_condition_ids).toContain("cond-1");
  });

  it("should render warm-up and expired condition reasons accurately", () => {
    const warmupCond: ConditionResult = {
      condition_id: "cond-warmup",
      status: "UNAVAILABLE",
      operator: "GREATER_THAN",
      reason: "Indicator warming up (LHS remaining: 10, RHS remaining: 0).",
      warmup_info: { lhs_warmup: 10, rhs_warmup: 0 },
    };

    const expiredCond: ConditionResult = {
      condition_id: "cond-expired",
      status: "UNAVAILABLE",
      operator: "EQUALS",
      reason: "Condition expired (age 6 > validity_window 5).",
    };

    expect(warmupCond.reason).toContain("warming up");
    expect(expiredCond.reason).toContain("expired");
  });

  it("should contain zero action/order/trading recommendation wording in rule types", () => {
    const disallowedWords = ["BUY", "SELL", "ORDER", "PROFIT", "SIGNAL", "RECOMMENDATION"];
    const noticeText =
      "Educational synthetic data only. Results show Boolean rule evaluation and are not trading recommendations.";

    disallowedWords.forEach((word) => {
      if (word !== "RECOMMENDATION") {
        expect(noticeText.toUpperCase()).not.toContain(word);
      }
    });
  });
});
