"use client";

import React from "react";
import { Download, FileJson, FileSpreadsheet } from "lucide-react";
import { getExportUrl } from "@/lib/api";

interface ExportButtonsProps {
  baselineRunId?: string | null;
  comparisonRunId?: string | null;
}

export const ExportButtons: React.FC<ExportButtonsProps> = ({
  baselineRunId,
  comparisonRunId,
}) => {
  const handleExport = (runId: string, format: "json" | "csv") => {
    const url = getExportUrl(runId, format);
    window.open(url, "_blank");
  };

  return (
    <div className="p-4 bg-slate-900/60 border border-slate-800 rounded-xl shadow-lg mb-8 min-w-0">
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 min-w-0">
        <div className="min-w-0">
          <h4 className="font-semibold text-slate-100 text-sm flex items-center space-x-2 truncate">
            <Download className="w-4 h-4 text-sky-400 shrink-0" />
            <span>Educational Inspection Exports</span>
          </h4>
          <p className="text-xs text-slate-400 mt-0.5">
            Download deterministic educational inspection metadata and point evaluation results.
          </p>
        </div>

        <div className="flex flex-wrap gap-2 min-w-0">
          {baselineRunId && (
            <div className="flex flex-wrap gap-2 min-w-0">
              <button
                type="button"
                onClick={() => handleExport(baselineRunId, "json")}
                className="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 rounded-lg text-xs font-medium flex items-center space-x-1.5 transition-colors focus:outline-none focus:ring-1 focus:ring-sky-500 shrink-0"
              >
                <FileJson className="w-3.5 h-3.5 text-sky-400 shrink-0" />
                <span>Baseline JSON</span>
              </button>
              <button
                type="button"
                onClick={() => handleExport(baselineRunId, "csv")}
                className="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 rounded-lg text-xs font-medium flex items-center space-x-1.5 transition-colors focus:outline-none focus:ring-1 focus:ring-sky-500 shrink-0"
              >
                <FileSpreadsheet className="w-3.5 h-3.5 text-emerald-400 shrink-0" />
                <span>Baseline CSV</span>
              </button>
            </div>
          )}

          {comparisonRunId && (
            <div className="flex flex-wrap gap-2 min-w-0">
              <button
                type="button"
                onClick={() => handleExport(comparisonRunId, "json")}
                className="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 rounded-lg text-xs font-medium flex items-center space-x-1.5 transition-colors focus:outline-none focus:ring-1 focus:ring-sky-500 shrink-0"
              >
                <FileJson className="w-3.5 h-3.5 text-sky-400 shrink-0" />
                <span>Comparison JSON</span>
              </button>
              <button
                type="button"
                onClick={() => handleExport(comparisonRunId, "csv")}
                className="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 rounded-lg text-xs font-medium flex items-center space-x-1.5 transition-colors focus:outline-none focus:ring-1 focus:ring-sky-500 shrink-0"
              >
                <FileSpreadsheet className="w-3.5 h-3.5 text-emerald-400 shrink-0" />
                <span>Comparison CSV</span>
              </button>
            </div>
          )}
        </div>
      </div>
    </div>

  );
};
