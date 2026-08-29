import React from "react";
import { Handle, Position } from "@xyflow/react";

export function LogicalGroupNode({ data }: { data: any }) {
  const type = data.type || "AND";
  const badgeColors: Record<string, string> = {
    AND: "bg-purple-900 text-purple-300 border-purple-500",
    OR: "bg-pink-900 text-pink-300 border-pink-500",
    NOT: "bg-red-900 text-red-300 border-red-500",
  };

  return (
    <div className="bg-slate-900 border border-purple-500 shadow-md rounded-xl p-3 w-40 text-slate-100 font-sans backdrop-blur-md bg-opacity-80">
      <Handle
        type="target"
        position={Position.Left}
        id="input"
        style={{ background: "#a855f7", width: "8px", height: "8px" }}
      />

      <div className="flex flex-col items-center justify-center space-y-2 py-2">
        <span className="text-[10px] text-slate-400 uppercase tracking-widest font-bold">Logical Group</span>
        <span className={`text-base font-extrabold px-4 py-1 rounded-full border ${badgeColors[type] || badgeColors.AND}`}>
          {type}
        </span>
      </div>

      <Handle
        type="source"
        position={Position.Right}
        id="output"
        style={{ background: "#a855f7", width: "8px", height: "8px" }}
      />
    </div>
  );
}
