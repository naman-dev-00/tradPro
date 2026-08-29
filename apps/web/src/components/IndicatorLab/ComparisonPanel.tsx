"use client";

import React from "react";
import { ComparisonConfig } from "./CandlestickChart";
import { Layers, Trash2, Info } from "lucide-react";

interface ComparisonPanelProps {
  comparisons: ComparisonConfig[];
  onRemoveComparison: (id: string) => void;
}

export function ComparisonPanel({ comparisons, onRemoveComparison }: ComparisonPanelProps) {
  if (comparisons.length === 0) return null;

  return (
    <div className="bg-slate-950 border border-slate-900 rounded-xl p-4 text-slate-100 font-sans space-y-3">
      <div className="flex justify-between items-center border-b border-slate-900 pb-2">
        <h3 className="font-bold text-xs text-indigo-400 uppercase tracking-wider flex items-center gap-1.5">
          <Layers size={14} />
          Educational Comparison Mode ({comparisons.length}/3)
        </h3>
        <span className="text-[10px] text-slate-400 font-mono">Max 3 Indicators</span>
      </div>

      <p className="text-[11px] text-slate-400 leading-relaxed bg-slate-900/40 p-2.5 rounded-lg border border-slate-900 flex items-start gap-2">
        <Info size={14} className="text-indigo-400 shrink-0 mt-0.5" />
        <span>
          Comparing multiple indicator configurations on the same dataset helps visualize warm-up differences and sensitivity to parameter changes.
        </span>
      </p>

      <div className="space-y-2">
        {comparisons.map((comp) => {
          const paramStr = Object.entries(comp.params)
            .map(([k, v]) => `${k.replace("_", " ")}: ${v}`)
            .join(", ") || "No params";

          return (
            <div
              key={comp.id}
              className="bg-slate-900/60 border border-slate-800 rounded-lg p-2.5 flex items-center justify-between gap-3 text-xs"
            >
              <div className="flex items-center gap-2 overflow-hidden">
                <span
                  className="w-3 h-3 rounded-full shrink-0"
                  style={{ backgroundColor: comp.color }}
                />
                <span className="font-bold text-white">{comp.indicator}</span>
                <span className="text-[10px] text-slate-400 font-mono truncate">({paramStr})</span>
              </div>
              <button
                onClick={() => onRemoveComparison(comp.id)}
                className="text-slate-500 hover:text-red-400 p-1 rounded hover:bg-slate-800 transition"
                title="Remove Indicator"
              >
                <Trash2 size={14} />
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}
