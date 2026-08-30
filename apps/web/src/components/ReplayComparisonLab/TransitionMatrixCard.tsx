"use client";

import React from "react";

interface TransitionMatrixCardProps {
  statusTransitionCounts: Record<string, number>;
}

const STATUSES = ["TRUE", "FALSE", "UNAVAILABLE", "INVALID", "ABSENT"];

const STATUS_BADGE_STYLE: Record<string, string> = {
  TRUE: "bg-emerald-950/80 text-emerald-300 border-emerald-600/50",
  FALSE: "bg-rose-950/80 text-rose-300 border-rose-600/50",
  UNAVAILABLE: "bg-amber-950/80 text-amber-300 border-amber-600/50",
  INVALID: "bg-red-950/80 text-red-300 border-red-600/50",
  ABSENT: "bg-slate-800/80 text-slate-400 border-slate-700/50",
};

export const TransitionMatrixCard: React.FC<TransitionMatrixCardProps> = ({
  statusTransitionCounts,
}) => {
  return (
    <div className="p-5 bg-slate-900/60 border border-slate-800 rounded-xl shadow-lg mb-8">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h3 className="font-semibold text-slate-100 text-base">
            Neutral Status Transition Matrix
          </h3>
          <p className="text-xs text-slate-400 mt-0.5">
            Cross-tabulation of Baseline Status (rows) vs Comparison Status (columns).
          </p>
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-xs text-left border-collapse min-w-[500px]">
          <thead>
            <tr className="border-b border-slate-800 text-slate-400 font-semibold bg-slate-950/40">
              <th className="py-2.5 px-3 border-r border-slate-800">
                Baseline ↓ \ Comparison →
              </th>
              {STATUSES.map((cStat) => (
                <th key={cStat} className="py-2.5 px-3 text-center">
                  <span
                    className={`inline-block px-2 py-0.5 rounded border text-[11px] font-mono font-medium ${
                      STATUS_BADGE_STYLE[cStat] || "bg-slate-800 text-slate-300"
                    }`}
                  >
                    {cStat}
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {STATUSES.map((bStat) => (
              <tr
                key={bStat}
                className="border-b border-slate-800/60 hover:bg-slate-800/30 transition-colors"
              >
                <td className="py-2.5 px-3 border-r border-slate-800 font-medium">
                  <span
                    className={`inline-block px-2 py-0.5 rounded border text-[11px] font-mono font-medium ${
                      STATUS_BADGE_STYLE[bStat] || "bg-slate-800 text-slate-300"
                    }`}
                  >
                    {bStat}
                  </span>
                </td>
                {STATUSES.map((cStat) => {
                  const transitionKey = `${bStat} -> ${cStat}`;
                  const count = statusTransitionCounts[transitionKey] || 0;
                  const isDiagonal = bStat === cStat;

                  return (
                    <td
                      key={cStat}
                      className={`py-2.5 px-3 text-center font-mono font-medium ${
                        count > 0
                          ? isDiagonal
                            ? "text-slate-300 bg-slate-800/20"
                            : "text-amber-300 font-bold bg-amber-950/20"
                          : "text-slate-600"
                      }`}
                    >
                      {count}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
};
