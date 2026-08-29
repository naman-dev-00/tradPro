import React from "react";
import { AlertTriangle, ShieldCheck } from "lucide-react";

export const EducationalNotice: React.FC = () => {
  return (
    <div
      role="region"
      aria-label="Educational Notice"
      className="mb-6 rounded-xl border border-amber-500/30 bg-amber-500/10 p-4 text-amber-200 backdrop-blur-md"
    >
      <div className="flex items-start space-x-3">
        <AlertTriangle className="mt-0.5 h-5 w-5 flex-shrink-0 text-amber-400" />
        <div className="space-y-1 text-sm leading-relaxed">
          <p className="font-semibold text-amber-300">
            Educational Synthetic-Data Boolean Replay
          </p>
          <p className="text-amber-200/90">
            This workspace evaluates Boolean strategy rules repeatedly across historical synthetic candle timestamps.
            It <strong className="text-amber-300 underline">does not simulate trades, calculate profitability, calculate win rates, or provide trading recommendations</strong>. All output statuses are strictly neutral (<code className="rounded bg-amber-950/60 px-1 py-0.5 font-mono text-xs text-amber-300">TRUE</code>, <code className="rounded bg-amber-950/60 px-1 py-0.5 font-mono text-xs text-amber-300">FALSE</code>, <code className="rounded bg-amber-950/60 px-1 py-0.5 font-mono text-xs text-amber-300">UNAVAILABLE</code>, <code className="rounded bg-amber-950/60 px-1 py-0.5 font-mono text-xs text-amber-300">INVALID</code>).
          </p>
          <div className="mt-2 flex items-center space-x-1.5 text-xs text-amber-400/90">
            <ShieldCheck className="h-4 w-4" />
            <span>Synthetic Fixture Datasets Only • Zero Broker Connectivity • Zero Order Execution</span>
          </div>
        </div>
      </div>
    </div>
  );
};
