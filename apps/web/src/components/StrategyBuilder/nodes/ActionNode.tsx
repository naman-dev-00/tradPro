import React from "react";
import { Handle, Position } from "@xyflow/react";

export function ActionNode({ data }: { data: any }) {
  const risk = data.risk_config || {};

  return (
    <div className="bg-slate-900 border border-amber-500 shadow-md rounded-xl p-3 w-60 text-slate-100 font-sans backdrop-blur-md bg-opacity-80">
      <Handle
        type="target"
        position={Position.Left}
        id="input"
        style={{ background: "#d97706", width: "8px", height: "8px" }}
      />

      <div className="flex items-center justify-between border-b border-amber-800 pb-1 mb-2">
        <span className="text-[10px] text-amber-400 font-bold uppercase tracking-wide">Paper Trade Action</span>
        <span className="bg-amber-950 text-amber-300 text-[9px] px-1.5 py-0.5 rounded font-semibold">EXECUTE</span>
      </div>

      <div className="space-y-1.5 text-xs text-slate-300">
        <div className="flex justify-between">
          <span className="text-slate-400">Position Size:</span>
          <span className="font-semibold font-mono text-slate-200">
            {risk.max_position_size != null ? `₹${risk.max_position_size.toLocaleString()}` : "₹0"}
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-slate-400">Stop Loss:</span>
          <span className="font-semibold font-mono text-red-400">{risk.stop_loss_pct ?? 0}%</span>
        </div>
        <div className="flex justify-between">
          <span className="text-slate-400">Take Profit:</span>
          <span className="font-semibold font-mono text-green-400">{risk.take_profit_pct ?? 0}%</span>
        </div>
        <div className="flex justify-between border-t border-slate-800 pt-1 mt-1">
          <span className="text-slate-400">Candle Window:</span>
          <span className="font-semibold font-mono text-slate-200">{risk.validity_window ?? 1} candles</span>
        </div>
      </div>
    </div>
  );
}
