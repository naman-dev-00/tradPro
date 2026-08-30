"use client";

import React, { useState, useEffect, useMemo } from "react";
import Link from "next/link";
import {
  getDatasetManifest,
  getStrategies,
  createHistoricalReplay,
  getExportJsonUrl,
  getExportCsvUrl,
  DatasetManifestEntry,
  StrategyResponse,
  HistoricalReplayResult,
} from "@/lib/api";
import { EducationalNotice } from "./EducationalNotice";
import { AuthHeaderBadge } from "@/components/AuthHeaderBadge";
import {
  Play,
  Loader2,
  AlertCircle,
  ShieldCheck,
  ChevronDown,
  ChevronUp,
  FileSpreadsheet,
  FileCode,
  Clock,
  History,
  GitCompare,
} from "lucide-react";



export function HistoricalReplayLab() {
  const [manifest, setManifest] = useState<DatasetManifestEntry[]>([]);
  const [strategies, setStrategies] = useState<StrategyResponse[]>([]);
  const [loadingManifest, setLoadingManifest] = useState(true);

  const [selectedStrategyId, setSelectedStrategyId] = useState<string>("");
  const [selectedReferenceId, setSelectedReferenceId] = useState<string>("");
  const [selectedSubjectIds, setSelectedSubjectIds] = useState<string[]>([]);
  const [startTimestamp, setStartTimestamp] = useState<string>("2026-08-28T09:15:00.000Z");
  const [endTimestamp, setEndTimestamp] = useState<string>("2026-08-28T15:30:00.000Z");
  const [samplingStep, setSamplingStep] = useState<number>(1);

  const [executing, setExecuting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [replayResult, setReplayResult] = useState<HistoricalReplayResult | null>(null);
  const [isReused, setIsReused] = useState<boolean>(false);
  const [expandedPointIndex, setExpandedPointIndex] = useState<number | null>(null);

  useEffect(() => {
    async function loadData() {
      try {
        setLoadingManifest(true);
        const [mList, sList] = await Promise.all([getDatasetManifest(), getStrategies()]);
        setManifest(mList);
        setStrategies(sList);

        const ref = mList.find((d) => d.category === "REFERENCE");
        if (ref) setSelectedReferenceId(ref.dataset_id);

        const subjs = mList.filter((d) => d.category === "SUBJECT").slice(0, 2);
        if (subjs.length > 0) setSelectedSubjectIds(subjs.map((s) => s.dataset_id));

        if (sList.length > 0) setSelectedStrategyId(sList[0].id);
      } catch (err: any) {
        setError(err.message || "Failed to load initial workspace data");
      } finally {
        setLoadingManifest(false);
      }
    }
    loadData();
  }, []);

  const referenceDatasets = useMemo(
    () => manifest.filter((d) => d.category === "REFERENCE"),
    [manifest]
  );
  const subjectDatasets = useMemo(
    () => manifest.filter((d) => d.category === "SUBJECT"),
    [manifest]
  );

  const estimatedPoints = useMemo(() => {
    if (!startTimestamp || !endTimestamp) return 0;
    try {
      const s = new Date(startTimestamp).getTime();
      const e = new Date(endTimestamp).getTime();
      if (isNaN(s) || isNaN(e) || s > e) return 0;
      const diffMins = (e - s) / (1000 * 60);
      const estCandles = Math.floor(diffMins / 15) + 1;
      return Math.max(1, Math.floor(estCandles / samplingStep));
    } catch {
      return 0;
    }
  }, [startTimestamp, endTimestamp, samplingStep]);

  const estimatedEvaluations = estimatedPoints * selectedSubjectIds.length;

  const isOverLimit = estimatedEvaluations > 20000 || selectedSubjectIds.length > 20 || estimatedPoints > 1000;

  const handleSubjectToggle = (id: string) => {
    setSelectedSubjectIds((prev) => {
      if (prev.includes(id)) return prev.filter((item) => item !== id);
      if (prev.length >= 20) return prev;
      return [...prev, id];
    });
  };

  const handleRunReplay = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    if (!selectedStrategyId) {
      setError("Please select a strategy.");
      return;
    }
    if (!selectedReferenceId) {
      setError("Please select a reference dataset.");
      return;
    }
    if (selectedSubjectIds.length === 0) {
      setError("Please select at least 1 subject dataset.");
      return;
    }
    if (selectedSubjectIds.length > 20) {
      setError("Maximum 20 subject datasets allowed.");
      return;
    }
    if (isOverLimit) {
      setError("Estimated evaluations exceed safety limit of 20,000 evaluations or 1,000 points.");
      return;
    }

    const stratObj = strategies.find((s) => s.id === selectedStrategyId);

    try {
      setExecuting(true);
      const response = await createHistoricalReplay({
        strategy_id: selectedStrategyId,
        strategy_payload: stratObj?.payload,
        reference_dataset_id: selectedReferenceId,
        subject_dataset_ids: selectedSubjectIds,
        start_timestamp: startTimestamp,
        end_timestamp: endTimestamp,
        sampling_step: samplingStep,
      });

      setIsReused(response.is_reused);
      setReplayResult(response.run.result_payload);
    } catch (err: any) {
      setError(err.message || "Historical replay execution failed.");
    } finally {
      setExecuting(false);
    }
  };

  return (
    <div className="mx-auto max-w-7xl px-4 py-8 text-slate-100 sm:px-6 lg:px-8">
      {/* Header */}
      <div className="mb-8 flex flex-col items-start justify-between gap-4 md:flex-row md:items-center">
        <div>
          <h1 className="text-3xl font-extrabold tracking-tight text-white">
            Historical Replay Lab
          </h1>
          <p className="mt-1 text-sm text-slate-400">
            Evaluate Boolean strategy rules repeatedly across historical synthetic candle timestamps
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Link
            href="/replay-comparison-lab"
            className="inline-flex items-center space-x-2 rounded-lg border border-slate-700 bg-slate-800/80 px-3 py-1.5 text-xs font-semibold text-slate-200 transition-colors hover:bg-slate-700"
          >
            <GitCompare className="h-4 w-4 text-sky-400" />
            <span>Replay Comparison</span>
          </Link>
          <Link
            href="/inspection-history"
            className="inline-flex items-center space-x-2 rounded-lg border border-slate-700 bg-slate-800/80 px-3 py-1.5 text-xs font-semibold text-slate-200 transition-colors hover:bg-slate-700"
          >
            <History className="h-4 w-4 text-emerald-400" />
            <span>Inspection History</span>
          </Link>
          <Link
            href="/data-quality-lab"
            className="inline-flex items-center space-x-2 rounded-lg border border-slate-700 bg-slate-800/80 px-3 py-1.5 text-xs font-semibold text-slate-200 transition-colors hover:bg-slate-700"
          >
            <ShieldCheck className="h-4 w-4 text-emerald-400" />
            <span>Data Quality</span>
          </Link>
          <AuthHeaderBadge />
        </div>
      </div>

      <EducationalNotice />

      {error && (
        <div
          role="alert"
          className="aria-live-polite mb-6 rounded-xl border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-200"
        >
          <div className="flex items-center space-x-2">
            <AlertCircle className="h-5 w-5 text-red-400" />
            <span className="font-semibold">{error}</span>
          </div>
        </div>
      )}

      {/* Replay Configuration Form */}
      <form onSubmit={handleRunReplay} className="mb-10 rounded-2xl border border-slate-800 bg-slate-900/60 p-6 backdrop-blur-xl shadow-xl">
        <h2 className="mb-4 text-lg font-bold text-slate-200">Replay Parameters</h2>
        <div className="grid grid-cols-1 gap-6 md:grid-cols-2 lg:grid-cols-3">
          {/* Strategy */}
          <div>
            <label htmlFor="strategy-select" className="mb-2 block text-xs font-semibold uppercase tracking-wider text-slate-400">
              Strategy
            </label>
            <select
              id="strategy-select"
              value={selectedStrategyId}
              onChange={(e) => setSelectedStrategyId(e.target.value)}
              className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-white focus:border-cyan-500 focus:outline-none focus:ring-1 focus:ring-cyan-500"
            >
              {strategies.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name} ({s.timeframe})
                </option>
              ))}
            </select>
          </div>

          {/* Reference Dataset */}
          <div>
            <label htmlFor="reference-select" className="mb-2 block text-xs font-semibold uppercase tracking-wider text-slate-400">
              Reference Dataset (REFERENCE)
            </label>
            <select
              id="reference-select"
              value={selectedReferenceId}
              onChange={(e) => setSelectedReferenceId(e.target.value)}
              className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-white focus:border-cyan-500 focus:outline-none focus:ring-1 focus:ring-cyan-500"
            >
              {referenceDatasets.map((d) => (
                <option key={d.dataset_id} value={d.dataset_id}>
                  {d.display_name} ({d.completed_candle_count} candles)
                </option>
              ))}
            </select>
          </div>

          {/* Time Range & Sampling */}
          <div>
            <label className="mb-2 block text-xs font-semibold uppercase tracking-wider text-slate-400">
              Time Range (UTC) & Sampling
            </label>
            <div className="grid grid-cols-3 gap-2">
              <input
                type="text"
                value={startTimestamp}
                onChange={(e) => setStartTimestamp(e.target.value)}
                placeholder="Start UTC"
                className="rounded-lg border border-slate-700 bg-slate-950 px-2.5 py-1.5 text-xs text-white focus:border-cyan-500 focus:outline-none"
              />
              <input
                type="text"
                value={endTimestamp}
                onChange={(e) => setEndTimestamp(e.target.value)}
                placeholder="End UTC"
                className="rounded-lg border border-slate-700 bg-slate-950 px-2.5 py-1.5 text-xs text-white focus:border-cyan-500 focus:outline-none"
              />
              <select
                value={samplingStep}
                onChange={(e) => setSamplingStep(parseInt(e.target.value, 10))}
                className="rounded-lg border border-slate-700 bg-slate-950 px-2 py-1.5 text-xs text-white focus:border-cyan-500 focus:outline-none"
              >
                <option value={1}>Step 1</option>
                <option value={2}>Step 2</option>
                <option value={5}>Step 5</option>
              </select>
            </div>
          </div>
        </div>

        {/* Subject Selection */}
        <div className="mt-6">
          <label className="mb-2 flex items-center justify-between text-xs font-semibold uppercase tracking-wider text-slate-400">
            <span>Subject Datasets ({selectedSubjectIds.length}/20 selected)</span>
            <span className="text-slate-500">Select 1 to 20 candidate options</span>
          </label>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {subjectDatasets.map((ds) => {
              const isChecked = selectedSubjectIds.includes(ds.dataset_id);
              return (
                <label
                  key={ds.dataset_id}
                  className={`flex cursor-pointer items-start space-x-3 rounded-lg border p-3 transition-colors ${
                    isChecked
                      ? "border-cyan-500/60 bg-cyan-500/10 text-white"
                      : "border-slate-800 bg-slate-950/60 text-slate-400 hover:border-slate-700"
                  }`}
                >
                  <input
                    type="checkbox"
                    checked={isChecked}
                    onChange={() => handleSubjectToggle(ds.dataset_id)}
                    className="mt-0.5 h-4 w-4 rounded border-slate-700 text-cyan-500 focus:ring-cyan-500"
                  />
                  <div className="text-xs">
                    <span className="font-semibold text-slate-200">{ds.display_name}</span>
                    <span className="ml-2 font-mono text-[10px] text-slate-500">
                      ({ds.completed_candle_count} candles)
                    </span>
                  </div>
                </label>
              );
            })}
          </div>
        </div>

        {/* Live Estimate Calculator */}
        <div className="mt-6 flex flex-col items-start justify-between gap-4 rounded-xl border border-slate-800 bg-slate-950/80 p-4 sm:flex-row sm:items-center">
          <div className="flex items-center space-x-3 text-xs text-slate-300">
            <Clock className="h-4 w-4 text-cyan-400" />
            <span>
              Estimated: <strong>{estimatedPoints}</strong> timestamps × <strong>{selectedSubjectIds.length}</strong> subjects ={" "}
              <strong className={isOverLimit ? "text-red-400" : "text-emerald-400"}>
                {estimatedEvaluations} total evaluations
              </strong>
            </span>
          </div>

          <button
            type="submit"
            disabled={executing || loadingManifest || isOverLimit || selectedSubjectIds.length === 0}
            className="inline-flex items-center space-x-2 rounded-xl bg-cyan-500 px-5 py-2.5 text-sm font-semibold text-slate-950 shadow-lg shadow-cyan-500/20 transition-all hover:bg-cyan-400 disabled:opacity-50"
          >
            {executing ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" />
                <span>Running Replay...</span>
              </>
            ) : (
              <>
                <Play className="h-4 w-4 fill-current" />
                <span>Run Historical Replay</span>
              </>
            )}
          </button>
        </div>
      </form>

      {/* Screen Reader Live Region */}
      <div role="status" aria-live="polite" className="sr-only">
        {executing
          ? "Historical replay execution in progress..."
          : replayResult
          ? `Historical replay completed for ${replayResult.sampled_timestamp_count} timestamps.`
          : ""}
      </div>

      {/* Results View */}
      {replayResult && (
        <div className="space-y-8">
          {/* Summary & Exports Header */}
          <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6 backdrop-blur-xl shadow-xl">
            <div className="flex flex-col items-start justify-between gap-4 border-b border-slate-800/80 pb-4 md:flex-row md:items-center">
              <div>
                <div className="flex items-center space-x-2">
                  <h2 className="text-xl font-bold text-white">Replay Summary Results</h2>
                  {isReused && (
                    <span className="rounded-full bg-emerald-500/20 px-2.5 py-0.5 text-xs font-semibold text-emerald-300">
                      Reused Deduplicated Run
                    </span>
                  )}
                </div>
                <p className="mt-1 text-xs text-slate-400">
                  {replayResult.sampled_timestamp_count} sampled timestamps • {replayResult.total_evaluations} total evaluations
                </p>
              </div>

              {replayResult.run_id && (
                <div className="flex items-center space-x-3">
                  <a
                    href={getExportJsonUrl(replayResult.run_id)}
                    download
                    className="inline-flex items-center space-x-1.5 rounded-lg border border-slate-700 bg-slate-800/80 px-3 py-1.5 text-xs font-medium text-slate-200 transition-colors hover:bg-slate-700"
                  >
                    <FileCode className="h-4 w-4 text-cyan-400" />
                    <span>Export JSON</span>
                  </a>
                  <a
                    href={getExportCsvUrl(replayResult.run_id)}
                    download
                    className="inline-flex items-center space-x-1.5 rounded-lg border border-slate-700 bg-slate-800/80 px-3 py-1.5 text-xs font-medium text-slate-200 transition-colors hover:bg-slate-700"
                  >
                    <FileSpreadsheet className="h-4 w-4 text-emerald-400" />
                    <span>Export CSV</span>
                  </a>
                </div>
              )}
            </div>

            {/* Aggregate Status Counts Grid */}
            <div className="mt-6 grid grid-cols-2 gap-4 sm:grid-cols-4">
              <div className="rounded-xl border border-emerald-500/30 bg-emerald-500/10 p-4">
                <span className="text-xs font-semibold uppercase text-emerald-400">TRUE Count</span>
                <p className="mt-1 text-2xl font-black text-emerald-300">
                  {replayResult.aggregate_status_counts["TRUE"] || 0}
                </p>
              </div>
              <div className="rounded-xl border border-rose-500/30 bg-rose-500/10 p-4">
                <span className="text-xs font-semibold uppercase text-rose-400">FALSE Count</span>
                <p className="mt-1 text-2xl font-black text-rose-300">
                  {replayResult.aggregate_status_counts["FALSE"] || 0}
                </p>
              </div>
              <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 p-4">
                <span className="text-xs font-semibold uppercase text-amber-400">UNAVAILABLE Count</span>
                <p className="mt-1 text-2xl font-black text-amber-300">
                  {replayResult.aggregate_status_counts["UNAVAILABLE"] || 0}
                </p>
              </div>
              <div className="rounded-xl border border-slate-700 bg-slate-800/60 p-4">
                <span className="text-xs font-semibold uppercase text-slate-400">INVALID Count</span>
                <p className="mt-1 text-2xl font-black text-slate-300">
                  {replayResult.aggregate_status_counts["INVALID"] || 0}
                </p>
              </div>
            </div>
          </div>

          {/* Categorical Status Timelines per Subject */}
          <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6 backdrop-blur-xl shadow-xl">
            <h3 className="mb-4 text-lg font-bold text-white">Subject Status Timelines</h3>

            <div className="space-y-6">
              {replayResult.subject_timelines.map((timeline) => (
                <div key={timeline.dataset_id} className="rounded-xl border border-slate-800 bg-slate-950/60 p-4">
                  <div className="mb-3 flex flex-col justify-between gap-2 sm:flex-row sm:items-center">
                    <span className="font-bold text-slate-200">{timeline.dataset_id}</span>
                    <div className="flex flex-wrap items-center gap-3 text-xs text-slate-400">
                      <span>Max TRUE run: <strong className="text-emerald-400">{timeline.consecutive_status_runs["TRUE"] || 0}</strong></span>
                      <span>Max FALSE run: <strong className="text-rose-400">{timeline.consecutive_status_runs["FALSE"] || 0}</strong></span>
                    </div>
                  </div>

                  {/* Status Bar Visualization */}
                  <div className="flex h-6 w-full gap-1 overflow-hidden rounded-md bg-slate-900 p-0.5">
                    {timeline.points.map((pt, idx) => {
                      let bg = "bg-slate-700";
                      let label = "INVALID";
                      if (pt.status === "TRUE") {
                        bg = "bg-emerald-500";
                        label = "TRUE";
                      } else if (pt.status === "FALSE") {
                        bg = "bg-rose-500";
                        label = "FALSE";
                      } else if (pt.status === "UNAVAILABLE") {
                        bg = "bg-amber-500";
                        label = "UNAVAILABLE";
                      }
                      return (
                        <div
                          key={idx}
                          title={`${new Date(pt.timestamp).toLocaleTimeString()}: ${label}`}
                          className={`h-full flex-1 transition-transform hover:scale-110 ${bg}`}
                        />
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* Expandable Replay Points List */}
          <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6 backdrop-blur-xl shadow-xl">
            <h3 className="mb-4 text-lg font-bold text-white">Sampled Timestamp Replay Points</h3>
            <div className="space-y-3">
              {replayResult.replay_points.map((pt, idx) => {
                const isExpanded = expandedPointIndex === idx;
                return (
                  <div key={idx} className="rounded-xl border border-slate-800 bg-slate-950/80 p-4">
                    <button
                      onClick={() => setExpandedPointIndex(isExpanded ? null : idx)}
                      className="flex w-full items-center justify-between text-left text-sm font-semibold text-slate-200"
                    >
                      <div className="flex items-center space-x-3">
                        <Clock className="h-4 w-4 text-cyan-400" />
                        <span>{new Date(pt.evaluation_timestamp).toUTCString()}</span>
                        <div className="flex items-center space-x-2 text-xs">
                          <span className="rounded bg-emerald-500/20 px-2 py-0.5 text-emerald-300">
                            TRUE: {pt.status_counts["TRUE"] || 0}
                          </span>
                          <span className="rounded bg-rose-500/20 px-2 py-0.5 text-rose-300">
                            FALSE: {pt.status_counts["FALSE"] || 0}
                          </span>
                        </div>
                      </div>
                      {isExpanded ? <ChevronUp className="h-4 w-4 text-slate-400" /> : <ChevronDown className="h-4 w-4 text-slate-400" />}
                    </button>

                    {isExpanded && (
                      <div className="mt-4 space-y-3 border-t border-slate-800/80 pt-3">
                        {pt.results.map((res) => (
                          <div key={res.dataset_id} className="rounded-lg border border-slate-800 bg-slate-900/40 p-3 text-xs">
                            <div className="flex items-center justify-between">
                              <span className="font-mono font-semibold text-slate-300">{res.dataset_id}</span>
                              <span
                                className={`rounded px-2 py-0.5 font-bold ${
                                  res.overall_status === "TRUE"
                                    ? "bg-emerald-500/20 text-emerald-400"
                                    : res.overall_status === "FALSE"
                                    ? "bg-rose-500/20 text-rose-400"
                                    : "bg-amber-500/20 text-amber-400"
                                }`}
                              >
                                {res.overall_status}
                              </span>
                            </div>
                            <p className="mt-1 text-slate-400">{res.inspection_summary}</p>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
