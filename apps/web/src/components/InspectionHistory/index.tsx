"use client";

import React, { useState, useEffect } from "react";
import Link from "next/link";
import { useSearchParams, useRouter } from "next/navigation";
import {
  listInspectionRuns,
  PaginatedInspectionRunList,
} from "@/lib/api";
import {
  Filter,
  CheckCircle2,
  XCircle,
  AlertTriangle,
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  Layers,
  PlayCircle,
} from "lucide-react";
import { AuthHeaderBadge } from "@/components/AuthHeaderBadge";

export function InspectionHistory() {
  const searchParams = useSearchParams();
  const router = useRouter();

  const [page, setPage] = useState<number>(parseInt(searchParams.get("page") || "1", 10));
  const [statusFilter, setStatusFilter] = useState<string>(searchParams.get("status") || "");
  const [runTypeFilter, setRunTypeFilter] = useState<string>(searchParams.get("run_type") || "");

  const [data, setData] = useState<PaginatedInspectionRunList | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function fetchData() {
      try {
        setLoading(true);
        setError(null);
        const res = await listInspectionRuns({
          page,
          page_size: 15,
          status: statusFilter || undefined,
          run_type: runTypeFilter || undefined,
        });
        setData(res);
      } catch (err: any) {
        setError(err.message || "Failed to load inspection history.");
      } finally {
        setLoading(false);
      }
    }
    fetchData();
  }, [page, statusFilter, runTypeFilter]);

  const updateUrl = (newPage: number, newStatus: string, newRunType: string) => {
    const params = new URLSearchParams();
    if (newPage > 1) params.set("page", newPage.toString());
    if (newStatus) params.set("status", newStatus);
    if (newRunType) params.set("run_type", newRunType);
    router.push(`/inspection-history?${params.toString()}`);
  };

  const handlePageChange = (newPage: number) => {
    setPage(newPage);
    updateUrl(newPage, statusFilter, runTypeFilter);
  };

  const handleStatusFilterChange = (newStatus: string) => {
    setStatusFilter(newStatus);
    setPage(1);
    updateUrl(1, newStatus, runTypeFilter);
  };

  const handleRunTypeFilterChange = (newRunType: string) => {
    setRunTypeFilter(newRunType);
    setPage(1);
    updateUrl(1, statusFilter, newRunType);
  };

  return (
    <div className="mx-auto max-w-7xl px-4 py-8 text-slate-100 sm:px-6 lg:px-8">
      {/* Header */}
      <div className="mb-8 flex flex-col items-start justify-between gap-4 md:flex-row md:items-center">
        <div>
          <h1 className="text-3xl font-extrabold tracking-tight text-white">
            Inspection History
          </h1>
          <p className="mt-1 text-sm text-slate-400">
            Persistent educational inspection runs and historical replay audit logs
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Link
            href="/historical-replay-lab"
            className="inline-flex items-center space-x-2 rounded-lg border border-slate-700 bg-slate-800/80 px-3 py-1.5 text-xs font-semibold text-slate-200 transition-colors hover:bg-slate-700"
          >
            <PlayCircle className="h-4 w-4 text-cyan-400" />
            <span>Historical Replay Lab</span>
          </Link>
          <Link
            href="/multi-series-lab"
            className="inline-flex items-center space-x-2 rounded-lg border border-slate-700 bg-slate-800/80 px-3 py-1.5 text-xs font-semibold text-slate-200 transition-colors hover:bg-slate-700"
          >
            <Layers className="h-4 w-4 text-emerald-400" />
            <span>Multi-Series Lab</span>
          </Link>
          <AuthHeaderBadge />
        </div>
      </div>

      {/* Filter Toolbar */}
      <div className="mb-6 rounded-2xl border border-slate-800 bg-slate-900/60 p-4 backdrop-blur-xl shadow-lg">
        <div className="flex flex-wrap items-center gap-4">
          <div className="flex items-center space-x-2">
            <Filter className="h-4 w-4 text-slate-400" />
            <span className="text-xs font-bold uppercase tracking-wider text-slate-400">Filters</span>
          </div>

          {/* Status Filter */}
          <select
            value={statusFilter}
            onChange={(e) => handleStatusFilterChange(e.target.value)}
            className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-xs text-white focus:border-cyan-500 focus:outline-none"
          >
            <option value="">All Statuses</option>
            <option value="COMPLETED">COMPLETED</option>
            <option value="FAILED">FAILED</option>
          </select>

          {/* Run Type Filter */}
          <select
            value={runTypeFilter}
            onChange={(e) => handleRunTypeFilterChange(e.target.value)}
            className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-xs text-white focus:border-cyan-500 focus:outline-none"
          >
            <option value="">All Run Types</option>
            <option value="HISTORICAL_REPLAY">HISTORICAL_REPLAY</option>
            <option value="MULTI_SERIES">MULTI_SERIES</option>
            <option value="SINGLE_SERIES">SINGLE_SERIES</option>
          </select>
        </div>
      </div>

      {error && (
        <div role="alert" className="mb-6 rounded-xl border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-200">
          {error}
        </div>
      )}

      {/* Runs Table */}
      <div className="overflow-hidden rounded-2xl border border-slate-800 bg-slate-900/60 backdrop-blur-xl shadow-xl">
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs text-slate-300">
            <thead className="border-b border-slate-800/80 bg-slate-950/80 font-semibold uppercase tracking-wider text-slate-400">
              <tr>
                <th className="px-4 py-3.5">Run ID</th>
                <th className="px-4 py-3.5">Type</th>
                <th className="px-4 py-3.5">Datasets</th>
                <th className="px-4 py-3.5">Status</th>
                <th className="px-4 py-3.5">Reproducibility</th>
                <th className="px-4 py-3.5">Created At</th>
                <th className="px-4 py-3.5 text-right">Details</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/60">
              {loading ? (
                <tr>
                  <td colSpan={7} className="py-12 text-center text-slate-500">
                    Loading inspection history...
                  </td>
                </tr>
              ) : !data || data.items.length === 0 ? (
                <tr>
                  <td colSpan={7} className="py-12 text-center text-slate-500">
                    No inspection runs found.
                  </td>
                </tr>
              ) : (
                data.items.map((item) => (
                  <tr key={item.id} className="transition-colors hover:bg-slate-800/40">
                    <td className="px-4 py-3 font-mono text-slate-200">{item.id.slice(0, 8)}...</td>
                    <td className="px-4 py-3 font-medium text-slate-300">{item.run_type}</td>
                    <td className="px-4 py-3">
                      <span className="font-semibold text-slate-200">{item.reference_dataset_id}</span>
                      <span className="ml-1 text-slate-500">({item.subject_dataset_ids.length} subjects)</span>
                    </td>
                    <td className="px-4 py-3">
                      <span
                        className={`inline-flex items-center space-x-1 rounded px-2 py-0.5 font-bold text-[10px] ${
                          item.status === "COMPLETED"
                            ? "bg-emerald-500/20 text-emerald-300"
                            : "bg-rose-500/20 text-rose-300"
                        }`}
                      >
                        {item.status === "COMPLETED" ? <CheckCircle2 className="h-3 w-3" /> : <XCircle className="h-3 w-3" />}
                        <span>{item.status}</span>
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      {item.is_exact_match ? (
                        <span className="text-emerald-400 font-medium">Exact Match</span>
                      ) : (
                        <span className="inline-flex items-center space-x-1 text-amber-400 font-medium">
                          <AlertTriangle className="h-3 w-3" />
                          <span>Mismatch</span>
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-slate-400">
                      {new Date(item.created_at).toLocaleString()}
                    </td>
                    <td className="px-4 py-3 text-right space-x-3">
                      {item.status === "COMPLETED" && (
                        <Link
                          href={`/replay-comparison-lab?baseline=${item.id}`}
                          className="inline-flex items-center space-x-1 text-sky-400 hover:text-sky-300 font-medium"
                        >
                          <span>Compare</span>
                        </Link>
                      )}
                      <Link
                        href={`/historical-replay-lab?run_id=${item.id}`}
                        className="inline-flex items-center space-x-1 text-cyan-400 hover:text-cyan-300 font-medium"
                      >
                        <span>Open</span>
                        <ExternalLink className="h-3 w-3" />
                      </Link>
                    </td>

                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        {/* Pagination Controls */}
        {data && data.total_pages > 1 && (
          <div className="flex items-center justify-between border-t border-slate-800/80 bg-slate-950/80 px-4 py-3 text-xs text-slate-400">
            <span>
              Page {data.page} of {data.total_pages} ({data.total} total runs)
            </span>
            <div className="flex items-center space-x-2">
              <button
                disabled={data.page <= 1}
                onClick={() => handlePageChange(data.page - 1)}
                className="inline-flex items-center rounded-lg border border-slate-700 bg-slate-900 px-2.5 py-1 text-slate-300 disabled:opacity-40"
              >
                <ChevronLeft className="h-4 w-4" />
                <span>Prev</span>
              </button>
              <button
                disabled={data.page >= data.total_pages}
                onClick={() => handlePageChange(data.page + 1)}
                className="inline-flex items-center rounded-lg border border-slate-700 bg-slate-900 px-2.5 py-1 text-slate-300 disabled:opacity-40"
              >
                <span>Next</span>
                <ChevronRight className="h-4 w-4" />
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
