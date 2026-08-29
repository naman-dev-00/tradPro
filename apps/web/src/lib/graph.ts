import { Node, Edge } from "@xyflow/react";

export interface StrategyPayload {
  id: string;
  name: string;
  description?: string;
  timeframe: string;
  candidate_selection_mode: string;
  global_conditions?: any;
  candidate_conditions?: any;
  action: {
    type: string;
    risk_config: {
      max_position_size: number;
      stop_loss_pct: number;
      take_profit_pct: number;
      validity_window: number;
    };
  };
}

// Converts the React Flow visual graph to standard Strategy JSON matching contracts
export function graphToStrategy(nodes: Node[], edges: Edge[]): any {
  const rootNode = nodes.find((n) => n.type === "strategyRoot");
  if (!rootNode) return null;

  const strategyId = rootNode.data.id || rootNode.id;
  const strategy: any = {
    id: strategyId,
    name: rootNode.data.name || "Unnamed Strategy",
    description: rootNode.data.description || "",
    timeframe: rootNode.data.timeframe || "15m",
    candidate_selection_mode: rootNode.data.candidate_selection_mode || "FIRST_ELIGIBLE",
  };

  // Helper to recursively parse a condition node
  const buildConditionTree = (nodeId: string): any => {
    const node = nodes.find((n) => n.id === nodeId);
    if (!node) return null;

    if (node.type === "condition") {
      const cond: any = {
        type: "CONDITION",
        id: node.id,
        lhs: node.data.lhs || { indicator: "PRICE", symbol: "CANDIDATE" },
        operator: node.data.operator || "GREATER_THAN",
        rhs: node.data.rhs || { type: "NUMBER", value: 0 },
      };
      if (node.data.tolerance !== undefined && node.data.tolerance !== null) {
        cond.tolerance = Number(node.data.tolerance);
      }
      return cond;
    }

    if (node.type === "logicalGroup") {
      const childEdges = edges.filter((e) => e.source === nodeId);
      const childConditions = childEdges
        .map((e) => buildConditionTree(e.target))
        .filter((c) => c !== null);

      return {
        type: node.data.type || "AND",
        conditions: childConditions,
      };
    }

    return null;
  };

  // Find Global Condition Edge
  const globalEdge = edges.find((e) => e.source === rootNode.id && e.sourceHandle === "global");
  if (globalEdge) {
    strategy.global_conditions = buildConditionTree(globalEdge.target);
  }

  // Find Candidate Condition Edge
  const candidateEdge = edges.find((e) => e.source === rootNode.id && e.sourceHandle === "candidate");
  if (candidateEdge) {
    strategy.candidate_conditions = buildConditionTree(candidateEdge.target);
  }

  // Find Action Edge
  const actionEdge = edges.find((e) => e.source === rootNode.id && e.sourceHandle === "action");
  if (actionEdge) {
    const actionNode = nodes.find((n) => n.id === actionEdge.target);
    if (actionNode) {
      const actionData = actionNode.data as any;
      strategy.action = {
        type: "PAPER_TRADE",
        risk_config: {
          max_position_size: Number(actionData.risk_config?.max_position_size ?? 100000),
          stop_loss_pct: Number(actionData.risk_config?.stop_loss_pct ?? 2.5),
          take_profit_pct: Number(actionData.risk_config?.take_profit_pct ?? 5),
          validity_window: Number(actionData.risk_config?.validity_window ?? 5),
        },
      };
    }
  } else {
    // Return empty action to trigger validation error
    strategy.action = {
      type: "PAPER_TRADE",
      risk_config: {
        max_position_size: 0,
        stop_loss_pct: 0,
        take_profit_pct: 0,
        validity_window: 1,
      },
    };
  }

  return strategy;
}

// Converts loaded Strategy JSON back into a visual Node & Edge layout
export function strategyToGraph(strategy: any): { nodes: Node[]; edges: Edge[] } {
  const nodes: Node[] = [];
  const edges: Edge[] = [];

  const rootId = "root";
  nodes.push({
    id: rootId,
    type: "strategyRoot",
    position: { x: 100, y: 300 },
    data: {
      id: strategy.id,
      name: strategy.name,
      description: strategy.description,
      timeframe: strategy.timeframe,
      candidate_selection_mode: strategy.candidate_selection_mode,
    },
  });

  let uniqueIdCounter = 1;
  const nextId = (prefix: string) => `${prefix}-${uniqueIdCounter++}`;

  const traverseCondition = (
    conditionNode: any,
    parentX: number,
    parentY: number,
    parentId: string,
    sourceHandle: string
  ) => {
    if (!conditionNode) return;

    if (conditionNode.type === "CONDITION") {
      const condId = nextId("cond");
      nodes.push({
        id: condId,
        type: "condition",
        position: { x: parentX + 300, y: parentY },
        data: {
          lhs: conditionNode.lhs,
          operator: conditionNode.operator,
          rhs: conditionNode.rhs,
          tolerance: conditionNode.tolerance,
        },
      });

      edges.push({
        id: nextId("edge"),
        source: parentId,
        sourceHandle: sourceHandle,
        target: condId,
        targetHandle: "input",
      });
    } else if (["AND", "OR", "NOT"].includes(conditionNode.type)) {
      const groupId = nextId("group");
      nodes.push({
        id: groupId,
        type: "logicalGroup",
        position: { x: parentX + 300, y: parentY },
        data: {
          type: conditionNode.type,
        },
      });

      edges.push({
        id: nextId("edge"),
        source: parentId,
        sourceHandle: sourceHandle,
        target: groupId,
        targetHandle: "input",
      });

      const children = conditionNode.conditions || [];
      const totalChildren = children.length;
      children.forEach((child: any, idx: number) => {
        // Distribute children vertically
        const offsetMultiplier = idx - (totalChildren - 1) / 2;
        const childY = parentY + offsetMultiplier * 180;
        traverseCondition(child, parentX + 300, childY, groupId, "output");
      });
    }
  };

  // Build Global Condition Graph
  if (strategy.global_conditions) {
    traverseCondition(strategy.global_conditions, 100, 150, rootId, "global");
  }

  // Build Candidate Condition Graph
  if (strategy.candidate_conditions) {
    traverseCondition(strategy.candidate_conditions, 100, 450, rootId, "candidate");
  }

  // Build Action Node
  if (strategy.action) {
    const actionId = "action-node";
    nodes.push({
      id: actionId,
      type: "action",
      position: { x: 400, y: 700 },
      data: {
        type: strategy.action.type,
        risk_config: strategy.action.risk_config,
      },
    });

    edges.push({
      id: "edge-action",
      source: rootId,
      sourceHandle: "action",
      target: actionId,
      targetHandle: "input",
    });
  }

  return { nodes, edges };
}
