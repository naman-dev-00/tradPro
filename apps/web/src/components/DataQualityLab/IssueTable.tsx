import React, { useState, useId } from "react";
import { DatasetQualityIssue, DatasetIssueSeverity } from "@/lib/api";
import { AlertCircle, AlertTriangle, Info, ChevronRight, ChevronDown, CheckCircle2 } from "lucide-react";

interface IssueTableProps {
  issues: DatasetQualityIssue[];
  totalIssueCount: number;
  reportedIssueCount: number;
  issuesTruncated: boolean;
}

const PAGE_SIZE = 50;

export function IssueTable({
  issues,
  totalIssueCount,
  reportedIssueCount,
  issuesTruncated,
}: IssueTableProps) {
  const [severityFilter, setSeverityFilter] = useState<"ALL" | DatasetIssueSeverity>("ALL");
  const [currentPage, setCurrentPage] = useState(1);
  const [expandedRows, setExpandedRows] = useState<Record<number, boolean>>({});
  const baseId = useId();

  const toggleRow = (idx: number) => {
    setExpandedRows((prev) => ({ ...prev, [idx]: !prev[idx] }));
  };

  const filteredIssues = issues.filter((iss) => {
    if (severityFilter === "ALL") return true;
    return iss.severity === severityFilter;
  });

  const totalPages = Math.max(1, Math.ceil(filteredIssues.length / PAGE_SIZE));
  const pageIndex = Math.min(currentPage, totalPages);
  const displayedIssues = filteredIssues.slice((pageIndex - 1) * PAGE_SIZE, pageIndex * PAGE_SIZE);

  const getSeverityBadge = (severity: DatasetIssueSeverity) => {
    switch (severity) {
      case "ERROR":
        return (
          <span className="inline-flex items-center space-x-1 px-2 py-0.5 rounded text-[10px] font-bold bg-rose-950/70 border border-rose-500/50 text-rose-300">
            <AlertCircle className="w-3 h-3" aria-hidden="true" />
            <span>ERROR</span>
          </span>
        );
      case "WARNING":
        return (
          <span className="inline-flex items-center space-x-1 px-2 py-0.5 rounded text-[10px] font-bold bg-amber-950/70 border border-amber-500/50 text-amber-300">
            <AlertTriangle className="w-3 h-3" aria-hidden="true" />
            <span>WARNING</span>
          </span>
        );
      case "INFO":
        return (
          <span className="inline-flex items-center space-x-1 px-2 py-0.5 rounded text-[10px] font-bold bg-sky-950/70 border border-sky-500/50 text-sky-300">
            <Info className="w-3 h-3" aria-hidden="true" />
            <span>INFO</span>
          </span>
        );
    }
  };

  return (
    <div className="bg-slate-900/70 border border-slate-800 rounded-xl p-5 mb-6">
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 mb-4">
        <div>
          <h2 className="text-sm font-semibold text-slate-200 uppercase tracking-wider">
            Quality & Schema Audit Findings
          </h2>
          <div className="text-xs text-slate-400 mt-0.5">
            Showing {filteredIssues.length} of {totalIssueCount} total detected issues ({reportedIssueCount} in report)
            {issuesTruncated && " — results truncated to top 1,000"}
          </div>
        </div>


        {/* Severity Filter Tabs */}
        <div className="flex items-center space-x-1 bg-slate-800/80 p-1 rounded-lg border border-slate-700/60" role="tablist">
          {(["ALL", "ERROR", "WARNING", "INFO"] as const).map((tab) => {
            const count = tab === "ALL" ? issues.length : issues.filter((i) => i.severity === tab).length;
            const isSelected = severityFilter === tab;

            return (
              <button
                key={tab}
                type="button"
                role="tab"
                aria-selected={isSelected}
                onClick={() => {
                  setSeverityFilter(tab);
                  setCurrentPage(1);
                }}
                className={`px-3 py-1 text-xs rounded font-medium transition-colors focus:outline-none focus:ring-2 focus:ring-sky-500 ${
                  isSelected
                    ? "bg-slate-700 text-white shadow-sm font-semibold"
                    : "text-slate-400 hover:text-slate-200"
                }`}
              >
                {tab} ({count})
              </button>
            );
          })}
        </div>
      </div>

      {displayedIssues.length === 0 ? (
        <div className="p-8 text-center bg-slate-950/30 rounded-lg border border-slate-800/60 text-slate-400">
          <CheckCircle2 className="w-8 h-8 text-emerald-400 mx-auto mb-2" aria-hidden="true" />
          <div className="text-sm font-semibold text-slate-200">No Quality Issues Detected</div>
          <div className="text-xs text-slate-400 mt-1">
            {severityFilter === "ALL"
              ? "All audited schema, timestamp, continuity, and integrity checks passed."
              : `No issues matching severity filter '${severityFilter}'.`}
          </div>
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-slate-800">
          <table className="w-full text-left text-xs border-collapse">
            <thead className="bg-slate-800/80 text-slate-300 font-semibold uppercase tracking-wider text-[11px] border-b border-slate-700/80">
              <tr>
                <th className="py-2.5 px-3 w-8" scope="col"><span className="sr-only">Expand</span></th>
                <th className="py-2.5 px-3 w-28" scope="col">Severity</th>
                <th className="py-2.5 px-3 w-56" scope="col">Issue Code</th>
                <th className="py-2.5 px-3" scope="col">Message</th>
                <th className="py-2.5 px-3 w-20" scope="col">Row #</th>
                <th className="py-2.5 px-3 w-28" scope="col">Field</th>
                <th className="py-2.5 px-3 w-40" scope="col">Timestamp</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/60 font-mono text-[11px]">
              {displayedIssues.map((iss, idx) => {
                const isExpanded = !!expandedRows[idx];
                const detailId = `${baseId}-issue-detail-${idx}`;

                return (
                  <React.Fragment key={idx}>
                    <tr className="hover:bg-slate-800/30 transition-colors">
                      <td className="py-2 px-2 text-center">
                        <button
                          type="button"
                          onClick={() => toggleRow(idx)}
                          aria-expanded={isExpanded}
                          aria-controls={detailId}
                          aria-label={`Toggle details for row ${idx + 1}`}
                          className="p-1 rounded text-slate-400 hover:text-slate-200 focus:outline-none focus:ring-1 focus:ring-sky-500"
                        >
                          {isExpanded ? (
                            <ChevronDown className="w-3.5 h-3.5" aria-hidden="true" />
                          ) : (
                            <ChevronRight className="w-3.5 h-3.5" aria-hidden="true" />
                          )}
                        </button>
                      </td>
                      <td className="py-2 px-3">{getSeverityBadge(iss.severity)}</td>
                      <td className="py-2 px-3 font-semibold text-slate-200 text-[11px]">{iss.code}</td>
                      <td className="py-2 px-3 font-sans text-slate-300 max-w-md truncate" title={iss.message}>
                        {iss.message}
                      </td>
                      <td className="py-2 px-3 text-slate-400">{iss.row_number ?? "—"}</td>
                      <td className="py-2 px-3 text-sky-300">{iss.field ?? "—"}</td>
                      <td className="py-2 px-3 text-slate-400 truncate">
                        {iss.timestamp ? new Date(iss.timestamp).toISOString() : "—"}
                      </td>
                    </tr>

                    {isExpanded && (
                      <tr id={detailId} className="bg-slate-950/40 font-sans text-xs">
                        <td colSpan={7} className="p-4 border-t border-slate-800/80">
                          <div className="space-y-2 bg-slate-900/80 p-3 rounded border border-slate-800">
                            <div>
                              <span className="font-semibold text-slate-300 text-xs">Detailed Finding:</span>
                              <p className="text-slate-300 text-xs mt-0.5 leading-relaxed">{iss.message}</p>
                            </div>
                            {(iss.expected || iss.actual) && (
                              <div className="grid grid-cols-1 md:grid-cols-2 gap-3 pt-2 border-t border-slate-800 font-mono text-[11px]">
                                {iss.expected && (
                                  <div>
                                    <span className="text-emerald-400 font-semibold block text-[10px] uppercase">
                                      Expected:
                                    </span>
                                    <span className="text-slate-300 break-all">{iss.expected}</span>
                                  </div>
                                )}
                                {iss.actual && (
                                  <div>
                                    <span className="text-rose-400 font-semibold block text-[10px] uppercase">
                                      Actual:
                                    </span>
                                    <span className="text-slate-300 break-all">{iss.actual}</span>
                                  </div>
                                )}
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
      )}

      {/* Pagination Controls */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between mt-4 pt-3 border-t border-slate-800 text-xs text-slate-400">
          <div>
            Page <span className="font-semibold text-slate-200">{pageIndex}</span> of{" "}
            <span className="font-semibold text-slate-200">{totalPages}</span>
          </div>
          <div className="flex items-center space-x-2">
            <button
              type="button"
              disabled={pageIndex === 1}
              onClick={() => setCurrentPage((p) => Math.max(1, p - 1))}
              className="px-3 py-1 rounded bg-slate-800 border border-slate-700 text-slate-300 disabled:opacity-40 disabled:cursor-not-allowed hover:bg-slate-700 focus:outline-none focus:ring-2 focus:ring-sky-500"
            >
              Previous
            </button>
            <button
              type="button"
              disabled={pageIndex === totalPages}
              onClick={() => setCurrentPage((p) => Math.min(totalPages, p + 1))}
              className="px-3 py-1 rounded bg-slate-800 border border-slate-700 text-slate-300 disabled:opacity-40 disabled:cursor-not-allowed hover:bg-slate-700 focus:outline-none focus:ring-2 focus:ring-sky-500"
            >
              Next
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
