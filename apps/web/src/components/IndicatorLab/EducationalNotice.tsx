import React from "react";
import { AlertTriangle, ShieldCheck } from "lucide-react";

export function EducationalNotice() {
  return (
    <div className="bg-gradient-to-r from-amber-950/40 via-indigo-950/40 to-slate-900 border border-amber-500/30 rounded-xl p-4 text-slate-100 flex items-start gap-3 shadow-lg font-sans">
      <AlertTriangle className="text-amber-400 shrink-0 mt-0.5" size={20} />
      <div className="flex-1">
        <h4 className="font-bold text-xs text-amber-300 uppercase tracking-wider flex items-center gap-2">
          Educational Inspection Environment
          <span className="bg-amber-950 text-amber-300 text-[10px] px-2 py-0.5 rounded-full font-mono border border-amber-800/60">
            Synthetic Data Only
          </span>
        </h4>
        <p className="text-xs text-slate-300 mt-1 leading-relaxed font-medium">
          Educational synthetic data only. This page does not provide trading recommendations or place orders.
        </p>
      </div>
      <div className="shrink-0 text-right hidden sm:block">
        <span className="text-[10px] text-slate-400 font-mono flex items-center gap-1">
          <ShieldCheck size={12} className="text-indigo-400" />
          Deterministic Engine
        </span>
      </div>
    </div>
  );
}
