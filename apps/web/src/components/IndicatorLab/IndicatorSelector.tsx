"use client";

import React from "react";
import { SupportedIndicatorMetadata } from "../../lib/api";
import { DynamicParamForm } from "./DynamicParamForm";
import { Activity, Play } from "lucide-react";

interface IndicatorSelectorProps {
  supportedIndicators: SupportedIndicatorMetadata[];
  selectedIndicatorName: string;
  params: Record<string, any>;
  paramErrors: Record<string, string>;
  loading: boolean;
  onSelectIndicator: (name: string) => void;
  onChangeParam: (key: string, val: number) => void;
  onCalculate: () => void;
  onAddComparison?: () => void;
  comparisonCount: number;
}

export function IndicatorSelector({
  supportedIndicators,
  selectedIndicatorName,
  params,
  paramErrors,
  loading,
  onSelectIndicator,
  onChangeParam,
  onCalculate,
  onAddComparison,
  comparisonCount,
}: IndicatorSelectorProps) {
  const currentMetadata =
    supportedIndicators.find((i) => i.name === selectedIndicatorName) || supportedIndicators[0] || null;

  const hasErrors = Object.keys(paramErrors).length > 0;

  return (
    <div className="bg-slate-950 border border-slate-900 rounded-xl p-4 text-slate-100 font-sans space-y-4">
      <div className="border-b border-slate-900 pb-2">
        <h3 className="font-bold text-xs text-indigo-400 uppercase tracking-wider flex items-center gap-1.5">
          <Activity size={14} />
          Indicator Configurator
        </h3>
      </div>

      {/* Select Indicator Dropdown */}
      <div>
        <label className="block text-[11px] font-semibold text-slate-400 mb-1.5">Select Indicator</label>
        <select
          value={selectedIndicatorName}
          onChange={(e) => onSelectIndicator(e.target.value)}
          disabled={loading}
          className="w-full bg-slate-900 border border-slate-800 rounded-lg px-3 py-2 text-xs text-white focus:outline-none focus:border-indigo-500 transition"
        >
          {supportedIndicators.map((ind) => (
            <option key={ind.name} value={ind.name}>
              {ind.name} ({ind.description})
            </option>
          ))}
        </select>
      </div>

      {/* Indicator Description */}
      {currentMetadata && (
        <p className="text-[11px] text-slate-400 leading-relaxed bg-slate-900/40 p-2.5 rounded-lg border border-slate-900">
          {currentMetadata.description}
        </p>
      )}

      {/* Dynamic Parameter Form */}
      <DynamicParamForm
        indicator={currentMetadata}
        params={params}
        errors={paramErrors}
        onChangeParam={onChangeParam}
      />

      {/* Action Buttons */}
      <div className="space-y-2 pt-2">
        <button
          onClick={onCalculate}
          disabled={loading || hasErrors}
          className={`w-full py-2 px-4 rounded-lg font-bold text-xs flex items-center justify-center gap-2 shadow transition ${
            loading || hasErrors
              ? "bg-slate-800 text-slate-500 cursor-not-allowed border border-slate-900"
              : "bg-indigo-600 hover:bg-indigo-500 text-white"
          }`}
        >
          {loading ? (
            <>
              <div className="w-3.5 h-3.5 border-2 border-white border-t-transparent rounded-full animate-spin"></div>
              <span>Calculating...</span>
            </>
          ) : (
            <>
              <Play size={14} />
              Calculate Indicator
            </>
          )}
        </button>

        {onAddComparison && (
          <button
            onClick={onAddComparison}
            disabled={loading || hasErrors || comparisonCount >= 3}
            className={`w-full py-1.5 px-3 rounded-lg text-xs font-semibold border transition ${
              comparisonCount >= 3 || hasErrors || loading
                ? "border-slate-900 bg-slate-900/30 text-slate-600 cursor-not-allowed"
                : "border-slate-800 hover:bg-slate-900 text-indigo-300"
            }`}
          >
            {comparisonCount >= 3 ? "Max 3 Comparisons Added" : "+ Add to Comparison Mode"}
          </button>
        )}
      </div>
    </div>
  );
}
