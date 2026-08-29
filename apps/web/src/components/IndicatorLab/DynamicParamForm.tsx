"use client";

import React from "react";
import { SupportedIndicatorMetadata } from "../../lib/api";
import { Sliders } from "lucide-react";

interface DynamicParamFormProps {
  indicator: SupportedIndicatorMetadata | null;
  params: Record<string, any>;
  errors: Record<string, string>;
  onChangeParam: (key: string, value: number) => void;
}

export function DynamicParamForm({
  indicator,
  params,
  errors,
  onChangeParam,
}: DynamicParamFormProps) {
  if (!indicator) return null;

  const paramKeys = Object.keys(indicator.parameters);

  if (paramKeys.length === 0) {
    return (
      <div className="bg-slate-900/40 border border-slate-900 rounded-lg p-3 text-center">
        <p className="text-[11px] text-slate-400">This indicator has no configurable parameters.</p>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="border-b border-slate-900 pb-1 flex items-center gap-1 text-[11px] font-bold text-slate-400 uppercase tracking-wider">
        <Sliders size={12} />
        Parameters
      </div>

      <div className="space-y-2.5">
        {paramKeys.map((key) => {
          const meta = indicator.parameters[key];
          const val = params[key] ?? meta.default ?? 14;
          const err = errors[key];

          return (
            <div key={key}>
              <div className="flex justify-between items-center text-[11px] mb-1">
                <label className="font-semibold text-slate-300 capitalize">{key.replace("_", " ")}</label>
                <span className="text-[10px] text-slate-500 font-mono">Min: {meta.minimum ?? 1}</span>
              </div>
              <input
                type="number"
                value={val}
                min={meta.minimum ?? 1}
                onChange={(e) => onChangeParam(key, parseInt(e.target.value, 10) || 0)}
                className={`w-full bg-slate-900 border rounded-lg px-3 py-1.5 text-xs text-white font-mono focus:outline-none transition ${
                  err ? "border-red-500/80 focus:border-red-400" : "border-slate-800 focus:border-indigo-500"
                }`}
              />
              {err && <p className="text-[10px] text-red-400 mt-1 font-medium">{err}</p>}
            </div>
          );
        })}
      </div>
    </div>
  );
}
