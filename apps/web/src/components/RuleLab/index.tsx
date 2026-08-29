"use client";

import React, { useState, useEffect } from "react";
import Link from "next/link";
import { EducationalNotice } from "./EducationalNotice";
import { RuleResultTree } from "./RuleResultTree";
import {
  getStrategies,
  getSyntheticDatasets,
  getSyntheticDatasetById,
  evaluateRules,
  StrategyResponse,
  DatasetMetadata,
  RuleEvaluationResult,
} from "@/lib/api";

const PRESET_EXAMPLE_STRATEGY = {
  id: "preset-rsi-ema-strategy",
  name: "Preset Educational Rule Strategy (RSI & EMA)",
  timeframe: "15m",
  candidate_selection_mode: "FIRST_ELIGIBLE",
  global_conditions: {
    id: "global.group.0",
    type: "AND",
    conditions: [
      {
        id: "global.cond.ema",
        type: "CONDITION",
        lhs: { indicator: "EMA", symbol: "NIFTY", params: { period: 5 } },
        operator: "GREATER_THAN",
        rhs: { type: "NUMBER", value: 100.0 },
      },
    ],
  },
  candidate_conditions: {
    id: "candidate.group.0",
    type: "AND",
    conditions: [
      {
        id: "candidate.cond.rsi",
        type: "CONDITION",
        lhs: { indicator: "RSI", symbol: "CANDIDATE", params: { period: 14 } },
        operator: "GREATER_THAN",
        rhs: { type: "NUMBER", value: 40.0 },
      },
    ],
  },
  action: {
    type: "PAPER_TRADE",
    risk_config: {
      max_position_size: 100000,
      stop_loss_pct: 2.5,
      take_profit_pct: 5,
      validity_window: 5,
    },
  },
};

