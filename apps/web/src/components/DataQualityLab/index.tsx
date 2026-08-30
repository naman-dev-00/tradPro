"use client";

import React, { useEffect, useState, useCallback } from "react";
import Link from "next/link";
import {
  fetchDatasetQualitySummaries,
  fetchDatasetQualityReport,
  auditDatasetsBatch,
  getDataQualityExportUrl,
  DatasetQualityListItem,
  DatasetQualityReport,
  DatasetAuditBatchResponse,
} from "@/lib/api";
import { EducationalNotice } from "./EducationalNotice";
import { DatasetQualitySelector } from "./DatasetQualitySelector";
import { QualitySummaryCards } from "./QualitySummaryCards";
import { ProvenanceCard } from "./ProvenanceCard";
import { ChecksumVerificationCard } from "./ChecksumVerificationCard";
import { IssueTable } from "./IssueTable";
import {
  ShieldCheck,
  Home,
  Layers,
  PlayCircle,
  GitCompare,
  Activity,
  Download,
  RefreshCw,
  AlertCircle,
  CheckCircle2,
  AlertTriangle,
  XCircle,
} from "lucide-react";


export function DataQualityLab() {
  const [datasets, setDatasets] = useState<DatasetQualityListItem[]>([]);
  const [selectedDatasetId, setSelectedDatasetId] = useState<string>("");
  const [activeReport, setActiveReport] = useState<DatasetQualityReport | null>(null);

  const [isBatchMode, setIsBatchMode] = useState<boolean>(false);
  const [selectedBatchIds, setSelectedBatchIds] = useState<string[]>([]);
  const [batchResponse, setBatchResponse] = useState<DatasetAuditBatchResponse | null>(null);

  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const loadDatasetList = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const list = await fetchDatasetQualitySummaries();
      setDatasets(list);
      if (list.length > 0) {
        setSelectedDatasetId((prev) => prev || list[0].dataset_id);
        setSelectedBatchIds(list.map((d) => d.dataset_id));
      }
    } catch (err: any) {
      setError(err.message || "Failed to load whitelisted dataset summaries.");
    } finally {
      setLoading(false);
    }
  }, []);

  const loadReport = useCallback(async (id: string) => {
    if (!id) return;
    try {
      setLoading(true);
      setError(null);
      const report = await fetchDatasetQualityReport(id);
      setActiveReport(report);
    } catch (err: any) {
      setError(err.message || `Failed to load quality report for dataset '${id}'.`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadDatasetList();
  }, [loadDatasetList]);

  useEffect(() => {
    if (selectedDatasetId && !isBatchMode) {
      loadReport(selectedDatasetId);
    }
  }, [selectedDatasetId, isBatchMode, loadReport]);

  const handleToggleBatchId = (id: string) => {
    setSelectedBatchIds((prev) => {
      if (prev.includes(id)) {
        if (prev.length === 1) return prev; // keep at least 1
        return prev.filter((item) => item !== id);
      } else {
        if (prev.length >= 20) return prev; // max 20
        return [...prev, id];
      }
    });
  };

  const handleRunBatchAudit = async () => {
    if (selectedBatchIds.length === 0) return;
    try {
      setLoading(true);
      setError(null);
      const res = await auditDatasetsBatch(selectedBatchIds);
      setBatchResponse(res);
    } catch (err: any) {
      setError(err.message || "Failed to run batch quality audit.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 p-4 md:p-8">
      {/* Top Header & Cross-Lab Navigation */}
      <header className="flex flex-col md:flex-row md:items-center justify-between pb-6 mb-6 border-b border-slate-800 gap-4">
        <div>
          <div className="flex items-center space-x-3">
            <div className="p-2 bg-emerald-950/60 border border-emerald-500/40 rounded-lg text-emerald-400">
              <ShieldCheck className="w-6 h-6" aria-hidden="true" />
            </div>
            <div>
              <h1 className="text-xl md:text-2xl font-bold tracking-tight text-white flex items-center space-x-2">
                <span>Synthetic Dataset Quality & Provenance Lab</span>
              </h1>
              <p className="text-xs text-slate-400 mt-0.5">
                Deterministic schema validation, timestamp continuity, OHLCV bounds & checksum verification.
              </p>
            </div>
          </div>
        </div>

        {/* Global Navigation Links */}
        <nav aria-label="Labs Navigation" className="flex flex-wrap items-center gap-2 text-xs">
          <Link
            href="/"
            className="flex items-center space-x-1.5 px-3 py-1.5 rounded-lg bg-slate-900 border border-slate-800 hover:bg-slate-800 text-slate-300 transition-colors focus:outline-none focus:ring-2 focus:ring-sky-500"
          >
            <Home className="w-3.5 h-3.5" aria-hidden="true" />
            <span>Home</span>
          </Link>
          <Link
            href="/indicator-lab"
            className="flex items-center space-x-1.5 px-3 py-1.5 rounded-lg bg-slate-900 border border-slate-800 hover:bg-slate-800 text-slate-300 transition-colors focus:outline-none focus:ring-2 focus:ring-sky-500"
          >
            <Activity className="w-3.5 h-3.5" aria-hidden="true" />
            <span>Indicator Lab</span>
          </Link>
          <Link
            href="/multi-series-lab"
            className="flex items-center space-x-1.5 px-3 py-1.5 rounded-lg bg-slate-900 border border-slate-800 hover:bg-slate-800 text-slate-300 transition-colors focus:outline-none focus:ring-2 focus:ring-sky-500"
          >
            <Layers className="w-3.5 h-3.5" aria-hidden="true" />
            <span>Multi-Series Lab</span>
          </Link>
          <Link
            href="/historical-replay-lab"
            className="flex items-center space-x-1.5 px-3 py-1.5 rounded-lg bg-slate-900 border border-slate-800 hover:bg-slate-800 text-slate-300 transition-colors focus:outline-none focus:ring-2 focus:ring-sky-500"
          >
            <PlayCircle className="w-3.5 h-3.5" aria-hidden="true" />
            <span>Historical Replay</span>
          </Link>
          <Link
            href="/replay-comparison-lab"
            className="flex items-center space-x-1.5 px-3 py-1.5 rounded-lg bg-slate-900 border border-slate-800 hover:bg-slate-800 text-slate-300 transition-colors focus:outline-none focus:ring-2 focus:ring-sky-500"
          >
            <GitCompare className="w-3.5 h-3.5" aria-hidden="true" />
            <span>Replay Comparison</span>
          </Link>
        </nav>
      </header>

      {/* Educational Notice Banner */}
      <EducationalNotice />

      {/* Error Announcement */}
      {error && (
        <div
          role="alert"
          className="mb-6 p-4 rounded-lg bg-rose-950/50 border border-rose-500/50 text-rose-200 flex items-start space-x-3 text-xs"
        >
          <AlertCircle className="w-4 h-4 text-rose-400 mt-0.5 shrink-0" aria-hidden="true" />
          <div>
            <span className="font-semibold">Quality Audit Error:</span> {error}
          </div>
        </div>
      )}

      {/* Mode Switcher Tabs */}
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center space-x-2 bg-slate-900 p-1 rounded-xl border border-slate-800" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={!isBatchMode}
            onClick={() => setIsBatchMode(false)}
            className={`px-4 py-2 rounded-lg text-xs font-semibold transition-all focus:outline-none focus:ring-2 focus:ring-sky-500 ${
              !isBatchMode
                ? "bg-emerald-600 text-white shadow-md shadow-emerald-950/40"
                : "text-slate-400 hover:text-slate-200"
            }`}
          >
            Single Dataset Diagnostics
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={isBatchMode}
            onClick={() => {
              setIsBatchMode(true);
              if (!batchResponse) {
                handleRunBatchAudit();
              }
            }}
            className={`px-4 py-2 rounded-lg text-xs font-semibold transition-all focus:outline-none focus:ring-2 focus:ring-sky-500 ${
              isBatchMode
                ? "bg-emerald-600 text-white shadow-md shadow-emerald-950/40"
                : "text-slate-400 hover:text-slate-200"
            }`}
          >
            Batch Quality Audit (1–20)
          </button>
        </div>

        <button
          type="button"
          onClick={() => (isBatchMode ? handleRunBatchAudit() : loadReport(selectedDatasetId))}
          disabled={loading}
          className="flex items-center space-x-1.5 px-3.5 py-2 rounded-lg bg-slate-900 border border-slate-800 hover:bg-slate-800 text-slate-300 text-xs font-medium focus:outline-none focus:ring-2 focus:ring-sky-500"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin text-sky-400" : ""}`} aria-hidden="true" />
          <span>Refresh Audit</span>
        </button>
      </div>

      {/* Dataset Selector Component */}
      <DatasetQualitySelector
        datasets={datasets}
        selectedDatasetId={selectedDatasetId}
        onSelectDataset={setSelectedDatasetId}
        selectedBatchIds={selectedBatchIds}
        onToggleBatchId={handleToggleBatchId}
        isBatchMode={isBatchMode}
      />

      {/* Single Dataset Mode Content */}
      {!isBatchMode && activeReport && (
        <div aria-live="polite">
          {/* Summary Cards */}
          <QualitySummaryCards report={activeReport} />

          {/* Provenance & Checksum Cards Side-by-Side */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <ProvenanceCard provenance={activeReport.provenance} />
            <ChecksumVerificationCard summary={activeReport.summary} />
          </div>

          {/* Export Button Header */}
          <div className="flex items-center justify-between mb-4">
            <span className="text-xs text-slate-400 font-mono">
              Audit Rules Version: v{activeReport.audit_rules_version}
            </span>

            <a
              href={getDataQualityExportUrl(activeReport.dataset_id)}
              download={`data_quality_${activeReport.dataset_id}.json`}
              className="inline-flex items-center space-x-1.5 px-4 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-semibold shadow-md shadow-emerald-950/30 transition-colors focus:outline-none focus:ring-2 focus:ring-emerald-400"
            >
              <Download className="w-3.5 h-3.5" aria-hidden="true" />
              <span>Export JSON Report</span>
            </a>
          </div>

          {/* Detailed Issues Table */}
          <IssueTable
            issues={activeReport.issues}
            totalIssueCount={activeReport.total_issue_count}
            reportedIssueCount={activeReport.reported_issue_count}
            issuesTruncated={activeReport.issues_truncated}
          />
        </div>
      )}

      {/* Batch Quality Audit Mode Content */}
      {isBatchMode && (
        <div aria-live="polite">
          <div className="flex items-center justify-between mb-4">
            <button
              type="button"
              onClick={handleRunBatchAudit}
              disabled={loading || selectedBatchIds.length === 0}
              className="px-4 py-2 rounded-lg bg-sky-600 hover:bg-sky-500 disabled:opacity-50 text-white text-xs font-semibold shadow-md focus:outline-none focus:ring-2 focus:ring-sky-400"
            >
              Run Batch Audit ({selectedBatchIds.length} datasets)
            </button>
          </div>

          {batchResponse && (
            <div className="space-y-6">
              {/* Status Counters Banner */}
              <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
                <div className="bg-slate-900/70 border border-slate-800 p-4 rounded-xl">
                  <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Total Audited</span>
                  <div className="text-2xl font-bold text-slate-100 mt-1">{batchResponse.total_datasets}</div>
                </div>
                <div className="bg-emerald-950/20 border border-emerald-500/40 p-4 rounded-xl">
                  <span className="text-xs font-semibold text-emerald-400 uppercase tracking-wider flex items-center space-x-1.5">
                    <CheckCircle2 className="w-3.5 h-3.5" aria-hidden="true" />
                    <span>PASS Status</span>
                  </span>
                  <div className="text-2xl font-bold text-emerald-300 mt-1">{batchResponse.status_counts.PASS}</div>
                </div>
                <div className="bg-amber-950/20 border border-amber-500/40 p-4 rounded-xl">
                  <span className="text-xs font-semibold text-amber-400 uppercase tracking-wider flex items-center space-x-1.5">
                    <AlertTriangle className="w-3.5 h-3.5" aria-hidden="true" />
                    <span>WARN Status</span>
                  </span>
                  <div className="text-2xl font-bold text-amber-300 mt-1">{batchResponse.status_counts.WARN}</div>
                </div>
                <div className="bg-rose-950/20 border border-rose-500/40 p-4 rounded-xl">
                  <span className="text-xs font-semibold text-rose-400 uppercase tracking-wider flex items-center space-x-1.5">
                    <XCircle className="w-3.5 h-3.5" aria-hidden="true" />
                    <span>FAIL Status</span>
                  </span>
                  <div className="text-2xl font-bold text-rose-300 mt-1">{batchResponse.status_counts.FAIL}</div>
                </div>
              </div>

              {/* Batch Reports List */}
              <div className="bg-slate-900/70 border border-slate-800 rounded-xl p-5">
                <div className="flex items-center justify-between mb-4">
                  <h3 className="text-sm font-semibold text-slate-200 uppercase tracking-wider">
                    Batch Inspection Results ({batchResponse.reports.length} Datasets)
                  </h3>
                </div>

                <div className="divide-y divide-slate-800">
                  {batchResponse.reports.map((rep) => (
                    <div key={rep.dataset_id} className="py-4 flex flex-col md:flex-row md:items-center justify-between gap-4">
                      <div>
                        <div className="flex items-center space-x-2">
                          <span
                            className={`px-2 py-0.5 rounded text-[10px] font-bold border ${
                              rep.status === "PASS"
                                ? "bg-emerald-950/70 text-emerald-300 border-emerald-500/50"
                                : rep.status === "WARN"
                                ? "bg-amber-950/70 text-amber-300 border-amber-500/50"
                                : "bg-rose-950/70 text-rose-300 border-rose-500/50"
                            }`}
                          >
                            {rep.status}
                          </span>
                          <span className="text-sm font-semibold text-slate-200">{rep.provenance.display_name}</span>
                          <span className="text-xs font-mono text-slate-400">({rep.dataset_id})</span>
                        </div>
                        <div className="flex items-center space-x-3 text-xs text-slate-400 mt-1.5">
                          <span>{rep.summary.total_rows} rows</span>
                          <span>•</span>
                          <span>{rep.summary.completed_rows} completed</span>
                          <span>•</span>
                          <span>{rep.total_issue_count} issue(s)</span>
                          <span>•</span>
                          <span className="font-mono text-[11px] text-sky-400">{rep.provenance.timeframe}</span>
                        </div>
                      </div>

                      <div className="flex items-center space-x-2">
                        <button
                          type="button"
                          onClick={() => {
                            setSelectedDatasetId(rep.dataset_id);
                            setIsBatchMode(false);
                          }}
                          className="px-3 py-1.5 rounded bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-medium border border-slate-700 focus:outline-none focus:ring-2 focus:ring-sky-500"
                        >
                          View Details
                        </button>
                        <a
                          href={getDataQualityExportUrl(rep.dataset_id)}
                          download={`data_quality_${rep.dataset_id}.json`}
                          className="p-1.5 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 focus:outline-none focus:ring-2 focus:ring-sky-500"
                          title="Export JSON"
                          aria-label={`Export JSON report for ${rep.dataset_id}`}
                        >
                          <Download className="w-3.5 h-3.5" aria-hidden="true" />
                        </a>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
