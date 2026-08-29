import React from "react";

export const EducationalNotice: React.FC = () => {
  return (
    <div className="bg-amber-950/40 border border-amber-500/40 rounded-lg p-3.5 mb-6 flex items-start gap-3 shadow-md">
      <div className="text-amber-400 text-lg mt-0.5 font-bold">⚠️</div>
      <div className="text-xs text-amber-200/90 leading-relaxed">
        <span className="font-semibold text-amber-300">Educational Notice:</span>{" "}
        Educational synthetic data only. Results show Boolean rule evaluation and are not trading recommendations.
        This interface does not evaluate profitability, generate BUY or SELL signals, rank instruments, or execute orders.
      </div>
    </div>
  );
};
