import React from "react";
import { AlertCircle, CheckCircle } from "lucide-react";

interface ValidationErrorPanelProps {
  errors: string[];
}

export function ValidationErrorPanel({ errors }: ValidationErrorPanelProps) {
  const isValid = errors.length === 0;

  return (
    <div className="bg-slate-950 border border-slate-800 rounded-xl p-4 text-slate-100 font-sans h-full flex flex-col">
      <div className="border-b border-slate-800 pb-2 mb-3">
        <h3 className="font-bold text-sm text-indigo-400 tracking-wide uppercase flex items-center gap-1.5">
          Validation Output
          <span className={`text-[10px] px-2 py-0.5 rounded-full font-bold ${
            isValid ? "bg-green-950 text-green-400" : "bg-red-950 text-red-400"
          }`}>
            {isValid ? "Passed" : `${errors.length} Issues`}
          </span>
        </h3>
      </div>

      <div className="flex-1 overflow-y-auto max-h-[160px] space-y-2">
        {isValid ? (
          <div className="flex items-center gap-2 text-green-400 text-xs py-2">
            <CheckCircle size={16} />
            <span>All checks passed! The strategy conforms to rules and schemas.</span>
          </div>
        ) : (
          <div className="space-y-2.5">
            {errors.map((error, idx) => (
              <div key={idx} className="flex items-start gap-2 bg-red-950/20 border border-red-900/50 rounded p-2 text-[11px] text-red-300">
                <AlertCircle size={14} className="mt-0.5 shrink-0 text-red-400" />
                <span className="break-all font-medium leading-relaxed">{error}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
