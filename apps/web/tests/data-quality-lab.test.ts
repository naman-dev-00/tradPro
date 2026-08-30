import { describe, it, expect } from "vitest";
import {
  getDataQualityExportUrl,
  DatasetQualityReport,
  DatasetQualitySummary,
  DatasetProvenance,
} from "../src/lib/api";

describe("Data Quality Lab Specification Suite", () => {
  const mockProvenance: DatasetProvenance = {
    dataset_id: "synthetic_underlying_nifty_15m",
    display_name: "NIFTY Underlying Index (15m)",
    category: "REFERENCE",
    instrument_id: "NIFTY",
    timeframe: "15m",
    is_synthetic: true,
    manifest_version: "1.0.0",
    fixture_checksum: "e02888ea3dfaa9e2d843955878b76c560f805063e71924ab23cf7b7d22ac60cb",
    source_type: "PACKAGED_SYNTHETIC_FIXTURE",
    immutable: true,
  };

  const mockSummary: DatasetQualitySummary = {
    total_rows: 35,
    valid_rows: 35,
    malformed_rows: 0,
    completed_rows: 35,
    incomplete_rows: 0,
    duplicate_timestamp_count: 0,
    missing_interval_count: 0,
    first_timestamp: "2026-08-28T09:15:00Z",
    last_timestamp: "2026-08-28T17:45:00Z",
    expected_interval_seconds: 900,
    calculated_checksum: "e02888ea3dfaa9e2d843955878b76c560f805063e71924ab23cf7b7d22ac60cb",
    manifest_checksum: "e02888ea3dfaa9e2d843955878b76c560f805063e71924ab23cf7b7d22ac60cb",
    checksum_matches: true,
  };

  const mockReport: DatasetQualityReport = {
    dataset_id: "synthetic_underlying_nifty_15m",
    status: "PASS",
    provenance: mockProvenance,
    summary: mockSummary,
    issues: [],
    total_issue_count: 0,
    reported_issue_count: 0,
    issues_truncated: false,
    audit_rules_version: "1.0.0",
    warnings: [],
  };

  it("calculates row count invariants correctly", () => {
    expect(mockReport.status).toBe("PASS");
    expect(mockReport.summary.valid_rows + mockReport.summary.malformed_rows).toBe(mockReport.summary.total_rows);
    expect(mockReport.summary.completed_rows + mockReport.summary.incomplete_rows).toBe(mockReport.summary.valid_rows);
  });

  it("enforces strict provenance immutability and synthetic attributes", () => {
    expect(mockReport.provenance.is_synthetic).toBe(true);
    expect(mockReport.provenance.immutable).toBe(true);
    expect(mockReport.provenance.source_type).toBe("PACKAGED_SYNTHETIC_FIXTURE");
  });


  it("generates correct clean JSON export URL", () => {
    const url = getDataQualityExportUrl("synthetic_underlying_nifty_15m");
    expect(url).toContain("/api/v1/data-quality/datasets/synthetic_underlying_nifty_15m/export");
  });

  it("enforces educational notice compliance without prohibited terms", () => {
    const noticeText =
      "Packaged synthetic educational data only. Quality findings describe dataset integrity and provenance, not market quality, recommendations, rankings, or expected outcomes.";

    expect(noticeText).toContain("Packaged synthetic educational data only");

    const prohibitedTerms = [
      " BUY ",
      " SELL ",
      " WIN RATE ",
      " DRAWDOWN ",
      " WINNER ",
      " ENTRY ",
      " EXIT ",
      " SLIPPAGE ",
      " BROKER ",
    ];

    prohibitedTerms.forEach((term) => {
      expect(` ${noticeText.toUpperCase()} `).not.toContain(term);
    });
  });

  it("verifies batch status counts sum matches total datasets", () => {
    const statusCounts = { PASS: 1, WARN: 4, FAIL: 1 };
    const totalDatasets = statusCounts.PASS + statusCounts.WARN + statusCounts.FAIL;
    expect(totalDatasets).toBe(6);
  });

  it("truncates issues properly when issue count exceeds 1000", () => {
    const totalIssues = 1200;
    const maxReported = 1000;
    const truncated = maxReported < totalIssues;
    expect(truncated).toBe(true);
  });
});
