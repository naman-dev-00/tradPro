"use client";

import React from "react";
import { Info } from "lucide-react";

export const EducationalNotice: React.FC = () => {
  return (
    <div
      role="region"
      aria-label="Educational synthetic data notice"
      className="p-4 mb-6 rounded-lg bg-amber-900/30 border border-amber-500/40 text-amber-200 text-sm flex items-start space-x-3 shadow-md"
    >
      <Info className="w-5 h-5 text-amber-400 shrink-0 mt-0.5" aria-hidden="true" />
      <div>
        <h4 className="font-semibold text-amber-300 mb-1">
          Synthetic Educational Inspection & Reproducibility Notice
        </h4>
        <p className="leading-relaxed">
          Synthetic educational inspection only. Comparisons show neutral Boolean status differences and are not recommendations, rankings, trading signals, or profitability results.
        </p>
      </div>
    </div>
  );
};
