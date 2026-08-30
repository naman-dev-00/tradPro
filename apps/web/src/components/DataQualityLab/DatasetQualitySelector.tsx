import React from "react";
import { DatasetQualityListItem } from "@/lib/api";
import { CheckCircle2, AlertTriangle, XCircle, Database } from "lucide-react";

interface DatasetQualitySelectorProps {
  datasets: DatasetQualityListItem[];
  selectedDatasetId: string;
  onSelectDataset: (datasetId: string) => void;
  selectedBatchIds: string[];
  onToggleBatchId: (datasetId: string) => void;
  isBatchMode: boolean;
}

export function DatasetQualitySelector({
  datasets,
  selectedDatasetId,
  onSelectDataset,
  selectedBatchIds,
  onToggleBatchId,
  isBatchMode,
}: DatasetQualitySelectorProps) {
  const getStatusIcon = (status: string) => {
    switch (status) {
      case "PASS":
        return <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" aria-hidden="true" />;
      case "WARN":
        return <AlertTriangle className="w-3.5 h-3.5 text-amber-400" aria-hidden="true" />;
      case "FAIL":
        return <XCircle className="w-3.5 h-3.5 text-rose-400" aria-hidden="true" />;
      default:
        return null;
    }
  };

  const getStatusBadgeClass = (status: string) => {
    switch (status) {
      case "PASS":
        return "bg-emerald-950/60 text-emerald-300 border-emerald-500/40";
      case "WARN":
        return "bg-amber-950/60 text-amber-300 border-amber-500/40";
      case "FAIL":
        return "bg-rose-950/60 text-rose-300 border-rose-500/40";
      default:
        return "bg-slate-800 text-slate-300 border-slate-700";
    }
  };

  return (
    <div className="bg-slate-900/70 border border-slate-800 rounded-xl p-5 mb-6">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center space-x-2">
          <Database className="w-4 h-4 text-sky-400" aria-hidden="true" />
          <h2 className="text-sm font-semibold text-slate-200 uppercase tracking-wider">
            {isBatchMode ? "Select Datasets for Batch Audit (1-20)" : "Select Packaged Synthetic Dataset"}
          </h2>
        </div>
        <span className="text-xs text-slate-400 font-mono">
          {isBatchMode ? `${selectedBatchIds.length} / ${datasets.length} selected` : `${datasets.length} Whitelisted Fixtures`}
        </span>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
        {datasets.map((ds) => {
          const isSelected = isBatchMode
            ? selectedBatchIds.includes(ds.dataset_id)
            : selectedDatasetId === ds.dataset_id;

          return (
            <button
              key={ds.dataset_id}
              type="button"
              onClick={() => {
                if (isBatchMode) {
                  onToggleBatchId(ds.dataset_id);
                } else {
                  onSelectDataset(ds.dataset_id);
                }
              }}
              className={`text-left p-3.5 rounded-lg border transition-all flex flex-col justify-between space-y-2 focus:outline-none focus:ring-2 focus:ring-sky-500 ${
                isSelected
                  ? "bg-sky-950/40 border-sky-500/70 shadow-md shadow-sky-950/20"
                  : "bg-slate-800/50 border-slate-700/60 hover:bg-slate-800 hover:border-slate-600"
              }`}
              aria-pressed={isSelected}
            >
              <div className="flex items-start justify-between">
                <div>
                  <div className="font-medium text-xs text-slate-200 line-clamp-1">{ds.display_name}</div>
                  <div className="text-[10px] font-mono text-slate-400 mt-0.5">{ds.dataset_id}</div>
                </div>
                <span
                  className={`inline-flex items-center space-x-1 px-2 py-0.5 rounded text-[10px] font-bold tracking-wider border ${getStatusBadgeClass(
                    ds.status
                  )}`}
                >
                  {getStatusIcon(ds.status)}
                  <span>{ds.status}</span>
                </span>
              </div>

              <div className="flex items-center space-x-2 pt-1 border-t border-slate-700/40 text-[11px] text-slate-400">
                <span className="px-1.5 py-0.5 bg-slate-800 rounded border border-slate-700 font-mono text-[10px] text-sky-300">
                  {ds.category}
                </span>
                <span className="px-1.5 py-0.5 bg-slate-800 rounded border border-slate-700 font-mono text-[10px] text-slate-300">
                  {ds.timeframe}
                </span>
                <span className="font-mono text-[10px] text-slate-500 ml-auto">
                  {ds.summary.completed_rows} rows
                </span>
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}
