import React from "react";
import { Info } from "lucide-react";

export function EducationalNotice() {
  return (
    <div
      role="note"
      aria-label="Educational Notice"
      className="bg-amber-950/40 border border-amber-500/40 rounded-lg p-4 mb-6 flex items-start space-x-3 text-amber-200"
    >
      <Info className="w-5 h-5 mt-0.5 text-amber-400 shrink-0" aria-hidden="true" />
      <div className="text-sm">
        <span className="font-semibold block mb-0.5">Synthetic Educational Inspection Only</span>
        <p className="text-amber-300/90 text-xs leading-relaxed">
          Packaged synthetic educational data only. Quality findings describe dataset integrity and provenance, not market quality, recommendations, rankings, or expected outcomes.
        </p>
      </div>
    </div>
  );
}
