import React, { useState } from "react";
import { ConditionResult, GroupResult, EvaluationStatus } from "@/lib/api";

interface RuleResultTreeProps {
  result: GroupResult | ConditionResult;
  title?: string;
}

export const RuleResultTree: React.FC<RuleResultTreeProps> = ({ result, title }) => {
  return (
    <div className="bg-slate-900 border border-slate-800 rounded-lg p-4 mb-4 shadow-sm">
      {title && <h3 className="text-sm font-bold text-slate-200 mb-3 uppercase tracking-wider">{title}</h3>}
      <TreeNode node={result} isRoot={true} />
    </div>
  );
};

const getStatusBadge = (status: EvaluationStatus) => {
  switch (status) {
    case "TRUE":
      return "bg-emerald-500/20 text-emerald-400 border-emerald-500/40";
    case "FALSE":
      return "bg-rose-500/20 text-rose-400 border-rose-500/40";
    case "UNAVAILABLE":
      return "bg-amber-500/20 text-amber-400 border-amber-500/40";
    case "INVALID":
      return "bg-purple-500/20 text-purple-400 border-purple-500/40";
    default:
      return "bg-slate-700 text-slate-300 border-slate-600";
  }
};

const TreeNode: React.FC<{ node: GroupResult | ConditionResult; isRoot?: boolean }> = ({ node, isRoot = false }) => {
  const [expanded, setExpanded] = useState(true);

  const isGroup = "logical_operator" in node && Array.isArray((node as GroupResult).child_results);

  if (isGroup) {
    const group = node as GroupResult;
    return (
      <div className={`my-1.5 font-mono text-xs ${!isRoot ? "ml-4 border-l border-slate-800 pl-3" : ""}`}>
        <div
          className="flex items-center gap-2 cursor-pointer py-1 px-2 rounded hover:bg-slate-800/60 transition-colors"
          onClick={() => setExpanded(!expanded)}
        >
          <span className="text-slate-500 text-xs font-bold w-4">{expanded ? "▼" : "▶"}</span>
          <span className="bg-indigo-950/80 text-indigo-300 border border-indigo-500/40 px-2 py-0.5 rounded font-bold uppercase text-[10px]">
            {group.logical_operator} GROUP
          </span>
          <span className="text-slate-400 text-[11px] font-medium">({group.group_id})</span>
          <span className={`ml-auto px-2 py-0.5 text-[10px] font-bold rounded border ${getStatusBadge(group.status)}`}>
            {group.status}
          </span>
        </div>

        {group.reason && (
          <div className="ml-6 my-1 text-[11px] text-slate-400 italic">Reason: {group.reason}</div>
        )}

        {expanded && (
          <div className="mt-1 space-y-1">
            {group.child_results.map((child, idx) => (
              <TreeNode key={idx} node={child} />
            ))}
          </div>
        )}
      </div>
    );
  }

  const cond = node as ConditionResult;
  return (
    <div className="my-1.5 ml-4 border-l border-slate-800 pl-3 font-mono text-xs">
      <div className="bg-slate-950/70 border border-slate-800/80 rounded p-2.5 space-y-1.5 hover:border-slate-700 transition-colors">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <span className="text-slate-300 font-bold text-xs">{cond.condition_id}</span>
            <span className="text-slate-500 text-[10px]">[{cond.operator}]</span>
          </div>
          <span className={`px-2 py-0.5 text-[10px] font-bold rounded border ${getStatusBadge(cond.status)}`}>
            {cond.status}
          </span>
        </div>

        {cond.timestamp && (
          <div className="text-[10px] text-slate-400">Timestamp: {cond.timestamp}</div>
        )}

        <div className="grid grid-cols-2 gap-2 bg-slate-900/60 p-1.5 rounded text-[11px] text-slate-300 border border-slate-800">
          <div>
            <span className="text-slate-500 text-[10px] block">Left Value:</span>
            {formatValue(cond.left_value)}
          </div>
          <div>
            <span className="text-slate-500 text-[10px] block">Right Value:</span>
            {formatValue(cond.right_value)}
          </div>
        </div>

        {cond.reason && (
          <div className="text-[11px] text-slate-300 bg-slate-900/40 p-1.5 rounded border border-slate-800">
            <span className="text-slate-500 font-semibold">Evaluation Reason:</span> {cond.reason}
          </div>
        )}
      </div>
    </div>
  );
};

const formatValue = (val: any) => {
  if (val === undefined || val === null) return <span className="text-slate-500 italic">null</span>;
  if (typeof val === "object") return <span className="text-teal-300 font-mono">{JSON.stringify(val)}</span>;
  if (typeof val === "number") return <span className="text-teal-300 font-bold">{val.toFixed(4)}</span>;
  return <span className="text-teal-300">{String(val)}</span>;
};
