import { describe, it, expect } from "vitest";
import { graphToStrategy, strategyToGraph } from "../src/lib/graph";
import { Node, Edge } from "@xyflow/react";

describe("Graph Conversion Library", () => {
  const exampleStrategy = {
    id: "7b5ef35b-1175-430c-ab23-f22287955c45",
    name: "Nifty RSI Touch",
    description: "NIFTY and candidate conditions",
    timeframe: "15m",
    candidate_selection_mode: "FIRST_ELIGIBLE",
    global_conditions: {
      type: "CONDITION",
      lhs: { indicator: "PRICE", symbol: "NIFTY" },
      operator: "GREATER_THAN",
      rhs: { type: "NUMBER", value: 22000 },
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

  it("should convert a strategy payload into a graph of nodes and edges", () => {
    const { nodes, edges } = strategyToGraph(exampleStrategy);

    const rootNode = nodes.find((n) => n.type === "strategyRoot");
    expect(rootNode).toBeDefined();
    expect(rootNode?.data.name).toBe("Nifty RSI Touch");

    const actionNode = nodes.find((n) => n.type === "action");
    expect(actionNode).toBeDefined();
    expect((actionNode?.data as any).risk_config.max_position_size).toBe(100000);

    const conditionNodes = nodes.filter((n) => n.type === "condition");
    expect(conditionNodes.length).toBe(2); // NIFTY price, candidate RSI
  });

  it("should round-trip serialize a strategy payload back to its original JSON structure", () => {
    const { nodes, edges } = strategyToGraph(exampleStrategy);
    const serialized = graphToStrategy(nodes, edges);

    expect(serialized.id).toBe(exampleStrategy.id);
    expect(serialized.name).toBe(exampleStrategy.name);
    expect(serialized.timeframe).toBe(exampleStrategy.timeframe);
    expect(serialized.global_conditions.type).toBe("CONDITION");
    expect(serialized.global_conditions.lhs.symbol).toBe("NIFTY");
    expect(serialized.candidate_conditions.type).toBe("AND");
    expect(serialized.candidate_conditions.conditions[0].lhs.indicator).toBe("RSI");
    expect(serialized.action.risk_config.stop_loss_pct).toBe(2.5);
  });
});
