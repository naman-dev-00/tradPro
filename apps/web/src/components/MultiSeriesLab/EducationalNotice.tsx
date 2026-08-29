import React from "react";

export const EducationalNotice: React.FC = () => {
  return (
    <div
      role="region"
      aria-label="Educational Notice"
      className="bg-amber-500/10 border border-amber-500/30 rounded-lg p-4 mb-6 flex items-start gap-3 text-amber-200"
    >
      <div className="p-1 text-amber-400 shrink-0">
        <svg
          xmlns="http://www.w3.org/2000/svg"
          className="h-6 w-6"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          aria-hidden="true"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 10 11-18 0 9 9 0 0118 0z"
          />
        </svg>
      </div>
      <div>
        <h2 className="text-sm font-semibold text-amber-300 uppercase tracking-wider mb-1">
          Educational Inspection Interface
        </h2>
        <p className="text-xs sm:text-sm text-amber-200/90 leading-relaxed">
          Educational synthetic data only. Results are independent Boolean inspections and are not rankings or trading recommendations.
          This interface does not evaluate profitability, generate BUY or SELL signals, rank instruments, or execute orders.
        </p>
      </div>
    </div>
  );
};