export const RuleLabWorkspace: React.FC = () => {
  const [strategies, setStrategies] = useState<StrategyResponse[]>([]);
  const [datasets, setDatasets] = useState<DatasetMetadata[]>([]);
  const [selectedStrategyId, setSelectedStrategyId] = useState<string>("PRESET");
  const [selectedRefDatasetId, setSelectedRefDatasetId] = useState<string>("synthetic_underlying_nifty_15m");
  const [selectedSubjDatasetId, setSelectedSubjDatasetId] = useState<string>("synthetic_candidate_option_ce_23000_15m");
  const [candleIndex, setCandleIndex] = useState<number>(34);
  const [maxCandleCount, setMaxCandleCount] = useState<number>(35);
  const [evalResult, setEvalResult] = useState<RuleEvaluationResult | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function loadInitialData() {
      try {
        const [strats, dsets] = await Promise.all([
          getStrategies().catch(() => []),
          getSyntheticDatasets().catch(() => ({ datasets: [] })),
        ]);
        setStrategies(strats);
        setDatasets(dsets.datasets);
      } catch (err: any) {
        console.error("Initial load error:", err);
      }
    }
    loadInitialData();
  }, []);

  useEffect(() => {
    async function updateDatasetLength() {
      if (!selectedRefDatasetId) return;
      try {
        const detail = await getSyntheticDatasetById(selectedRefDatasetId);
        if (detail && detail.candles) {
          const total = detail.candles.length;
          setMaxCandleCount(total);
          setCandleIndex((prev) => (prev >= total ? Math.max(0, total - 1) : prev));
        }
      } catch (e) {
        console.error("Dataset detail fetch error:", e);
      }
    }
    updateDatasetLength();
  }, [selectedRefDatasetId]);

  const handleEvaluate = async () => {
    setLoading(true);
    setError(null);
    try {
      let strategyPayload: any = null;
      if (selectedStrategyId === "PRESET") {
        strategyPayload = PRESET_EXAMPLE_STRATEGY;
      } else {
        const found = strategies.find((s) => s.id === selectedStrategyId);
        if (found) {
          strategyPayload = found.payload;
          strategyPayload.id = found.id;
        } else {
          strategyPayload = PRESET_EXAMPLE_STRATEGY;
        }
      }

      // Fetch reference dataset to get timestamp at candleIndex
      const refDetail = await getSyntheticDatasetById(selectedRefDatasetId);
      const evalTs = refDetail.candles[candleIndex]?.timestamp;

      const result = await evaluateRules({
        strategy: strategyPayload,
        reference_dataset_id: selectedRefDatasetId,
        subject_dataset_id: selectedSubjDatasetId || undefined,
        eval_timestamp: evalTs,
      });

      setEvalResult(result);
    } catch (err: any) {
      setError(err.message || "Rule evaluation failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 p-4 sm:p-6 md:p-8 font-sans">
      <div className="max-w-7xl mx-auto space-y-6">
        {/* Header */}
        <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 border-b border-slate-800 pb-4">
          <div>
            <h1 className="text-2xl font-extrabold text-slate-100 tracking-tight flex items-center gap-2">
              <span>Rule Evaluation Inspection Lab</span>
              <span className="text-xs bg-indigo-500/20 text-indigo-300 border border-indigo-500/30 px-2.5 py-0.5 rounded-full font-mono uppercase">
                Milestone 2B
              </span>
            </h1>
            <p className="text-xs text-slate-400 mt-1">
              Deterministic Boolean rule evaluation of options strategies against synthetic educational datasets.
            </p>
          </div>

          <div className="flex items-center gap-2">
            <Link
              href="/builder"
              className="px-3.5 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 rounded text-xs font-semibold transition-colors flex items-center gap-1.5"
            >
              <span>⚙️</span> Strategy Builder
            </Link>
            <Link
              href="/indicator-lab"
              className="px-3.5 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 rounded text-xs font-semibold transition-colors flex items-center gap-1.5"
            >
              <span>📈</span> Indicator Lab
            </Link>
            <Link
              href="/multi-series-lab"
              className="px-3.5 py-1.5 bg-sky-950/60 hover:bg-sky-900/60 text-sky-300 border border-sky-600/40 rounded text-xs font-semibold transition-colors flex items-center gap-1.5"
            >
              <span>🌐</span> Multi-Series Lab
            </Link>
          </div>
        </div>

        {/* Educational Notice Banner */}
        <EducationalNotice />

        {/* Control Panel */}
        <div className="bg-slate-900 border border-slate-800 rounded-lg p-4 sm:p-5 shadow-lg space-y-4">
          <h2 className="text-sm font-bold text-slate-200 uppercase tracking-wider">Evaluation Configuration</h2>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            {/* Strategy Selector */}
            <div>
              <label className="block text-xs font-semibold text-slate-400 mb-1">Strategy</label>
              <select
                className="w-full bg-slate-950 border border-slate-800 rounded px-3 py-2 text-xs focus:outline-none focus:border-indigo-500 text-slate-200"
                value={selectedStrategyId}
                onChange={(e) => setSelectedStrategyId(e.target.value)}
              >
                <option value="PRESET">Preset Educational Strategy (RSI & EMA)</option>
                {strategies.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name} ({s.id.slice(0, 8)}...)
                  </option>
                ))}
              </select>
            </div>

            {/* Reference Dataset */}
            <div>
              <label className="block text-xs font-semibold text-slate-400 mb-1">Reference Dataset (Global Scope)</label>
              <select
                className="w-full bg-slate-950 border border-slate-800 rounded px-3 py-2 text-xs focus:outline-none focus:border-indigo-500 text-slate-200"
                value={selectedRefDatasetId}
                onChange={(e) => setSelectedRefDatasetId(e.target.value)}
              >
                {datasets.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name}
                  </option>
                ))}
              </select>
            </div>

            {/* Subject Dataset */}
            <div>
              <label className="block text-xs font-semibold text-slate-400 mb-1">Subject Dataset (Candidate Scope)</label>
              <select
                className="w-full bg-slate-950 border border-slate-800 rounded px-3 py-2 text-xs focus:outline-none focus:border-indigo-500 text-slate-200"
                value={selectedSubjDatasetId}
                onChange={(e) => setSelectedSubjDatasetId(e.target.value)}
              >
                <option value="">None (Global Scope Only)</option>
                {datasets.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name}
                  </option>
                ))}
              </select>
            </div>
          </div>

          {/* Completed Candle Index Slider */}
          <div className="bg-slate-950/60 border border-slate-800/80 p-3.5 rounded-lg space-y-2">
            <div className="flex justify-between items-center text-xs">
              <span className="font-semibold text-slate-300">Target Completed Candle Index:</span>
              <span className="font-mono text-indigo-400 font-bold bg-indigo-950/80 border border-indigo-500/30 px-2 py-0.5 rounded">
                Index {candleIndex} / {maxCandleCount - 1}
              </span>
            </div>
            <input
              type="range"
              min="0"
              max={Math.max(0, maxCandleCount - 1)}
              value={candleIndex}
              onChange={(e) => setCandleIndex(Number(e.target.value))}
              className="w-full accent-indigo-500 bg-slate-800 h-2 rounded cursor-pointer"
            />
          </div>

          {/* Evaluate Button */}
          <button
            onClick={handleEvaluate}
            disabled={loading}
            className="w-full py-2.5 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white font-bold text-xs rounded transition-colors shadow-md flex items-center justify-center gap-2"
          >
            {loading ? <span>Evaluating Rules...</span> : <span>Execute Boolean Rule Evaluation</span>}
          </button>
        </div>

        {/* Error Banner */}
        {error && (
          <div className="bg-rose-950/40 border border-rose-500/40 rounded-lg p-4 text-xs text-rose-200">
            <span className="font-bold text-rose-400">Evaluation Error:</span> {error}
          </div>
        )}

        {/* Evaluation Summary & Results */}
        {evalResult && (
          <div className="space-y-6">
            {/* Overall Status Summary Card */}
            <div className="bg-slate-900 border border-slate-800 rounded-lg p-5 shadow-lg space-y-3">
              <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 border-b border-slate-800 pb-3">
                <div>
                  <span className="text-xs text-slate-400 uppercase font-semibold">Overall Strategy Evaluation Status</span>
                  <div className="flex items-center gap-3 mt-1">
                    <span
                      className={`text-xl font-extrabold px-3 py-1 rounded border ${
                        evalResult.overall_status === "TRUE"
                          ? "bg-emerald-500/20 text-emerald-400 border-emerald-500/40"
                          : evalResult.overall_status === "FALSE"
                          ? "bg-rose-500/20 text-rose-400 border-rose-500/40"
                          : evalResult.overall_status === "UNAVAILABLE"
                          ? "bg-amber-500/20 text-amber-400 border-amber-500/40"
                          : "bg-purple-500/20 text-purple-400 border-purple-500/40"
                      }`}
                    >
                      {evalResult.overall_status}
                    </span>
                    <span className="text-xs text-slate-400 font-mono">Evaluated At: {evalResult.evaluated_at}</span>
                  </div>
                </div>

                {selectedStrategyId !== "PRESET" && (
                  <Link
                    href={`/builder?id=${selectedStrategyId}`}
                    className="text-xs bg-slate-800 hover:bg-slate-700 text-slate-200 px-3 py-1.5 rounded border border-slate-700 font-semibold self-start transition-colors"
                  >
                    Edit Strategy in Builder →
                  </Link>
                )}
              </div>

              {/* Status Breakdown Counts */}
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-center text-xs">
                <div className="bg-slate-950 p-2.5 rounded border border-emerald-500/20">
                  <div className="text-slate-400 font-medium">Passed Conditions</div>
                  <div className="text-lg font-bold text-emerald-400">{evalResult.passed_condition_ids.length}</div>
                </div>
                <div className="bg-slate-950 p-2.5 rounded border border-rose-500/20">
                  <div className="text-slate-400 font-medium">Failed Conditions</div>
                  <div className="text-lg font-bold text-rose-400">{evalResult.failed_condition_ids.length}</div>
                </div>
                <div className="bg-slate-950 p-2.5 rounded border border-amber-500/20">
                  <div className="text-slate-400 font-medium">Unavailable Conditions</div>
                  <div className="text-lg font-bold text-amber-400">{evalResult.unavailable_condition_ids.length}</div>
                </div>
                <div className="bg-slate-950 p-2.5 rounded border border-purple-500/20">
                  <div className="text-slate-400 font-medium">Invalid Conditions</div>
                  <div className="text-lg font-bold text-purple-400">{evalResult.invalid_condition_ids.length}</div>
                </div>
              </div>
            </div>

            {/* Reference Series Result Tree */}
            {evalResult.reference_series_result && (
              <RuleResultTree
                result={evalResult.reference_series_result}
                title={`Reference Scope Evaluation Tree (${evalResult.reference_timestamp || "N/A"})`}
              />
            )}

            {/* Subject Series Result Tree */}
            {evalResult.subject_series_result && (
              <RuleResultTree
                result={evalResult.subject_series_result}
                title={`Subject Scope Evaluation Tree (${evalResult.subject_timestamp || "N/A"})`}
              />
            )}
          </div>
        )}
      </div>
    </div>
  );
};
