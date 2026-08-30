"use client";

import React, { useState, useEffect } from "react";
import Link from "next/link";
import {
  getStrategies,
  getDatasetManifest,
  evaluateMultiSeries,
  StrategyResponse,
  DatasetManifestEntry,
  MultiSeriesEvaluationResult,
  SeriesEvaluationResult,
  EvaluationStatus,
} from "@/lib/api";
import { EducationalNotice } from "./EducationalNotice";
import { RuleResultTree } from "../RuleLab/RuleResultTree";
import { AuthHeaderBadge } from "@/components/AuthHeaderBadge";

export const MultiSeriesLabWorkspace: React.FC = () => {
  const [strategies, setStrategies] = useState<StrategyResponse[]>([]);
  const [manifest, setManifest] = useState<DatasetManifestEntry[]>([]);
  const [selectedStrategyId, setSelectedStrategyId] = useState<string>("");
  const [selectedRefDatasetId, setSelectedRefDatasetId] = useState<string>("");
  const [selectedSubjectDatasetIds, setSelectedSubjectDatasetIds] = useState<string[]>([]);
  const [evalTimestamp, setEvalTimestamp] = useState<string>("2026-08-28T17:45:00.000Z");

  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<MultiSeriesEvaluationResult | null>(null);
  const [expandedSeriesMap, setExpandedSeriesMap] = useState<Record<string, boolean>>({});
  const [announcement, setAnnouncement] = useState<string>("");

  useEffect(() => {
    async function loadInitialData() {
      try {
        setLoading(true);
        const [strats, datasets] = await Promise.all([
          getStrategies(),
          getDatasetManifest(),
        ]);
        setStrategies(strats);
        setManifest(datasets);

        if (strats.length > 0) {
          setSelectedStrategyId(strats[0].id || "");
        }

        const refs = datasets.filter((d: DatasetManifestEntry) => d.category === "REFERENCE");
        if (refs.length > 0) {
          setSelectedRefDatasetId(refs[0].dataset_id);
        }

        const subjs = datasets.filter((d: DatasetManifestEntry) => d.category === "SUBJECT");
        if (subjs.length > 0) {
          // Select default 3 subject options
          setSelectedSubjectDatasetIds(subjs.slice(0, 3).map((s: DatasetManifestEntry) => s.dataset_id));
        }
      } catch (err: any) {
        setError(err.message || "Failed loading initial strategies or dataset manifest.");
      } finally {
        setLoading(false);
      }
    }
    loadInitialData();
  }, []);

  const referenceDatasets = manifest.filter((d: DatasetManifestEntry) => d.category === "REFERENCE");
  const subjectDatasets = manifest.filter((d: DatasetManifestEntry) => d.category === "SUBJECT");

  const toggleSubjectDataset = (id: string) => {
    if (selectedSubjectDatasetIds.includes(id)) {
      setSelectedSubjectDatasetIds(selectedSubjectDatasetIds.filter((s) => s !== id));
    } else {
      if (selectedSubjectDatasetIds.length >= 20) {
        setError("Maximum 20 subject datasets can be selected.");
        return;
      }
      setSelectedSubjectDatasetIds([...selectedSubjectDatasetIds, id]);
    }
    setError(null);
  };

  const handleSelectAllSubjects = () => {
    const allIds = subjectDatasets.map((s) => s.dataset_id).slice(0, 20);
    setSelectedSubjectDatasetIds(allIds);
    setError(null);
  };

  const handleClearSubjects = () => {
    setSelectedSubjectDatasetIds([]);
    setError(null);
  };

  const toggleExpandSeries = (datasetId: string) => {
    setExpandedSeriesMap((prev) => ({
      ...prev,
      [datasetId]: !prev[datasetId],
    }));
  };

  const handleEvaluate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedStrategyId) {
      setError("Please select a strategy.");
      return;
    }
    if (!selectedRefDatasetId) {
      setError("Please select a reference dataset.");
      return;
    }
    if (selectedSubjectDatasetIds.length === 0) {
      setError("Please select at least 1 subject dataset.");
      return;
    }
    if (selectedSubjectDatasetIds.length > 20) {
      setError("Maximum 20 subject datasets allowed.");
      return;
    }

    try {
      setLoading(true);
      setError(null);
      setAnnouncement("Running multi-series rule inspection...");

      const res = await evaluateMultiSeries({
        strategy_id: selectedStrategyId,
        reference_dataset_id: selectedRefDatasetId,
        subject_dataset_ids: selectedSubjectDatasetIds,
        eval_timestamp: evalTimestamp,
      });

      setResult(res);
      setAnnouncement(
        `Evaluation completed. Inspected ${res.total_series_evaluated} series. Results: ${res.status_counts.TRUE || 0} TRUE, ${res.status_counts.FALSE || 0} FALSE, ${res.status_counts.UNAVAILABLE || 0} UNAVAILABLE, ${res.status_counts.INVALID || 0} INVALID.`
      );
    } catch (err: any) {
      const msg = err.message || "Multi-series rule evaluation failed.";
      setError(msg);
      setAnnouncement(`Evaluation error: ${msg}`);
    } finally {
      setLoading(false);
    }
  };

  const getStatusBadge = (status: EvaluationStatus) => {
    switch (status) {
      case "TRUE":
        return (
          <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-semibold bg-emerald-500/20 text-emerald-300 border border-emerald-500/30">
            <svg className="w-3.5 h-3.5 text-emerald-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7" />
            </svg>
            TRUE
          </span>
        );
      case "FALSE":
        return (
          <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-semibold bg-rose-500/20 text-rose-300 border border-rose-500/30">
            <svg className="w-3.5 h-3.5 text-rose-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M6 18L18 6M6 6l12 12" />
            </svg>
            FALSE
          </span>
        );
      case "UNAVAILABLE":
        return (
          <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-semibold bg-amber-500/20 text-amber-300 border border-amber-500/30">
            <svg className="w-3.5 h-3.5 text-amber-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            UNAVAILABLE
          </span>
        );
      case "INVALID":
      default:
        return (
          <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-semibold bg-purple-500/20 text-purple-300 border border-purple-500/30">
            <svg className="w-3.5 h-3.5 text-purple-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
            </svg>
            INVALID
          </span>
        );
    }
  };

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8 text-slate-100">
      {/* Live Region for Accessibility Announcements */}
      <div role="status" aria-live="polite" className="sr-only">
        {announcement}
      </div>

      <header className="mb-6 flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-white mb-2">
            Multi-Series Rule Inspection Lab
          </h1>
          <p className="text-sm text-slate-400">
            Inspect Boolean strategy rules independently across multiple packaged synthetic subject datasets.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Link
            href="/replay-comparison-lab"
            className="inline-flex items-center space-x-1.5 rounded-lg border border-slate-700 bg-slate-800/80 px-3 py-1.5 text-xs font-semibold text-slate-200 transition-colors hover:bg-slate-700"
          >
            <span>Replay Comparison</span>
          </Link>
          <Link
            href="/historical-replay-lab"
            className="inline-flex items-center space-x-1.5 rounded-lg border border-slate-700 bg-slate-800/80 px-3 py-1.5 text-xs font-semibold text-slate-200 transition-colors hover:bg-slate-700"
          >
            <span>Historical Replay Lab</span>
          </Link>
          <Link
            href="/data-quality-lab"
            className="inline-flex items-center space-x-1.5 rounded-lg border border-slate-700 bg-slate-800/80 px-3 py-1.5 text-xs font-semibold text-emerald-400 transition-colors hover:bg-slate-700"
          >
            <span>Data Quality Lab</span>
          </Link>
          <AuthHeaderBadge />
        </div>
      </header>



      <EducationalNotice />

      {/* Error Alert Box */}
      {error && (
        <div role="alert" className="bg-rose-500/10 border border-rose-500/30 rounded-lg p-4 mb-6 text-rose-200 text-sm">
          <div className="font-semibold text-rose-400 mb-1">Inspection Request Error</div>
          <div>{error}</div>
        </div>
      )}

      {/* Form Controls */}
      <form onSubmit={handleEvaluate} className="bg-slate-900/80 border border-slate-800 rounded-xl p-5 mb-8 shadow-lg space-y-6">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          {/* Strategy Selection */}
          <div>
            <label htmlFor="strategy-select" className="block text-xs font-semibold uppercase tracking-wider text-slate-300 mb-2">
              Select Strategy
            </label>
            <select
              id="strategy-select"
              value={selectedStrategyId}
              onChange={(e) => setSelectedStrategyId(e.target.value)}
              className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:ring-2 focus:ring-sky-500"
            >
              {strategies.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name} ({s.timeframe})
                </option>
              ))}
            </select>
          </div>

          {/* Reference Dataset Selection */}
          <div>
            <label htmlFor="reference-dataset-select" className="block text-xs font-semibold uppercase tracking-wider text-slate-300 mb-2">
              Reference Dataset (Underlying Index)
            </label>
            <select
              id="reference-dataset-select"
              value={selectedRefDatasetId}
              onChange={(e) => setSelectedRefDatasetId(e.target.value)}
              className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:ring-2 focus:ring-sky-500"
            >
              {referenceDatasets.map((d) => (
                <option key={d.dataset_id} value={d.dataset_id}>
                  {d.display_name} ({d.completed_candle_count} completed candles)
                </option>
              ))}
            </select>
          </div>
        </div>

        {/* Keyboard-Accessible Subject Multi-Select List */}
        <div>
          <div className="flex items-center justify-between mb-2">
            <label id="subjects-group-label" className="text-xs font-semibold uppercase tracking-wider text-slate-300">
              Subject Datasets ({selectedSubjectDatasetIds.length} / 20 selected)
            </label>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={handleSelectAllSubjects}
                className="text-xs font-medium text-sky-400 hover:text-sky-300 focus:outline-none focus:underline"
              >
                Select All
              </button>
              <span className="text-slate-600">|</span>
              <button
                type="button"
                onClick={handleClearSubjects}
                className="text-xs font-medium text-slate-400 hover:text-slate-300 focus:outline-none focus:underline"
              >
                Clear Selection
              </button>
            </div>
          </div>

          <div
            role="group"
            aria-labelledby="subjects-group-label"
            className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3 bg-slate-950/60 p-4 border border-slate-800 rounded-lg max-h-60 overflow-y-auto"
          >
            {subjectDatasets.map((d) => {
              const isChecked = selectedSubjectDatasetIds.includes(d.dataset_id);
              return (
                <label
                  key={d.dataset_id}
                  className={`flex items-start gap-3 p-2.5 rounded-lg border text-xs cursor-pointer transition-colors ${
                    isChecked
                      ? "bg-sky-950/40 border-sky-600/40 text-slate-100"
                      : "bg-slate-900/40 border-slate-800/80 text-slate-400 hover:text-slate-200"
                  }`}
                >
                  <input
                    type="checkbox"
                    checked={isChecked}
                    onChange={() => toggleSubjectDataset(d.dataset_id)}
                    className="mt-0.5 rounded border-slate-700 bg-slate-900 text-sky-500 focus:ring-2 focus:ring-sky-500 shrink-0"
                  />
                  <div>
                    <div className="font-semibold text-slate-200">{d.display_name}</div>
                    <div className="text-[11px] text-slate-400 mt-0.5">
                      {d.instrument_id} • {d.timeframe} • {d.completed_candle_count} candles
                    </div>
                  </div>
                </label>
              );
            })}
          </div>
        </div>

        {/* Evaluation Timestamp Input */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6 items-end">
          <div>
            <label htmlFor="eval-timestamp-input" className="block text-xs font-semibold uppercase tracking-wider text-slate-300 mb-2">
              Required UTC Evaluation Timestamp
            </label>
            <input
              id="eval-timestamp-input"
              type="text"
              value={evalTimestamp}
              onChange={(e) => setEvalTimestamp(e.target.value)}
              className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:ring-2 focus:ring-sky-500 font-mono"
            />
            <p className="text-[11px] text-slate-500 mt-1">ISO 8601 UTC timestamp ($t \le eval\_timestamp$ strictly enforced).</p>
          </div>

          <div className="flex justify-end">
            <button
              type="submit"
              disabled={loading || selectedSubjectDatasetIds.length === 0}
              className="w-full sm:w-auto px-6 py-2.5 rounded-lg bg-sky-600 hover:bg-sky-500 disabled:opacity-50 disabled:cursor-not-allowed font-semibold text-sm text-white shadow-md transition-colors focus:outline-none focus:ring-2 focus:ring-sky-400"
            >
              {loading ? "Evaluating..." : "Run Multi-Series Inspection"}
            </button>
          </div>
        </div>
      </form>

      {/* Results Workspace */}
      {result && (
        <section aria-label="Inspection Results" className="space-y-6">
          {/* Status Counts Summary Grid */}
          <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
            <div className="bg-slate-900 border border-emerald-500/30 rounded-xl p-4 text-center">
              <div className="text-xs uppercase tracking-wider text-emerald-400 font-semibold">TRUE</div>
              <div className="text-2xl font-bold text-emerald-300 mt-1">{result.status_counts.TRUE || 0}</div>
            </div>
            <div className="bg-slate-900 border border-rose-500/30 rounded-xl p-4 text-center">
              <div className="text-xs uppercase tracking-wider text-rose-400 font-semibold">FALSE</div>
              <div className="text-2xl font-bold text-rose-300 mt-1">{result.status_counts.FALSE || 0}</div>
            </div>
            <div className="bg-slate-900 border border-amber-500/30 rounded-xl p-4 text-center">
              <div className="text-xs uppercase tracking-wider text-amber-400 font-semibold">UNAVAILABLE</div>
              <div className="text-2xl font-bold text-amber-300 mt-1">{result.status_counts.UNAVAILABLE || 0}</div>
            </div>
            <div className="bg-slate-900 border border-purple-500/30 rounded-xl p-4 text-center">
              <div className="text-xs uppercase tracking-wider text-purple-400 font-semibold">INVALID</div>
              <div className="text-2xl font-bold text-purple-300 mt-1">{result.status_counts.INVALID || 0}</div>
            </div>
            <div className="col-span-2 sm:col-span-1 bg-slate-900 border border-slate-800 rounded-xl p-4 text-center">
              <div className="text-xs uppercase tracking-wider text-slate-400 font-semibold">TOTAL EVALUATED</div>
              <div className="text-2xl font-bold text-slate-200 mt-1">{result.total_series_evaluated}</div>
            </div>
          </div>

          {/* Series Result Cards (Deterministic Input Order) */}
          <div className="space-y-4">
            <h2 className="text-lg font-bold text-slate-200">Independent Series Inspection Results</h2>
            {result.results.map((ser: SeriesEvaluationResult, index: number) => {
              const isExpanded = !!expandedSeriesMap[ser.dataset_id];
              const regionId = `series-tree-${ser.dataset_id}`;

              return (
                <div
                  key={ser.dataset_id}
                  className="bg-slate-900/90 border border-slate-800 rounded-xl overflow-hidden shadow-sm"
                >
                  <div className="p-4 sm:p-5 flex flex-col sm:flex-row sm:items-center justify-between gap-4">
                    <div className="space-y-1">
                      <div className="flex items-center gap-3">
                        <span className="text-xs font-mono text-slate-500">#{index + 1}</span>
                        <h3 className="text-base font-bold text-slate-100">{ser.dataset_id}</h3>
                        {getStatusBadge(ser.overall_status)}
                      </div>
                      <p className="text-xs text-slate-400">{ser.inspection_summary}</p>
                      <div className="flex flex-wrap items-center gap-2 text-[11px] text-slate-400 pt-1">
                        <span>Instrument: <strong className="text-slate-200">{ser.instrument_id}</strong></span>
                        <span>•</span>
                        <span>Timeframe: <strong className="text-slate-200">{ser.timeframe}</strong></span>
                        {ser.candle_timestamp_used && (
                          <>
                            <span>•</span>
                            <span>Candle: <strong className="text-slate-200">{ser.candle_timestamp_used}</strong></span>
                          </>
                        )}
                      </div>
                    </div>

                    <div className="flex items-center gap-2 shrink-0">
                      <button
                        type="button"
                        onClick={() => toggleExpandSeries(ser.dataset_id)}
                        aria-expanded={isExpanded}
                        aria-controls={regionId}
                        className="px-3 py-1.5 rounded-lg border border-slate-700 bg-slate-800 hover:bg-slate-700 text-xs font-medium text-slate-200 transition-colors focus:outline-none focus:ring-2 focus:ring-sky-500"
                      >
                        {isExpanded ? "Hide Rule Tree" : "Inspect Rule Tree"}
                      </button>

                      <Link
                        href={`/builder?id=${selectedStrategyId}`}
                        className="px-3 py-1.5 rounded-lg border border-slate-800 bg-slate-900 hover:bg-slate-800 text-xs font-medium text-sky-400 hover:text-sky-300 transition-colors focus:outline-none focus:ring-2 focus:ring-sky-500"
                      >
                        Builder
                      </Link>

                      <Link
                        href={`/indicator-lab?dataset=${ser.dataset_id}`}
                        className="px-3 py-1.5 rounded-lg border border-slate-800 bg-slate-900 hover:bg-slate-800 text-xs font-medium text-slate-300 hover:text-white transition-colors focus:outline-none focus:ring-2 focus:ring-sky-500"
                      >
                        Indicator Lab
                      </Link>
                    </div>
                  </div>

                  {/* Expandable Rule Result Tree */}
                  {isExpanded && (
                    <div id={regionId} role="region" aria-label={`Rule result tree for ${ser.dataset_id}`} className="border-t border-slate-800 p-4 sm:p-5 bg-slate-950/60 space-y-4">
                      {ser.reference_result && (
                        <div>
                          <h4 className="text-xs font-semibold uppercase tracking-wider text-slate-400 mb-2">
                            Reference Scope Result ({result.reference_dataset_id})
                          </h4>
                          <RuleResultTree result={ser.reference_result} />
                        </div>
                      )}

                      {ser.subject_result && (
                        <div>
                          <h4 className="text-xs font-semibold uppercase tracking-wider text-slate-400 mb-2">
                            Subject Scope Result ({ser.dataset_id})
                          </h4>
                          <RuleResultTree result={ser.subject_result} />
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </section>
      )}
    </div>
  );
};
