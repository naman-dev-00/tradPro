import React from "react";
import { PlusCircle, HelpCircle } from "lucide-react";

export function Sidebar() {
  const onDragStart = (event: React.DragEvent, nodeType: string) => {
    event.dataTransfer.setData("application/reactflow", nodeType);
    event.dataTransfer.effectAllowed = "move";
  };

  return (
    <div className="bg-slate-950 border border-slate-800 rounded-xl p-4 text-slate-100 font-sans space-y-4">
      <div className="border-b border-slate-800 pb-2">
        <h3 className="font-bold text-sm text-indigo-400 tracking-wide uppercase">Toolbox</h3>
      </div>

      <p className="text-[10px] text-slate-400 leading-relaxed bg-slate-900/50 p-2 rounded border border-slate-900">
        Drag these components onto the canvas on the right to construct your options strategy. Connect them from the Root ports.
      </p>

      <div className="space-y-3">
        {/* Logical Group Node */}
        <div
          className="border border-purple-500 bg-purple-950/20 hover:bg-purple-950/40 rounded-xl p-3 cursor-grab select-none active:cursor-grabbing transition"
          onDragStart={(e) => onDragStart(e, "logicalGroup")}
          draggable
        >
          <div className="flex justify-between items-center">
            <span className="text-xs font-bold text-purple-300">Logical Group</span>
            <span className="bg-purple-900 text-purple-300 text-[9px] px-1.5 py-0.5 rounded font-mono">AND/OR/NOT</span>
          </div>
          <p className="text-[10px] text-slate-400 mt-1">Combine nested conditions with operators.</p>
        </div>

        {/* Condition Node */}
        <div
          className="border border-teal-500 bg-teal-950/20 hover:bg-teal-950/40 rounded-xl p-3 cursor-grab select-none active:cursor-grabbing transition"
          onDragStart={(e) => onDragStart(e, "condition")}
          draggable
        >
          <div className="flex justify-between items-center">
            <span className="text-xs font-bold text-teal-300">Condition Node</span>
            <span className="bg-teal-900 text-teal-300 text-[9px] px-1.5 py-0.5 rounded font-mono">LHS & RHS</span>
          </div>
          <p className="text-[10px] text-slate-400 mt-1">Single rule matching indicators and operators.</p>
        </div>

        {/* Action Node */}
        <div
          className="border border-amber-500 bg-amber-950/20 hover:bg-amber-950/40 rounded-xl p-3 cursor-grab select-none active:cursor-grabbing transition"
          onDragStart={(e) => onDragStart(e, "action")}
          draggable
        >
          <div className="flex justify-between items-center">
            <span className="text-xs font-bold text-amber-300">Action Node</span>
            <span className="bg-amber-900 text-amber-300 text-[9px] px-1.5 py-0.5 rounded font-mono">RISK CONFIG</span>
          </div>
          <p className="text-[10px] text-slate-400 mt-1">Execute trade and configure risk tolerances.</p>
        </div>
      </div>

      <div className="border-t border-slate-900 pt-3">
        <div className="flex items-center gap-1.5 text-[10px] text-slate-500">
          <HelpCircle size={12} />
          <span>Double-click an edge to delete it.</span>
        </div>
      </div>
    </div>
  );
}
