import React from "react";
import { Handle, Position } from "@xyflow/react";

export function StrategyRootNode({ data }: { data: any }) {
  return (
    <div className="bg-slate-900 border border-indigo-500 shadow-lg rounded-xl p-4 w-72 text-slate-100 font-sans backdrop-blur-md bg-opacity-80">
      <div className="flex items-center justify-between border-b border-indigo-500 pb-2 mb-3">
        <h4 className="text-indigo-400 font-bold text-sm tracking-wide uppercase">Strategy Settings</h4>
        <span className="bg-indigo-900 text-indigo-300 text-xs px-2 py-0.5 rounded font-mono">ROOT</span>
      </div>
      <div className="space-y-2 text-xs">
        <div>
          <label className="text-slate-400 block font-semibold">Name</label>
          <div className="text-slate-100 font-medium truncate">{data.name || "Unnamed Strategy"}</div>
        </div>
        {data.description && (
          <div>
            <label className="text-slate-400 block font-semibold">Description</label>
            <div className="text-slate-300 truncate max-w-[240px]">{data.description}</div>
          </div>
        )}
        <div className="grid grid-cols-2 gap-2 border-t border-slate-800 pt-2">
          <div>
            <label className="text-slate-400 block font-semibold">Timeframe</label>
            <span className="bg-slate-800 text-slate-200 px-2 py-0.5 rounded font-mono mt-0.5 inline-block">{data.timeframe || "15m"}</span>
          </div>
          <div>
            <label className="text-slate-400 block font-semibold">Selection Mode</label>
            <span className="bg-slate-800 text-slate-200 px-2 py-0.5 rounded font-mono mt-0.5 inline-block text-[10px]">{data.candidate_selection_mode || "FIRST_ELIGIBLE"}</span>
          </div>
        </div>
      </div>

      {/* Global Conditions Handle */}
      <div className="relative mt-4 flex justify-between items-center bg-slate-800/50 p-1.5 rounded border border-slate-800">
        <span className="text-[10px] text-indigo-300 uppercase tracking-wider font-semibold">Global Conditions</span>
        <Handle
          type="source"
          position={Position.Right}
          id="global"
          style={{ top: "auto", transform: "translateY(50%)", background: "#6366f1", width: "10px", height: "10px" }}
        />
      </div>

      {/* Candidate Conditions Handle */}
      <div className="relative mt-2 flex justify-between items-center bg-slate-800/50 p-1.5 rounded border border-slate-800">
        <span className="text-[10px] text-teal-300 uppercase tracking-wider font-semibold">Candidate Conditions</span>
        <Handle
          type="source"
          position={Position.Right}
          id="candidate"
          style={{ top: "auto", transform: "translateY(50%)", background: "#14b8a6", width: "10px", height: "10px" }}
        />
      </div>

      {/* Action Handle */}
      <div className="relative mt-2 flex justify-between items-center bg-slate-800/50 p-1.5 rounded border border-slate-800">
        <span className="text-[10px] text-amber-300 uppercase tracking-wider font-semibold">Paper Trade Action</span>
        <Handle
          type="source"
          position={Position.Right}
          id="action"
          style={{ top: "auto", transform: "translateY(50%)", background: "#f59e0b", width: "10px", height: "10px" }}
        />
      </div>
    </div>
  );
}
