"use client";

import React from "react";
import { PaperPosition } from "../../lib/api";

interface Props {
  positions: PaperPosition[];
}

export const ActivePositionsTable: React.FC<Props> = ({ positions }) => {
  return (
    <div className="bg-slate-900 border border-slate-800 rounded-lg p-4">
      <h3 className="text-sm font-semibold text-slate-200 mb-3 flex items-center justify-between">
        <span>Active Paper Positions</span>
        <span className="text-xs font-normal text-slate-400">{positions.length} active</span>
      </h3>

      {positions.length === 0 ? (
        <div className="text-center py-6 text-xs text-slate-400 bg-slate-950/40 rounded border border-slate-800/50">
          No open positions held.
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs text-slate-300">
            <thead className="bg-slate-800/60 text-slate-400 uppercase tracking-wider text-[10px]">
              <tr>
                <th className="py-2 px-3">Instrument</th>
                <th className="py-2 px-3">Net Qty</th>
                <th className="py-2 px-3">Avg Price</th>
                <th className="py-2 px-3">Mark Price</th>
                <th className="py-2 px-3">Unrealized P&L</th>
                <th className="py-2 px-3">Gross Realized</th>
                <th className="py-2 px-3">Fees</th>
                <th className="py-2 px-3">Net Realized</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {positions.map((p) => {
                const netQty = Number(p.net_quantity);
                const unPnl = Number(p.unrealized_pnl);
                const netPnl = Number(p.net_realized_pnl);

                return (
                  <tr key={p.id} className="hover:bg-slate-800/40 transition-colors">
                    <td className="py-2.5 px-3 font-mono font-medium text-slate-200">{p.instrument_id}</td>
                    <td className={`py-2.5 px-3 font-semibold ${netQty > 0 ? "text-emerald-400" : netQty < 0 ? "text-rose-400" : "text-slate-400"}`}>
                      {netQty > 0 ? `+${netQty}` : netQty}
                    </td>
                    <td className="py-2.5 px-3">₹{p.average_entry_price}</td>
                    <td className="py-2.5 px-3">₹{p.last_mark_price}</td>
                    <td className={`py-2.5 px-3 font-medium ${unPnl >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                      ₹{p.unrealized_pnl}
                    </td>
                    <td className="py-2.5 px-3 text-slate-300">₹{p.gross_realized_pnl}</td>
                    <td className="py-2.5 px-3 text-slate-400">₹{p.total_fees}</td>
                    <td className={`py-2.5 px-3 font-semibold ${netPnl >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                      ₹{p.net_realized_pnl}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
};
