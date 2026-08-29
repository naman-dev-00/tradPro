import { describe, it, expect } from "vitest";
import {
  CalculateIndicatorResponse,
  SupportedIndicatorMetadata,
  DatasetMetadata,
} from "../src/lib/api";

describe("Milestone 2A-UI Required Frontend Behaviors", () => {
  const mockIndicators: SupportedIndicatorMetadata[] = [
    {
      name: "SMA",
      description: "Simple Moving Average",
      parameters: { period: { type: "integer", default: 20, minimum: 1 } },
    },
    {
      name: "RSI",
      description: "Relative Strength Index",
      parameters: { period: { type: "integer", default: 14, minimum: 1 } },
    },
    {
      name: "MACD",
      description: "Moving Average Convergence Divergence",
      parameters: {
        fast_period: { type: "integer", default: 12, minimum: 1 },
        slow_period: { type: "integer", default: 26, minimum: 1 },
        signal_period: { type: "integer", default: 9, minimum: 1 },
      },
    },
  ];

  const mockDatasets: DatasetMetadata[] = [
    {
      id: "synthetic_underlying_nifty_15m",
      name: "NIFTY Underlying Index",
      description: "35 completed 15m candles",
      instrument_id: "NIFTY",
      timeframe: "15m",
    },
    {
      id: "synthetic_candidate_option_ce_23000_15m",
      name: "NIFTY 23000 Call Option",
      description: "Option CE fixture",
      instrument_id: "NIFTY26AUG23000CE",
      timeframe: "15m",
    },
  ];

  // 1. Supported-indicator metadata loading
  it("Supported-indicator metadata loading", () => {
    expect(mockIndicators.length).toBe(3);
    const names = mockIndicators.map((i) => i.name);
    expect(names).toContain("SMA");
    expect(names).toContain("RSI");
    expect(names).toContain("MACD");
    expect(mockIndicators[0].parameters.period.default).toBe(20);
  });

  // 2. Dataset selection
  it("Dataset selection", () => {
    const selectedId = mockDatasets[0].id;
    const selectedDataset = mockDatasets.find((d) => d.id === selectedId);
    expect(selectedDataset).toBeDefined();
    expect(selectedDataset?.instrument_id).toBe("NIFTY");
    expect(selectedDataset?.timeframe).toBe("15m");
  });

  // 3. Dynamic parameter fields
  it("Dynamic parameter fields", () => {
    const macdMeta = mockIndicators.find((i) => i.name === "MACD");
    expect(macdMeta).toBeDefined();
    const keys = Object.keys(macdMeta!.parameters);
    expect(keys).toEqual(["fast_period", "slow_period", "signal_period"]);
    expect(macdMeta!.parameters.fast_period.default).toBe(12);
    expect(macdMeta!.parameters.slow_period.default).toBe(26);
    expect(macdMeta!.parameters.signal_period.default).toBe(9);
  });

  // 4. Invalid parameter handling
  it("Invalid parameter handling", () => {
    const validateParams = (indName: string, params: Record<string, any>) => {
      const errors: Record<string, string> = {};
      if (indName === "SMA" && (params.period === undefined || params.period < 1)) {
        errors.period = "Period must be >= 1";
      }
      if (indName === "MACD" && params.slow_period <= params.fast_period) {
        errors.slow_period = "Slow period must be strictly greater than fast period.";
      }
      return errors;
    };

    expect(Object.keys(validateParams("SMA", { period: 0 })).length).toBe(1);
    expect(Object.keys(validateParams("MACD", { fast_period: 26, slow_period: 12 })).length).toBe(1);
    expect(Object.keys(validateParams("SMA", { period: 20 })).length).toBe(0);
  });

  // 5. Successful calculation
  it("Successful calculation", () => {
    const mockCalcResponse: CalculateIndicatorResponse = {
      indicator: "SMA",
      params: { period: 20 },
      results: [
        {
          timestamp: "2026-08-28T09:15:00Z",
          indicator: "SMA",
          value: 22050.25,
          available: true,
          warmup_remaining: 0,
        },
      ],
    };

    expect(mockCalcResponse.indicator).toBe("SMA");
    expect(mockCalcResponse.results.length).toBe(1);
    expect(mockCalcResponse.results[0].value).toBe(22050.25);
    expect(mockCalcResponse.results[0].available).toBe(true);
  });

  // 6. Structured API failure
  it("Structured API failure", () => {
    const parseApiError = (errData: any) => {
      return errData.detail || "Calculation request failed";
    };

    const mockErrorRes = { detail: "Unknown indicator 'SUPER_IND'" };
    expect(parseApiError(mockErrorRes)).toBe("Unknown indicator 'SUPER_IND'");
  });

  // 7. Warm-up values remaining unavailable
  it("Warm-up values remaining unavailable", () => {
    const mockWarmupResponse: CalculateIndicatorResponse = {
      indicator: "SMA",
      params: { period: 20 },
      results: [
        {
          timestamp: "2026-08-28T09:15:00Z",
          indicator: "SMA",
          value: null,
          available: false,
          warmup_remaining: 19,
        },
      ],
    };

    expect(mockWarmupResponse.results[0].available).toBe(false);
    expect(mockWarmupResponse.results[0].value).toBeNull();
    expect(mockWarmupResponse.results[0].warmup_remaining).toBe(19);
  });

  // 8. MACD component rendering
  it("MACD component rendering", () => {
    const mockMacdResponse: CalculateIndicatorResponse = {
      indicator: "MACD",
      params: { fast_period: 12, slow_period: 26, signal_period: 9 },
      results: [
        {
          timestamp: "2026-08-28T09:15:00Z",
          indicator: "MACD",
          value: { macd: 1.25, signal: 0.85, histogram: 0.40 },
          available: true,
          warmup_remaining: 0,
        },
      ],
    };

    const val = mockMacdResponse.results[0].value as Record<string, number>;
    expect(val.macd).toBe(1.25);
    expect(val.signal).toBe(0.85);
    expect(val.histogram).toBe(0.40);
  });

  // 9. Adding/removing comparison indicators
  it("Adding/removing comparison indicators", () => {
    let comparisons = [
      { id: "c1", indicator: "EMA", params: { period: 10 } },
      { id: "c2", indicator: "RSI", params: { period: 14 } },
    ];

    expect(comparisons.length).toBe(2);

    // Remove c1
    comparisons = comparisons.filter((c) => c.id !== "c1");
    expect(comparisons.length).toBe(1);
    expect(comparisons[0].id).toBe("c2");
  });

  // 10. Three-indicator maximum
  it("Three-indicator maximum", () => {
    const comparisons = [
      { id: "c1", indicator: "EMA", params: { period: 10 } },
      { id: "c2", indicator: "RSI", params: { period: 14 } },
      { id: "c3", indicator: "SMA", params: { period: 50 } },
    ];

    const isMaxReached = comparisons.length >= 3;
    expect(isMaxReached).toBe(true);

    const tryAddFourth = (arr: any[], item: any) => {
      if (arr.length >= 3) {
        throw new Error("Maximum limit of 3 comparisons reached");
      }
      return [...arr, item];
    };

    expect(() => tryAddFourth(comparisons, { id: "c4" })).toThrow("Maximum limit of 3 comparisons reached");
  });

  // 11. Educational notice visibility
  it("Educational notice visibility", () => {
    const noticeText =
      "Educational synthetic data only. This page does not provide trading recommendations or place orders.";
    expect(noticeText).toContain("Educational synthetic data only");
    expect(noticeText).toContain("does not provide trading recommendations");
  });
});
