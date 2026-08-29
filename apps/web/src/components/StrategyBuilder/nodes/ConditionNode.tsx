import React from "react";
import { Handle, Position } from "@xyflow/react";

function formatIndicator(expr: any) {
  if (!expr) return "Select Indicator";
  const params: string[] = [];
  if (expr.params?.period) params.push(String(expr.params.period));
  if (expr.params?.level) params.push(expr.params.level);

  const paramsStr = params.length > 0 ? ` (${params.join(", ")})` : "";
  const symbolStr = expr.symbol ? ` [${expr.symbol}]` : "";
  return `${expr.indicator}${paramsStr}${symbolStr}`;
}

function formatComparison(rhs: any) {
  if (!rhs) return "None";
  if (rhs.type === "NUMBER") return String(rhs.value ?? 0);
  if (rhs.type === "NUMBER_RANGE") return `[${rhs.range?.join(", ") ?? "0, 0"}]`;
  if (rhs.type === "INDICATOR") return formatIndicator(rhs.indicator);
  return "None";
}

function formatOperator(op: string) {
  const map: Record<string, string> = {
    GREATER_THAN: " > ",
    LESS_THAN: " < ",
    CROSSES_ABOVE: " crosses above ",
    CROSSES_BELOW: " crosses below ",
    TOUCHES: " touches ",
    BETWEEN: " between ",
  };
  return map[op] || op;
}

export function ConditionNode({ data }: { data: any }) {
  const lhsStr = formatIndicator(data.lhs);
  const operatorStr = formatOperator(data.operator || "GREATER_THAN");
  const rhsStr = formatComparison(data.rhs);

  return (
    <div className="bg-slate-900 border border-teal-500 shadow-md rounded-xl p-3 w-64 text-slate-100 font-sans backdrop-blur-md bg-opacity-80">
      <Handle
        type="target"
        position={Position.Left}
        id="input"
        style={{ background: "#0d9488", width: "8px", height: "8px" }}
      />

      <div className="flex items-center justify-between border-b border-teal-800 pb-1 mb-2">
        <span className="text-[10px] text-teal-400 font-bold uppercase tracking-wide">Condition</span>
        <span className="bg-teal-950 text-teal-300 text-[9px] px-1.5 py-0.5 rounded font-semibold">IF</span>
      </div>

      <div className="space-y-1 text-xs">
        <div className="text-slate-300 truncate font-semibold" title={lhsStr}>
          {lhsStr}
        </div>
        <div className="text-teal-400 font-bold text-[10px] uppercase font-mono tracking-wider">
          {operatorStr}
        </div>
        <div className="text-slate-300 truncate font-semibold" title={rhsStr}>
          {rhsStr}
        </div>
      </div>
    </div>
  );
}
