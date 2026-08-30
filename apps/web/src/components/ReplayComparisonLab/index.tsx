"use client";

import React, { useEffect, useState, useCallback } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";

import {
  listInspectionRuns,
  verifyReplayRun,
  compareReplays,
  InspectionRunSummaryResponse,
  ReplayVerificationResult,
  ReplayComparisonResult,
} from "@/lib/api";
import { EducationalNotice } from "./EducationalNotice";
import { VerificationCard } from "./VerificationCard";
import { TransitionMatrixCard } from "./TransitionMatrixCard";
import { DifferenceTable } from "./DifferenceTable";
import { ExportButtons } from "./ExportButtons";
import { GitCompare, Home, History, Layers, PlayCircle, RefreshCw, AlertCircle } from "lucide-react";

export function ReplayComparisonLab() {
  const searchParams = useSearchParams();
  const initialBaseline = searchParams?.get("baseline") || "";
  const initialComparison = searchParams?.get("comparison") || "";

  const [completedRuns, setCompletedRuns] = useState<InspectionRunSummaryResponse[]>([]);
  const [loadingRuns, setLoadingRuns] = useState<boolean>(true);
  const [runsError, setRunsError] = useState<string | null>(null);

  const [baselineRunId, setBaselineRunId] = useState<string>(initialBaseline);
  const [comparisonRunId, setComparisonRunId] = useState<string>(initialComparison);

  const [baselineVerification, setBaselineVerification] = useState<ReplayVerificationResult | null>(null);
  const [comparisonVerification, setComparisonVerification] = useState<ReplayVerificationResult | null>(null);
  const [loadingVerification, setLoadingVerification] = useState<boolean>(false);

  const [comparisonResult, setComparisonResult] = useState<ReplayComparisonResult | null>(null);
  const [loadingComparison, setLoadingComparison] = useState<boolean>(false);
  const [comparisonError, setComparisonError] = useState<string | null>(null);

  const [includeUnchanged, setIncludeUnchanged] = useState<boolean>(false);
  const [ariaAnnouncement, setAriaAnnouncement] = useState<string>("");

  // Load all COMPLETED historical replay runs across pages
  const loadCompletedRuns = useCallback(async () => {
    setLoadingRuns(true);
    setRunsError(null);
    try {
      let page = 1;
      let totalPages = 1;
      const allCompleted: InspectionRunSummaryResponse[] = [];

      do {
        const res = await listInspectionRuns({
          page,
          page_size: 50,
          status: "COMPLETED",
          run_type: "HISTORICAL_REPLAY",
        });
        allCompleted.push(...res.items);
        totalPages = res.total_pages;
        page += 1;
      } while (page <= totalPages);

      setCompletedRuns(allCompleted);

      // Auto-select initial baseline/comparison if available
      if (allCompleted.length >= 2) {
        if (!initialBaseline && !baselineRunId) {
          setBaselineRunId(allCompleted[0].id);
        }
        if (!initialComparison && !comparisonRunId) {
          setComparisonRunId(allCompleted[1].id);
        }
      } else if (allCompleted.length === 1 && !baselineRunId) {
        setBaselineRunId(allCompleted[0].id);
      }
    } catch (err: any) {
      setRunsError(err.message || "Failed to load completed inspection runs.");
    } finally {
      setLoadingRuns(false);
    }
  }, [initialBaseline, initialComparison, baselineRunId, comparisonRunId]);

  useEffect(() => {
    loadCompletedRuns();
  }, [loadCompletedRuns]);

  // Execute comparison & verifications when selections change
  const executeComparison = useCallback(async () => {
    if (!baselineRunId || !comparisonRunId) {
      setComparisonResult(null);
      return;
    }

    if (baselineRunId === comparisonRunId) {
      setComparisonError("Baseline and Comparison cannot be the same inspection run.");
      setComparisonResult(null);
      return;
    }

    setLoadingComparison(true);
    setLoadingVerification(true);
    setComparisonError(null);

    try {
      // Parallel verification and comparison calls
      const [verBase, verComp, compRes] = await Promise.all([
        verifyReplayRun(baselineRunId),
        verifyReplayRun(comparisonRunId),
        compareReplays({
          baseline_run_id: baselineRunId,
          comparison_run_id: comparisonRunId,
          include_unchanged: includeUnchanged,
        }),
      ]);

      setBaselineVerification(verBase);
      setComparisonVerification(verComp);
      setComparisonResult(compRes);
      setAriaAnnouncement(
        `Comparison complete. ${compRes.changed_point_count} changed points and ${compRes.unchanged_point_count} unchanged points.`
      );
    } catch (err: any) {
      setComparisonError(err.message || "Failed to execute replay comparison.");
      setComparisonResult(null);
    } finally {
      setLoadingComparison(false);
      setLoadingVerification(false);
    }
  }, [baselineRunId, comparisonRunId, includeUnchanged]);

  useEffect(() => {
    if (baselineRunId && comparisonRunId && baselineRunId !== comparisonRunId) {
      executeComparison();
    }
  }, [baselineRunId, comparisonRunId, includeUnchanged, executeComparison]);

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 font-sans p-4 sm:p-6 lg:p-8 max-w-7xl mx-auto">
      {/* Hidden Live Region for Screen Readers */}
      <div className="sr-only" aria-live="polite" aria-atomic="true">
        {ariaAnnouncement}
      </div>

      {/* Header & Navigation */}
      <header className="mb-6 flex flex-col md:flex-row md:items-center md:justify-between gap-4 border-b border-slate-800 pb-5">
        <div>
          <div className="flex items-center space-x-2.5">
            <div className="p-2 rounded-lg bg-sky-950 text-sky-400 border border-sky-600/40">
              <GitCompare className="w-6 h-6" />
            </div>
            <h1 className="text-xl sm:text-2xl font-bold tracking-tight text-white">
              Deterministic Replay Comparison Lab
            </h1>
          </div>
          <p className="text-xs sm:text-sm text-slate-400 mt-1">
            Compare two packaged synthetic historical replay runs timestamp-by-timestamp and inspect Boolean status transitions.
          </p>
        </div>

        {/* Navigation Bar */}
        <nav aria-label="Main Navigation" className="flex flex-wrap items-center gap-2">
          <Link
            href="/"
            className="px-3 py-1.5 bg-slate-900 hover:bg-slate-800 border border-slate-800 text-slate-300 rounded-lg text-xs font-medium flex items-center space-x-1.5 transition-colors focus:outline-none focus:ring-1 focus:ring-sky-500"
          >
            <Home className="w-3.5 h-3.5" />
            <span>Home</span>
          </Link>
          <Link
            href="/inspection-history"
            className="px-3 py-1.5 bg-slate-900 hover:bg-slate-800 border border-slate-800 text-slate-300 rounded-lg text-xs font-medium flex items-center space-x-1.5 transition-colors focus:outline-none focus:ring-1 focus:ring-sky-500"
          >
            <History className="w-3.5 h-3.5" />
            <span>Inspection History</span>
          </Link>
          <Link
            href="/historical-replay-lab"
            className="px-3 py-1.5 bg-slate-900 hover:bg-slate-800 border border-slate-800 text-slate-300 rounded-lg text-xs font-medium flex items-center space-x-1.5 transition-colors focus:outline-none focus:ring-1 focus:ring-sky-500"
          >
            <PlayCircle className="w-3.5 h-3.5" />
            <span>Historical Replay Lab</span>
          </Link>
          <Link
            href="/multi-series-lab"
            className="px-3 py-1.5 bg-slate-900 hover:bg-slate-800 border border-slate-800 text-slate-300 rounded-lg text-xs font-medium flex items-center space-x-1.5 transition-colors focus:outline-none focus:ring-1 focus:ring-sky-500"
          >
            <Layers className="w-3.5 h-3.5" />
            <span>Multi-Series Lab</span>
          </Link>
        </nav>
      </header>

      {/* Educational Notice Banner */}
      <EducationalNotice />

      {/* Run Selectors Section */}
      <section aria-label="Run Selectors" className="p-5 bg-slate-900/60 border border-slate-800 rounded-xl shadow-lg mb-8">
        <div className="flex items-center justify-between mb-4">
          <h2 className="font-semibold text-slate-100 text-base">Select Historical Replay Runs</h2>
          <button
            type="button"
            onClick={loadCompletedRuns}
            disabled={loadingRuns}
            className="p-1.5 text-slate-400 hover:text-slate-100 rounded bg-slate-800/60 hover:bg-slate-800 transition-colors focus:outline-none focus:ring-1 focus:ring-sky-500"
            title="Refresh runs list"
          >
            <RefreshCw className={`w-4 h-4 ${loadingRuns ? "animate-spin" : ""}`} />
          </button>
        </div>

        {runsError && (
          <div className="p-3 mb-4 bg-red-950/40 border border-red-800 rounded-lg text-xs text-red-300 flex items-center space-x-2">
            <AlertCircle className="w-4 h-4 text-red-400 shrink-0" />
            <span>{runsError}</span>
          </div>
        )}

        <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
          {/* Baseline Run Selector */}
          <div>
            <label htmlFor="baseline-run-select" className="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-1.5">
              Baseline Run (Reference)
            </label>
            <select
              id="baseline-run-select"
              value={baselineRunId}
              onChange={(e) => setBaselineRunId(e.target.value)}
              disabled={loadingRuns || completedRuns.length === 0}
              className="w-full bg-slate-950 border border-slate-700 text-slate-100 text-xs rounded-lg p-2.5 focus:outline-none focus:ring-2 focus:ring-sky-500 font-mono disabled:opacity-50"
            >
              <option value="">-- Select Baseline Completed Run --</option>
              {completedRuns.map((run) => (
                <option key={run.id} value={run.id} disabled={run.id === comparisonRunId}>
                  {run.id.slice(0, 8)}... | {run.strategy_name || "Custom Strategy"} | {run.timeframe} ({new Date(run.created_at).toLocaleDateString()})
                </option>
              ))}
            </select>
          </div>

          {/* Comparison Run Selector */}
          <div>
            <label htmlFor="comparison-run-select" className="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-1.5">
              Comparison Run (Subject)
            </label>
            <select
              id="comparison-run-select"
              value={comparisonRunId}
              onChange={(e) => setComparisonRunId(e.target.value)}
              disabled={loadingRuns || completedRuns.length === 0}
              className="w-full bg-slate-950 border border-slate-700 text-slate-100 text-xs rounded-lg p-2.5 focus:outline-none focus:ring-2 focus:ring-sky-500 font-mono disabled:opacity-50"
            >
              <option value="">-- Select Comparison Completed Run --</option>
              {completedRuns.map((run) => (
                <option key={run.id} value={run.id} disabled={run.id === baselineRunId}>
                  {run.id.slice(0, 8)}... | {run.strategy_name || "Custom Strategy"} | {run.timeframe} ({new Date(run.created_at).toLocaleDateString()})
                </option>
              ))}
            </select>
          </div>
        </div>

        {/* Controls */}
        <div className="mt-4 pt-3 border-t border-slate-800/80 flex items-center justify-between text-xs">
          <label className="flex items-center space-x-2 text-slate-300 cursor-pointer">
            <input
              type="checkbox"
              checked={includeUnchanged}
              onChange={(e) => setIncludeUnchanged(e.target.checked)}
              className="rounded bg-slate-950 border-slate-700 text-sky-500 focus:ring-sky-500"
            />
            <span>Include unchanged replay points in detailed difference table</span>
          </label>
        </div>
      </section>

      {/* Comparison Error Banner */}
      {comparisonError && (
        <div className="p-4 mb-6 bg-red-950/40 border border-red-800 rounded-xl text-sm text-red-300 flex items-start space-x-3">
          <AlertCircle className="w-5 h-5 text-red-400 shrink-0 mt-0.5" />
          <div>
            <h4 className="font-semibold text-red-300">Comparison Execution Error</h4>
            <p className="text-xs text-red-400 mt-1">{comparisonError}</p>
          </div>
        </div>
      )}

      {/* Reproducibility Verification Cards */}
      {(baselineVerification || comparisonVerification || loadingVerification) && (
        <section aria-label="Reproducibility Verification Cards" className="grid grid-cols-1 md:grid-cols-2 gap-5 mb-8">
          <VerificationCard
            label="Baseline"
            runId={baselineRunId}
            verification={baselineVerification}
            loading={loadingVerification}
          />
          <VerificationCard
            label="Comparison"
            runId={comparisonRunId}
            verification={comparisonVerification}
            loading={loadingVerification}
          />
        </section>
      )}

      {/* Summary Metrics Cards */}
      {comparisonResult && (
        <>
          <section aria-label="Summary Metrics" className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-4 mb-8">
            <div className="p-4 bg-slate-900/60 border border-slate-800 rounded-xl text-center shadow-md">
              <span className="text-xs font-medium text-slate-400 uppercase tracking-wider block">Total Aligned Points</span>
              <span className="text-2xl font-bold text-slate-100 mt-1 block font-mono">{comparisonResult.aligned_point_count}</span>
            </div>
            <div className="p-4 bg-slate-900/60 border border-amber-500/40 rounded-xl text-center shadow-md bg-amber-950/10">
              <span className="text-xs font-medium text-amber-400 uppercase tracking-wider block">Changed Points</span>
              <span className="text-2xl font-bold text-amber-300 mt-1 block font-mono">{comparisonResult.changed_point_count}</span>
            </div>
            <div className="p-4 bg-slate-900/60 border border-slate-800 rounded-xl text-center shadow-md">
              <span className="text-xs font-medium text-slate-400 uppercase tracking-wider block">Unchanged Points</span>
              <span className="text-2xl font-bold text-slate-300 mt-1 block font-mono">{comparisonResult.unchanged_point_count}</span>
            </div>
            <div className="p-4 bg-slate-900/60 border border-slate-800 rounded-xl text-center shadow-md">
              <span className="text-xs font-medium text-slate-400 uppercase tracking-wider block">Baseline Only</span>
              <span className="text-2xl font-bold text-slate-300 mt-1 block font-mono">{comparisonResult.baseline_only_point_count}</span>
            </div>
            <div className="p-4 bg-slate-900/60 border border-slate-800 rounded-xl text-center shadow-md">
              <span className="text-xs font-medium text-slate-400 uppercase tracking-wider block">Comparison Only</span>
              <span className="text-2xl font-bold text-slate-300 mt-1 block font-mono">{comparisonResult.comparison_only_point_count}</span>
            </div>
          </section>

          {/* Export Buttons */}
          <ExportButtons baselineRunId={baselineRunId} comparisonRunId={comparisonRunId} />

          {/* Neutral Status Transition Matrix */}
          <TransitionMatrixCard statusTransitionCounts={comparisonResult.status_transition_counts} />

          {/* Detailed Difference Table */}
          <DifferenceTable
            differences={comparisonResult.differences}
            alignedPointCount={comparisonResult.aligned_point_count}
            changedPointCount={comparisonResult.changed_point_count}
            unchangedPointCount={comparisonResult.unchanged_point_count}
          />
        </>
      )}

      {loadingComparison && (
        <div className="py-16 text-center text-slate-400 animate-pulse">
          <GitCompare className="w-8 h-8 mx-auto mb-3 text-sky-400 animate-spin" />
          <p className="text-sm font-medium">Executing deterministic replay comparison...</p>
        </div>
      )}
    </div>
  );
}
