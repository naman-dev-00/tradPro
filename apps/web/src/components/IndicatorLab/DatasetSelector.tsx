"use client";

import React, { useState } from "react";
import { DatasetMetadata, DatasetDetailResponse } from "../../lib/api";
import { Database, Clock, Eye, X, Filter } from "lucide-react";

interface DatasetSelectorProps {
  datasets: DatasetMetadata[];
  selectedDatasetId: string;
  datasetDetail: DatasetDetailResponse | null;
  loading: boolean;
  onSelectDataset: (id: string) => void;
}

export function DatasetSelector({
  datasets,
  selectedDatasetId,
  datasetDetail,
  loading,
  onSelectDataset,
}: DatasetSelectorProps) {
  const [showModal, setShowModal] = useState(false);

  return (
    <div className="bg-slate-950 border border-slate-900 rounded-xl p-4 text-slate-100 font-sans space-y-4">
      <div className="flex justify-between items-center border-b border-slate-900 pb-2">
        <h3 className="font-bold text-xs text-indigo-400 uppercase tracking-wider flex items-center gap-1.5">
          <Database size={14} />
          Synthetic Dataset Selection
        </h3>
        {datasetDetail && (
          <button
            onClick={() => setShowModal(true)}
            className="text-[11px] text-indigo-400 hover:text-indigo-300 font-medium flex items-center gap-1 transition"
          >
            <Eye size={12} />
            Inspect OHLCV Data
          </button>
        )}
      </div>

      {/* Selector Dropdown */}
      <div>
        <label className="block text-[11px] font-semibold text-slate-400 mb-1.5">Select Dataset Fixture</label>
        <select
          value={selectedDatasetId}
          onChange={(e) => onSelectDataset(e.target.value)}
          disabled={loading}
          className="w-full bg-slate-900 border border-slate-800 rounded-lg px-3 py-2 text-xs text-white focus:outline-none focus:border-indigo-500 transition"
        >
          {datasets.map((d) => (
            <option key={d.id} value={d.id}>
              {d.name} ({d.instrument_id} • {d.timeframe})
            </option>
          ))}
        </select>
      </div>

      {/* Dataset Summary Metrics */}
      {datasetDetail && (
        <div className="space-y-2 pt-1">
          <div className="flex items-center justify-between text-xs">
            <span className="text-slate-400">Instrument:</span>
            <span className="font-mono bg-indigo-950 text-indigo-300 border border-indigo-900/60 px-2 py-0.5 rounded font-bold text-[11px]">
              {datasetDetail.instrument_id}
            </span>
          </div>

          <div className="flex items-center justify-between text-xs">
            <span className="text-slate-400">Timeframe:</span>
            <span className="font-mono bg-slate-900 border border-slate-800 text-slate-300 px-2 py-0.5 rounded font-bold text-[11px] flex items-center gap-1">
              <Clock size={10} />
              {datasetDetail.timeframe}
            </span>
          </div>

          <div className="grid grid-cols-3 gap-2 pt-2 border-t border-slate-900/80 text-center">
            <div className="bg-slate-900/50 border border-slate-900 rounded p-2">
              <span className="text-[10px] text-slate-400 block">Total</span>
              <span className="text-xs font-mono font-bold text-slate-200">{datasetDetail.total_candles}</span>
            </div>
            <div className="bg-slate-900/50 border border-slate-900 rounded p-2">
              <span className="text-[10px] text-slate-400 block">Completed</span>
              <span className="text-xs font-mono font-bold text-green-400">{datasetDetail.completed_candles}</span>
            </div>
            <div className="bg-slate-900/50 border border-slate-900 rounded p-2">
              <span className="text-[10px] text-slate-400 block">Excluded</span>
              <span className={`text-xs font-mono font-bold ${
                datasetDetail.excluded_incomplete_candles > 0 ? "text-amber-400" : "text-slate-500"
              }`}>
                {datasetDetail.excluded_incomplete_candles}
              </span>
            </div>
          </div>
        </div>
      )}

      {/* Raw OHLCV Modal */}
      {showModal && datasetDetail && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4">
          <div className="bg-slate-950 border border-slate-800 rounded-2xl max-w-4xl w-full max-h-[85vh] flex flex-col shadow-2xl overflow-hidden">
            <div className="p-4 border-b border-slate-900 flex justify-between items-center bg-slate-950">
              <div>
                <h3 className="font-bold text-sm text-white flex items-center gap-2">
                  Inspect Raw OHLCV Candles
                  <span className="text-xs bg-slate-900 text-indigo-400 border border-indigo-900 px-2 py-0.5 rounded font-mono">
                    {datasetDetail.instrument_id}
                  </span>
                </h3>
                <p className="text-xs text-slate-400 mt-0.5">{datasetDetail.name} • {datasetDetail.completed_candles} completed candles</p>
              </div>
              <button
                onClick={() => setShowModal(false)}
                className="text-slate-400 hover:text-white p-1 rounded-lg hover:bg-slate-900 transition"
              >
                <X size={18} />
              </button>
            </div>

            <div className="flex-1 overflow-auto p-4">
              <table className="w-full text-left border-collapse text-xs font-mono">
                <thead>
                  <tr className="border-b border-slate-800 text-slate-400 bg-slate-900/60 sticky top-0">
                    <th className="py-2 px-3 font-semibold">Timestamp (UTC)</th>
                    <th className="py-2 px-3 font-semibold">Open</th>
                    <th className="py-2 px-3 font-semibold">High</th>
                    <th className="py-2 px-3 font-semibold">Low</th>
                    <th className="py-2 px-3 font-semibold">Close</th>
                    <th className="py-2 px-3 font-semibold">Volume</th>
                    <th className="py-2 px-3 font-semibold">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-900/80">
                  {datasetDetail.candles.map((c, idx) => (
                    <tr key={idx} className="hover:bg-slate-900/40 text-slate-300">
                      <td className="py-2 px-3 text-slate-400">{new Date(c.timestamp).toISOString()}</td>
                      <td className="py-2 px-3 text-slate-200">{c.open.toFixed(2)}</td>
                      <td className="py-2 px-3 text-green-400">{c.high.toFixed(2)}</td>
                      <td className="py-2 px-3 text-red-400">{c.low.toFixed(2)}</td>
                      <td className="py-2 px-3 text-slate-100 font-bold">{c.close.toFixed(2)}</td>
                      <td className="py-2 px-3 text-slate-300">{c.volume.toLocaleString()}</td>
                      <td className="py-2 px-3">
                        <span className="bg-green-950 text-green-400 text-[10px] px-1.5 py-0.5 rounded border border-green-900/50">
                          Closed
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
