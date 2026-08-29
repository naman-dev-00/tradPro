"use client";

import React, { useState, useEffect, useCallback, useRef } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  useNodesState,
  useEdgesState,
  addEdge,
  Connection,
  Edge,
  Node,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import { StrategyRootNode } from "./nodes/StrategyRootNode";
import { LogicalGroupNode } from "./nodes/LogicalGroupNode";
import { ConditionNode } from "./nodes/ConditionNode";
import { ActionNode } from "./nodes/ActionNode";
import { Sidebar } from "./Sidebar";
import { PropertiesPanel } from "./PropertiesPanel";
import { ValidationErrorPanel } from "./ValidationErrorPanel";
import { JsonPreview } from "./JsonPreview";
import { graphToStrategy, strategyToGraph } from "../../lib/graph";
import { validateStrategy, saveStrategy } from "../../lib/api";
import { AlertCircle, Check } from "lucide-react";

const nodeTypes = {
  strategyRoot: StrategyRootNode,
  logicalGroup: LogicalGroupNode,
  condition: ConditionNode,
  action: ActionNode,
};

const initialNodes: Node[] = [
  {
    id: "root",
    type: "strategyRoot",
    position: { x: 100, y: 300 },
    data: {
      id: crypto.randomUUID(),
      name: "My Strategy",
      description: "Options trading strategy blueprint",
      timeframe: "15m",
      candidate_selection_mode: "FIRST_ELIGIBLE",
    },
  },
];

const initialEdges: Edge[] = [];

interface StrategyBuilderProps {
  initialStrategy?: any;
  onSaveSuccess?: () => void;
}

