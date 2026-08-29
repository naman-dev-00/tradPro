"use client";

import React, { useState } from "react";
import { IndicatorResultOutput } from "../../lib/api";
import { Table, CheckCircle, Clock, ChevronLeft, ChevronRight } from "lucide-react";

interface ResultsTableProps {
  results: IndicatorResultOutput[];
  indicatorName: string;
  params: Record<string, any>;
}

export function ResultsTable({ results, indicatorName }: ResultsTableProps) {
  const [currentPage, setCurrentPage] = useState(1);
  const pageSize = 10;

  if (!results || results.length === 0) return null;

  const totalPages = Math.ceil(results.length / pageSize);
  const startIndex = (currentPage - 1) * pageSize;
  const paginatedResults = results.slice(startIndex, startIndex + pageSize);

  const formatValue = (val: number | Record<string, number | null> | null): string => {
    if (val === null || val === undefined) return "Unavailable (Warm-up)";
    if (typeof val === "number") return val.toFixed(4);
    if (typeof val === "object") {
      const parts = Object.entries(val).map(([k, v]) => {
        const strVal = v === null || v === undefined ? "N/A" : Number(v).toFixed(4);
        return `${k.toUpperCase()}: ${strVal}`;
      });
      return parts.join(" | ");
    }
    return String(val);
  };

  return (
    <div className="bg-slate-950 border border-slate-900 rounded-xl p-4 text-slate-100 font-sans space-y-3">
      <div className="flex justify-between items-center border-b border-slate-900 pb-2">
        <h3 className="font-bold text-xs text-indigo-400 uppercase tracking-wider flex items-center gap-1.5">
          <Table size={14} />
          Timestamp-Aligned Results Table ({indicatorName})
        </h3>
        <div className="text-[10px] text-slate-400 font-mono">
          Total Candles: {results.length}
        </div>
      </div>

      {/* Table Container */}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs font-mono border-collapse">
          <thead>
            <tr className="border-b border-slate-800 text-slate-400 bg-slate-900/60">
              <th className="py-2.5 px-3 font-semibold">Timestamp (UTC)</th>
              <th className="py-2.5 px-3 font-semibold">Indicator</th>
              <th className="py-2.5 px-3 font-semibold">Calculated Value / Components</th>
              <th className="py-2.5 px-3 font-semibold">Status</th>
              <th className="py-2.5 px-3 font-semibold">Warmup Remaining</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-900">
            {paginatedResults.map((r, idx) => (
              <tr key={idx} className="hover:bg-slate-900/40 text-slate-300">
                <td className="py-2.5 px-3 text-slate-400">
                  {new Date(r.timestamp).toISOString()}
                </td>
                <td className="py-2.5 px-3 font-bold text-indigo-300">
                  {r.indicator}
                </td>
                <td className="py-2.5 px-3 font-medium text-slate-100">
                  {formatValue(r.value)}
                </td>
                <td className="py-2.5 px-3">
                  {r.available ? (
                    <span className="bg-green-950 text-green-400 border border-green-900 text-[10px] px-2 py-0.5 rounded-full font-bold inline-flex items-center gap-1">
                      <CheckCircle size={10} />
                      Available
                    </span>
                  ) : (
                    <span className="bg-amber-950/60 text-amber-400 border border-amber-900/60 text-[10px] px-2 py-0.5 rounded-full font-bold inline-flex items-center gap-1">
                      <Clock size={10} />
                      Warm-up
                    </span>
                  )}
                </td>
                <td className="py-2.5 px-3 text-slate-400 font-mono">
                  {r.warmup_remaining > 0 ? `${r.warmup_remaining} candles` : "0 (Ready)"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Pagination Footer */}
      {totalPages > 1 && (
        <div className="flex justify-between items-center pt-2 border-t border-slate-900/80 text-xs">
          <span className="text-slate-400">
            Page {currentPage} of {totalPages}
          </span>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setCurrentPage((p) => Math.max(1, p - 1))}
              disabled={currentPage === 1}
              className="p-1 rounded bg-slate-900 hover:bg-slate-800 text-slate-300 disabled:opacity-40 disabled:cursor-not-allowed transition"
            >
              <ChevronLeft size={16} />
            </button>
            <button
              onClick={() => setCurrentPage((p) => Math.min(totalPages, p + 1))}
              disabled={currentPage === totalPages}
              className="p-1 rounded bg-slate-900 hover:bg-slate-800 text-slate-300 disabled:opacity-40 disabled:cursor-not-allowed transition"
            >
              <ChevronRight size={16} />
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
