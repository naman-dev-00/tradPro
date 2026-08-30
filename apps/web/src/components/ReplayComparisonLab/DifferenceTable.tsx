"use client";

import React, { useState, useMemo } from "react";
import { ChevronDown, ChevronUp, Filter, ArrowLeft, ArrowRight } from "lucide-react";
import { ReplayStatusDifference } from "@/lib/api";

interface DifferenceTableProps {
  differences: ReplayStatusDifference[];
  alignedPointCount: number;
  changedPointCount: number;
  unchangedPointCount: number;
}

const PAGE_SIZE = 50;

const STATUS_BADGE_STYLE: Record<string, string> = {
  TRUE: "bg-emerald-950/80 text-emerald-300 border-emerald-600/50",
  FALSE: "bg-rose-950/80 text-rose-300 border-rose-600/50",
  UNAVAILABLE: "bg-amber-950/80 text-amber-300 border-amber-600/50",
  INVALID: "bg-red-950/80 text-red-300 border-red-600/50",
  ABSENT: "bg-slate-800/80 text-slate-400 border-slate-700/50",
};

export const DifferenceTable: React.FC<DifferenceTableProps> = ({
  differences,
}) => {
  const [filterStatus, setFilterStatus] = useState<string>("ALL");
  const [filterChanged, setFilterChanged] = useState<string>("ALL");
  const [expandedRowIndex, setExpandedRowIndex] = useState<number | null>(null);
  const [currentPage, setCurrentPage] = useState<number>(1);

  const filteredDifferences = useMemo(() => {
    return differences.filter((diff) => {
      if (filterChanged === "CHANGED_ONLY" && !diff.changed) return false;
      if (filterChanged === "UNCHANGED_ONLY" && diff.changed) return false;

      if (filterStatus !== "ALL") {
        const bStat = diff.baseline_present ? diff.baseline_status : "ABSENT";
        const cStat = diff.comparison_present ? diff.comparison_status : "ABSENT";
        if (bStat !== filterStatus && cStat !== filterStatus) return false;
      }

      return true;
    });
  }, [differences, filterStatus, filterChanged]);

  const totalPages = Math.ceil(filteredDifferences.length / PAGE_SIZE) || 1;
  const paginatedDifferences = useMemo(() => {
    const start = (currentPage - 1) * PAGE_SIZE;
    return filteredDifferences.slice(start, start + PAGE_SIZE);
  }, [filteredDifferences, currentPage]);

  const toggleRow = (idx: number) => {
    setExpandedRowIndex(expandedRowIndex === idx ? null : idx);
  };

  return (
    <div className="p-5 bg-slate-900/60 border border-slate-800 rounded-xl shadow-lg">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-4 pb-4 border-b border-slate-800">
        <div>
          <h3 className="font-semibold text-slate-100 text-base">
            Detailed Inspection Differences
          </h3>
          <p className="text-xs text-slate-400 mt-0.5">
            Showing {filteredDifferences.length} of {differences.length} evaluated point(s) in deterministic order.
          </p>
        </div>

        {/* Filters */}
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex items-center space-x-2">
            <Filter className="w-3.5 h-3.5 text-slate-400" />
            <span className="text-xs text-slate-400 font-medium">Filter State:</span>
            <select
              value={filterChanged}
              onChange={(e) => {
                setFilterChanged(e.target.value);
                setCurrentPage(1);
              }}
              aria-label="Filter by changed state"
              className="bg-slate-950 border border-slate-700 text-slate-200 text-xs rounded-lg px-2.5 py-1.5 focus:outline-none focus:ring-1 focus:ring-sky-500"
            >
              <option value="ALL">All Points</option>
              <option value="CHANGED_ONLY">Changed Only</option>
              <option value="UNCHANGED_ONLY">Unchanged Only</option>
            </select>
          </div>

          <div className="flex items-center space-x-2">
            <span className="text-xs text-slate-400 font-medium">Status:</span>
            <select
              value={filterStatus}
              onChange={(e) => {
                setFilterStatus(e.target.value);
                setCurrentPage(1);
              }}
              aria-label="Filter by status"
              className="bg-slate-950 border border-slate-700 text-slate-200 text-xs rounded-lg px-2.5 py-1.5 focus:outline-none focus:ring-1 focus:ring-sky-500"
            >
              <option value="ALL">All Statuses</option>
              <option value="TRUE">TRUE</option>
              <option value="FALSE">FALSE</option>
              <option value="UNAVAILABLE">UNAVAILABLE</option>
              <option value="INVALID">INVALID</option>
              <option value="ABSENT">ABSENT</option>
            </select>
          </div>
        </div>
      </div>

      {filteredDifferences.length === 0 ? (
        <div className="py-12 text-center text-slate-400 text-sm">
          No replay point differences match the selected filter criteria.
        </div>
      ) : (
        <>
          <div className="overflow-x-auto">
            <table className="w-full text-xs text-left border-collapse min-w-[700px]">
              <thead>
                <tr className="border-b border-slate-800 text-slate-400 font-semibold bg-slate-950/40">
                  <th className="py-3 px-3">UTC Timestamp</th>
                  <th className="py-3 px-3">Dataset ID</th>
                  <th className="py-3 px-3 text-center">Baseline Status</th>
                  <th className="py-3 px-3 text-center">Comparison Status</th>
                  <th className="py-3 px-3 text-center">Difference</th>
                  <th className="py-3 px-3">Explanation</th>
                  <th className="py-3 px-3 text-right">Details</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/60 font-mono">
                {paginatedDifferences.map((diff, idx) => {
                  const globalIdx = (currentPage - 1) * PAGE_SIZE + idx;
                  const isExpanded = expandedRowIndex === globalIdx;

                  const bStatText = diff.baseline_present ? (diff.baseline_status || "UNKNOWN") : "ABSENT";
                  const cStatText = diff.comparison_present ? (diff.comparison_status || "UNKNOWN") : "ABSENT";

                  const hasCondDiffs =
                    diff.newly_true_condition_ids.length > 0 ||
                    diff.no_longer_true_condition_ids.length > 0 ||
                    diff.newly_false_condition_ids.length > 0 ||
                    diff.no_longer_false_condition_ids.length > 0 ||
                    diff.newly_unavailable_condition_ids.length > 0 ||
                    diff.newly_invalid_condition_ids.length > 0;

                  return (
                    <React.Fragment key={`${diff.timestamp}-${diff.dataset_id}-${globalIdx}`}>
                      <tr className={`hover:bg-slate-800/40 transition-colors ${diff.changed ? "bg-amber-950/10" : ""}`}>
                        <td className="py-2.5 px-3 text-slate-300 whitespace-nowrap">
                          {diff.timestamp}
                        </td>
                        <td className="py-2.5 px-3 text-slate-200 font-semibold">
                          {diff.dataset_id}
                        </td>
                        <td className="py-2.5 px-3 text-center whitespace-nowrap">
                          <span
                            className={`inline-block px-2 py-0.5 rounded border text-[11px] font-medium ${
                              STATUS_BADGE_STYLE[bStatText] || "bg-slate-800 text-slate-300"
                            }`}
                          >
                            {bStatText}
                          </span>
                        </td>
                        <td className="py-2.5 px-3 text-center whitespace-nowrap">
                          <span
                            className={`inline-block px-2 py-0.5 rounded border text-[11px] font-medium ${
                              STATUS_BADGE_STYLE[cStatText] || "bg-slate-800 text-slate-300"
                            }`}
                          >
                            {cStatText}
                          </span>
                        </td>
                        <td className="py-2.5 px-3 text-center whitespace-nowrap">
                          {diff.changed ? (
                            <span className="px-2 py-0.5 rounded-full text-[10px] font-semibold bg-amber-950 text-amber-300 border border-amber-600/50">
                              CHANGED
                            </span>
                          ) : (
                            <span className="px-2 py-0.5 rounded-full text-[10px] text-slate-400 bg-slate-800">
                              UNCHANGED
                            </span>
                          )}
                        </td>
                        <td className="py-2.5 px-3 text-slate-300 font-sans text-xs max-w-xs truncate" title={diff.explanation}>
                          {diff.explanation}
                        </td>
                        <td className="py-2.5 px-3 text-right">
                          {hasCondDiffs && (
                            <button
                              type="button"
                              onClick={() => toggleRow(globalIdx)}
                              aria-expanded={isExpanded}
                              aria-controls={`diff-row-details-${globalIdx}`}
                              className="p-1 text-slate-400 hover:text-slate-100 focus:outline-none focus:ring-1 focus:ring-sky-500 rounded"
                            >
                              {isExpanded ? (
                                <ChevronUp className="w-4 h-4" />
                              ) : (
                                <ChevronDown className="w-4 h-4" />
                              )}
                            </button>
                          )}
                        </td>
                      </tr>

                      {/* Expandable Details Row */}
                      {isExpanded && hasCondDiffs && (
                        <tr id={`diff-row-details-${globalIdx}`} className="bg-slate-950/80 border-b border-slate-800 font-sans">
                          <td colSpan={7} className="p-4">
                            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3 text-xs">
                              {diff.newly_true_condition_ids.length > 0 && (
                                <div className="p-2.5 rounded bg-emerald-950/30 border border-emerald-800/40">
                                  <span className="font-semibold text-emerald-400 block mb-1">Newly True Conditions</span>
                                  <span className="font-mono text-emerald-300">{diff.newly_true_condition_ids.join(", ")}</span>
                                </div>
                              )}
                              {diff.no_longer_true_condition_ids.length > 0 && (
                                <div className="p-2.5 rounded bg-slate-900 border border-slate-800">
                                  <span className="font-semibold text-slate-400 block mb-1">No Longer True Conditions</span>
                                  <span className="font-mono text-slate-300">{diff.no_longer_true_condition_ids.join(", ")}</span>
                                </div>
                              )}
                              {diff.newly_false_condition_ids.length > 0 && (
                                <div className="p-2.5 rounded bg-rose-950/30 border border-rose-800/40">
                                  <span className="font-semibold text-rose-400 block mb-1">Newly False Conditions</span>
                                  <span className="font-mono text-rose-300">{diff.newly_false_condition_ids.join(", ")}</span>
                                </div>
                              )}
                              {diff.no_longer_false_condition_ids.length > 0 && (
                                <div className="p-2.5 rounded bg-slate-900 border border-slate-800">
                                  <span className="font-semibold text-slate-400 block mb-1">No Longer False Conditions</span>
                                  <span className="font-mono text-slate-300">{diff.no_longer_false_condition_ids.join(", ")}</span>
                                </div>
                              )}
                              {diff.newly_unavailable_condition_ids.length > 0 && (
                                <div className="p-2.5 rounded bg-amber-950/30 border border-amber-800/40">
                                  <span className="font-semibold text-amber-400 block mb-1">Newly Unavailable Conditions</span>
                                  <span className="font-mono text-amber-300">{diff.newly_unavailable_condition_ids.join(", ")}</span>
                                </div>
                              )}
                              {diff.newly_invalid_condition_ids.length > 0 && (
                                <div className="p-2.5 rounded bg-red-950/30 border border-red-800/40">
                                  <span className="font-semibold text-red-400 block mb-1">Newly Invalid Conditions</span>
                                  <span className="font-mono text-red-300">{diff.newly_invalid_condition_ids.join(", ")}</span>
                                </div>
                              )}
                            </div>
                          </td>
                        </tr>
                      )}
                    </React.Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>

          {/* Pagination Controls */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between mt-4 pt-3 border-t border-slate-800 text-xs text-slate-400">
              <span>
                Page {currentPage} of {totalPages}
              </span>
              <div className="flex items-center space-x-2">
                <button
                  type="button"
                  disabled={currentPage === 1}
                  onClick={() => setCurrentPage(currentPage - 1)}
                  className="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 disabled:opacity-40 disabled:cursor-not-allowed text-slate-200 rounded flex items-center space-x-1"
                >
                  <ArrowLeft className="w-3.5 h-3.5" />
                  <span>Previous</span>
                </button>
                <button
                  type="button"
                  disabled={currentPage === totalPages}
                  onClick={() => setCurrentPage(currentPage + 1)}
                  className="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 disabled:opacity-40 disabled:cursor-not-allowed text-slate-200 rounded flex items-center space-x-1"
                >
                  <span>Next</span>
                  <ArrowRight className="w-3.5 h-3.5" />
                </button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
};