export function StrategyBuilder({ initialStrategy, onSaveSuccess }: StrategyBuilderProps) {
  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [strategyJson, setStrategyJson] = useState<any>({});
  const [validationErrors, setValidationErrors] = useState<string[]>([]);
  const [saveStatus, setSaveStatus] = useState<{ type: "success" | "error" | null; msg: string }>({ type: null, msg: "" });
  const [reactFlowInstance, setReactFlowInstance] = useState<any>(null);
  const flowWrapper = useRef<HTMLDivElement>(null);

  // Sync loaded strategy if provided
  useEffect(() => {
    if (initialStrategy) {
      const { nodes: loadedNodes, edges: loadedEdges } = strategyToGraph(initialStrategy);
      setNodes(loadedNodes);
      setEdges(loadedEdges);
    }
  }, [initialStrategy, setNodes, setEdges]);

  // Regenerate JSON and validate whenever graph structure changes
  useEffect(() => {
    const payload = graphToStrategy(nodes, edges);
    if (payload) {
      setStrategyJson(payload);

      // Perform validation check
      validateStrategy(payload)
        .then((res) => {
          setValidationErrors(res.errors || []);
        })
        .catch((err) => {
          setValidationErrors([`Backend validation unreachable: ${err.message}`]);
        });
    }
  }, [nodes, edges]);

  const onConnect = useCallback(
    (params: Connection) => setEdges((eds) => addEdge({ ...params, type: "default", animated: true }, eds)),
    [setEdges]
  );

  const onNodeClick = useCallback((_: any, node: Node) => {
    setSelectedNodeId(node.id);
  }, []);

  const onPaneClick = useCallback(() => {
    setSelectedNodeId(null);
  }, []);

  // Update properties of a node
  const onNodeUpdate = useCallback(
    (nodeId: string, updatedData: any) => {
      setNodes((nds) =>
        nds.map((node) => (node.id === nodeId ? { ...node, data: updatedData } : node))
      );
    },
    [setNodes]
  );

  // Drag and drop node creation
  const onDragOver = useCallback((event: React.DragEvent) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
  }, []);

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault();

      if (!reactFlowInstance || !flowWrapper.current) return;

      const type = event.dataTransfer.getData("application/reactflow");
      if (!type) return;

      const rect = flowWrapper.current.getBoundingClientRect();
      const position = reactFlowInstance.screenToFlowPosition({
        x: event.clientX - rect.left,
        y: event.clientY - rect.top,
      });

      // Default data structures for new nodes
      let defaultData: any = {};
      if (type === "logicalGroup") {
        defaultData = { type: "AND" };
      } else if (type === "condition") {
        defaultData = {
          lhs: { indicator: "RSI", symbol: "CANDIDATE", params: { period: 14 } },
          operator: "LESS_THAN",
          rhs: { type: "NUMBER", value: 40 },
        };
      } else if (type === "action") {
        defaultData = {
          type: "PAPER_TRADE",
          risk_config: {
            max_position_size: 100000,
            stop_loss_pct: 2.5,
            take_profit_pct: 5.0,
            validity_window: 5,
          },
        };
      }

      const newNode: Node = {
        id: `${type}-${Date.now()}`,
        type,
        position,
        data: defaultData,
      };

      setNodes((nds) => nds.concat(newNode));
    },
    [reactFlowInstance, setNodes]
  );

  const handleSave = async () => {
    try {
      setSaveStatus({ type: null, msg: "" });
      const isUpdate = !!initialStrategy;
      const res = await saveStrategy(strategyJson, isUpdate);
      setSaveStatus({ type: "success", msg: "Strategy saved successfully!" });
      if (onSaveSuccess) {
        setTimeout(onSaveSuccess, 1500);
      }
    } catch (err: any) {
      setSaveStatus({ type: "error", msg: err.message || "Failed to save strategy." });
    }
  };

  const handleLoadExample = () => {
    const example = {
      id: "7b5ef35b-1175-430c-ab23-f22287955c45",
      name: "Example Strategy",
      description: "Global: NIFTY price > EMA 200. Candidate: RSI < 40 AND price TOUCHES S1.",
      timeframe: "15m",
      candidate_selection_mode: "FIRST_ELIGIBLE",
      global_conditions: {
        type: "CONDITION",
        lhs: { indicator: "PRICE", symbol: "NIFTY" },
        operator: "GREATER_THAN",
        rhs: {
          type: "INDICATOR",
          indicator: { indicator: "EMA", symbol: "NIFTY", params: { period: 200 } },
        },
      },
      candidate_conditions: {
        type: "AND",
        conditions: [
          {
            type: "CONDITION",
            lhs: { indicator: "RSI", symbol: "CANDIDATE", params: { period: 14 } },
            operator: "LESS_THAN",
            rhs: { type: "NUMBER", value: 40 },
          },
          {
            type: "CONDITION",
            lhs: { indicator: "PRICE", symbol: "CANDIDATE" },
            operator: "TOUCHES",
            rhs: {
              type: "INDICATOR",
              indicator: { indicator: "PIVOT", symbol: "CANDIDATE", params: { level: "S1" } },
            },
          },
        ],
      },
      action: {
        type: "PAPER_TRADE",
        risk_config: {
          max_position_size: 100000,
          stop_loss_pct: 2.5,
          take_profit_pct: 5,
          validity_window: 5,
        },
      },
    };

    const { nodes: exampleNodes, edges: exampleEdges } = strategyToGraph(example);
    setNodes(exampleNodes);
    setEdges(exampleEdges);
    setSaveStatus({ type: "success", msg: "Example loaded onto canvas." });
    setTimeout(() => setSaveStatus({ type: null, msg: "" }), 2000);
  };

  // Double click edge to delete
  const onEdgeDoubleClick = useCallback(
    (_: any, edge: Edge) => {
      setEdges((eds) => eds.filter((e) => e.id !== edge.id));
    },
    [setEdges]
  );

  const selectedNode = nodes.find((n) => n.id === selectedNodeId);

  return (
    <div className="flex flex-col h-full bg-slate-950 text-slate-100 font-sans">
      {/* Top Header Actions */}
      <div className="flex justify-between items-center border-b border-slate-900 bg-slate-950 p-4 shrink-0">
        <div>
          <h1 className="text-xl font-extrabold tracking-tight text-white flex items-center gap-2">
            Strategy Builder Workspace
            <span className="text-xs bg-indigo-900/50 border border-indigo-700/50 text-indigo-300 font-mono px-2 py-0.5 rounded">Milestone 1</span>
          </h1>
          <p className="text-xs text-slate-400 mt-0.5">Design, construct, and validate your visual options rule sets.</p>
        </div>
        <div className="flex items-center gap-3">
          <button
            onClick={handleLoadExample}
            className="border border-slate-800 hover:bg-slate-900 text-slate-200 text-xs px-3.5 py-1.5 rounded-lg font-medium transition"
          >
            Load Example Strategy
          </button>
          <button
            onClick={handleSave}
            disabled={validationErrors.length > 0}
            className={`text-white text-xs px-4 py-1.5 rounded-lg font-semibold shadow transition ${
              validationErrors.length > 0
                ? "bg-slate-800 text-slate-500 cursor-not-allowed border border-slate-900"
                : "bg-indigo-600 hover:bg-indigo-500"
            }`}
          >
            Save Strategy Blueprint
          </button>
        </div>
      </div>

      {/* Save Status Notification Banner */}
      {saveStatus.msg && (
        <div
          className={`px-4 py-2 text-xs flex items-center gap-2 border-b shrink-0 ${
            saveStatus.type === "success"
              ? "bg-green-950/30 border-green-900 text-green-300"
              : "bg-red-950/30 border-red-900 text-red-300"
          }`}
        >
          {saveStatus.type === "success" ? <Check size={14} /> : <AlertCircle size={14} />}
          <span>{saveStatus.msg}</span>
        </div>
      )}

      {/* Workspace Area */}
      <div className="flex flex-1 min-h-0">
        {/* Left Toolbox */}
        <div className="w-64 border-r border-slate-900 p-4 overflow-y-auto shrink-0 bg-slate-950/40">
          <Sidebar />
        </div>

        {/* Visual Graph Canvas */}
        <div className="flex-1 relative bg-slate-950" ref={flowWrapper}>
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onInit={setReactFlowInstance}
            onDrop={onDrop}
            onDragOver={onDragOver}
            onNodeClick={onNodeClick}
            onPaneClick={onPaneClick}
            onEdgeDoubleClick={onEdgeDoubleClick}
            fitView
          >
            <Background color="#1e293b" gap={20} />
            <Controls className="bg-slate-900 border-slate-800 text-slate-100 [&>button]:border-slate-800 [&>button]:bg-slate-900 [&>button]:text-slate-100 hover:[&>button]:bg-slate-800" />
          </ReactFlow>
        </div>

        {/* Right Properties Panel */}
        <div className="w-80 border-l border-slate-900 p-4 overflow-y-auto shrink-0 bg-slate-950/40">
          <PropertiesPanel selectedNode={selectedNode} onUpdate={onNodeUpdate} />
        </div>
      </div>

      {/* Bottom Validation and JSON Previews */}
      <div className="h-56 border-t border-slate-900 bg-slate-950 shrink-0 grid grid-cols-2 gap-4 p-4 min-h-[220px]">
        <div>
          <ValidationErrorPanel errors={validationErrors} />
        </div>
        <div>
          <JsonPreview strategyJson={strategyJson} />
        </div>
      </div>
    </div>
  );
}
