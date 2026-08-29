import React from "react";

interface PropertiesPanelProps {
  selectedNode: any;
  onUpdate: (nodeId: string, data: any) => void;
}

export function PropertiesPanel({ selectedNode, onUpdate }: PropertiesPanelProps) {
  if (!selectedNode) {
    return (
      <div className="bg-slate-900 border border-slate-800 rounded-xl p-4 text-slate-400 text-sm font-sans flex items-center justify-center h-full">
        Select a node to edit its properties
      </div>
    );
  }

  const { id, type, data } = selectedNode;

  const handleChange = (field: string, value: any) => {
    onUpdate(id, { ...data, [field]: value });
  };

  const handleNestedChange = (parentField: string, field: string, value: any) => {
    const parentVal = data[parentField] || {};
    onUpdate(id, {
      ...data,
      [parentField]: { ...parentVal, [field]: value },
    });
  };

  const handleParamsChange = (exprField: string, paramField: string, value: any) => {
    const expr = data[exprField] || {};
    const params = expr.params || {};
    onUpdate(id, {
      ...data,
      [exprField]: {
        ...expr,
        params: { ...params, [paramField]: value },
      },
    });
  };

  return (
    <div className="bg-slate-950 border border-slate-800 rounded-xl p-4 text-slate-100 font-sans space-y-4 max-h-[85vh] overflow-y-auto">
      <div className="border-b border-slate-800 pb-2 flex justify-between items-center">
        <h3 className="font-bold text-sm text-indigo-400 tracking-wide uppercase">Properties Editor</h3>
        <span className="text-[10px] bg-slate-800 px-2 py-0.5 rounded font-mono uppercase text-slate-300">{type}</span>
      </div>

      {type === "strategyRoot" && (
        <div className="space-y-3">
          <div>
            <label className="block text-xs font-semibold text-slate-400 mb-1">Strategy Name</label>
            <input
              type="text"
              className="w-full bg-slate-900 border border-slate-800 rounded px-2.5 py-1.5 text-xs focus:outline-none focus:border-indigo-500 text-slate-200"
              value={data.name || ""}
              onChange={(e) => handleChange("name", e.target.value)}
            />
          </div>
          <div>
            <label className="block text-xs font-semibold text-slate-400 mb-1">Description</label>
            <textarea
              className="w-full bg-slate-900 border border-slate-800 rounded px-2.5 py-1.5 text-xs focus:outline-none focus:border-indigo-500 text-slate-200 h-16 resize-none"
              value={data.description || ""}
              onChange={(e) => handleChange("description", e.target.value)}
            />
          </div>
          <div>
            <label className="block text-xs font-semibold text-slate-400 mb-1">Timeframe</label>
            <select
              className="w-full bg-slate-900 border border-slate-800 rounded px-2.5 py-1.5 text-xs focus:outline-none focus:border-indigo-500 text-slate-200"
              value={data.timeframe || "15m"}
              onChange={(e) => handleChange("timeframe", e.target.value)}
            >
              {["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d", "1w"].map((tf) => (
                <option key={tf} value={tf}>{tf}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-xs font-semibold text-slate-400 mb-1">Candidate Selection</label>
            <select
              className="w-full bg-slate-900 border border-slate-800 rounded px-2.5 py-1.5 text-xs focus:outline-none focus:border-indigo-500 text-slate-200"
              value={data.candidate_selection_mode || "FIRST_ELIGIBLE"}
              onChange={(e) => handleChange("candidate_selection_mode", e.target.value)}
            >
              <option value="FIRST_ELIGIBLE">FIRST_ELIGIBLE</option>
            </select>
          </div>
        </div>
      )}

      {type === "logicalGroup" && (
        <div className="space-y-3">
          <div>
            <label className="block text-xs font-semibold text-slate-400 mb-1">Group Type</label>
            <select
              className="w-full bg-slate-900 border border-slate-800 rounded px-2.5 py-1.5 text-xs focus:outline-none focus:border-indigo-500 text-slate-200"
              value={data.type || "AND"}
              onChange={(e) => handleChange("type", e.target.value)}
            >
              <option value="AND">AND</option>
              <option value="OR">OR</option>
              <option value="NOT">NOT</option>
            </select>
          </div>
        </div>
      )}

      {type === "condition" && (
        <div className="space-y-4">
          {/* LHS INDICATOR */}
          <div className="border border-slate-800 p-2.5 rounded bg-slate-900/40">
            <h4 className="text-xs font-bold text-teal-400 uppercase mb-2">Left-Hand Side (LHS)</h4>
            <div className="space-y-2">
              <div>
                <label className="block text-[10px] text-slate-400 mb-0.5">Indicator</label>
                <select
                  className="w-full bg-slate-900 border border-slate-800 rounded px-2 py-1 text-xs focus:outline-none text-slate-200"
                  value={data.lhs?.indicator || "PRICE"}
                  onChange={(e) => handleNestedChange("lhs", "indicator", e.target.value)}
                >
                  {["PRICE", "EMA", "RSI", "MACD", "PIVOT", "VOLUME"].map((ind) => (
                    <option key={ind} value={ind}>{ind}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-[10px] text-slate-400 mb-0.5">Symbol</label>
                <input
                  type="text"
                  placeholder="e.g. CANDIDATE or NIFTY"
                  className="w-full bg-slate-900 border border-slate-800 rounded px-2 py-1 text-xs focus:outline-none text-slate-200"
                  value={data.lhs?.symbol || ""}
                  onChange={(e) => handleNestedChange("lhs", "symbol", e.target.value)}
                />
              </div>
              {["EMA", "RSI", "MACD"].includes(data.lhs?.indicator) && (
                <div>
                  <label className="block text-[10px] text-slate-400 mb-0.5">Period</label>
                  <input
                    type="number"
                    className="w-full bg-slate-900 border border-slate-800 rounded px-2 py-1 text-xs focus:outline-none text-slate-200"
                    value={data.lhs?.params?.period || 14}
                    onChange={(e) => handleParamsChange("lhs", "period", Number(e.target.value))}
                  />
                </div>
              )}
              {data.lhs?.indicator === "PIVOT" && (
                <div>
                  <label className="block text-[10px] text-slate-400 mb-0.5">Pivot Level</label>
                  <select
                    className="w-full bg-slate-900 border border-slate-800 rounded px-2 py-1 text-xs focus:outline-none text-slate-200"
                    value={data.lhs?.params?.level || "P"}
                    onChange={(e) => handleParamsChange("lhs", "level", e.target.value)}
                  >
                    {["P", "R1", "R2", "R3", "S1", "S2", "S3"].map((lvl) => (
                      <option key={lvl} value={lvl}>{lvl}</option>
                    ))}
                  </select>
                </div>
              )}
            </div>
          </div>

          {/* OPERATOR */}
          <div>
            <label className="block text-xs font-semibold text-slate-400 mb-1">Operator</label>
            <select
              className="w-full bg-slate-900 border border-slate-800 rounded px-2.5 py-1.5 text-xs focus:outline-none focus:border-indigo-500 text-slate-200 font-bold"
              value={data.operator || "GREATER_THAN"}
              onChange={(e) => handleChange("operator", e.target.value)}
            >
              {[
                { val: "GREATER_THAN", label: "Greater Than (>)" },
                { val: "LESS_THAN", label: "Less Than (<)" },
                { val: "GREATER_THAN_OR_EQUAL", label: "Greater Than or Equal (>=)" },
                { val: "LESS_THAN_OR_EQUAL", label: "Less Than or Equal (<=)" },
                { val: "EQUALS", label: "Equals (==)" },
                { val: "CROSSES_ABOVE", label: "Crosses Above" },
                { val: "CROSSES_BELOW", label: "Crosses Below" },
                { val: "TOUCHES", label: "Touches" },
                { val: "BETWEEN", label: "Between" },
              ].map((op) => (
                <option key={op.val} value={op.val}>{op.label}</option>
              ))}
            </select>
          </div>

          {/* TOLERANCE (For EQUALS or TOUCHES) */}
          {(data.operator === "EQUALS" || data.operator === "TOUCHES") && (
            <div>
              <label className="block text-xs font-semibold text-slate-400 mb-1">
                Tolerance ({data.operator === "EQUALS" ? "Default 1e-6" : "Default 1e-4"})
              </label>
              <input
                type="number"
                step="any"
                min="0"
                className="w-full bg-slate-900 border border-slate-800 rounded px-2.5 py-1.5 text-xs focus:outline-none focus:border-indigo-500 text-slate-200"
                value={data.tolerance ?? ""}
                placeholder="Auto default tolerance"
                onChange={(e) => handleChange("tolerance", e.target.value === "" ? undefined : Number(e.target.value))}
              />
            </div>
          )}

          {/* RHS TARGET */}
          <div className="border border-slate-800 p-2.5 rounded bg-slate-900/40">
            <h4 className="text-xs font-bold text-teal-400 uppercase mb-2">Right-Hand Side (RHS)</h4>
            <div className="space-y-2">
              <div>
                <label className="block text-[10px] text-slate-400 mb-0.5">Value Type</label>
                <select
                  className="w-full bg-slate-900 border border-slate-800 rounded px-2 py-1 text-xs focus:outline-none text-slate-200"
                  value={data.rhs?.type || "NUMBER"}
                  onChange={(e) => handleNestedChange("rhs", "type", e.target.value)}
                >
                  <option value="NUMBER">Numeric Value</option>
                  <option value="NUMBER_RANGE">Range [Low, High]</option>
                  <option value="INDICATOR">Technical Indicator</option>
                </select>
              </div>

              {data.rhs?.type === "NUMBER" && (
                <div>
                  <label className="block text-[10px] text-slate-400 mb-0.5">Value</label>
                  <input
                    type="number"
                    step="any"
                    className="w-full bg-slate-900 border border-slate-800 rounded px-2 py-1 text-xs focus:outline-none text-slate-200"
                    value={data.rhs?.value ?? 0}
                    onChange={(e) => handleNestedChange("rhs", "value", Number(e.target.value))}
                  />
                </div>
              )}

              {data.rhs?.type === "NUMBER_RANGE" && (
                <div className="grid grid-cols-2 gap-2">
                  <div>
                    <label className="block text-[10px] text-slate-400 mb-0.5">Min</label>
                    <input
                      type="number"
                      step="any"
                      className="w-full bg-slate-900 border border-slate-800 rounded px-2 py-1 text-xs focus:outline-none text-slate-200"
                      value={data.rhs?.range?.[0] ?? 0}
                      onChange={(e) => {
                        const low = Number(e.target.value);
                        const high = data.rhs?.range?.[1] ?? 0;
                        handleNestedChange("rhs", "range", [low, high]);
                      }}
                    />
                  </div>
                  <div>
                    <label className="block text-[10px] text-slate-400 mb-0.5">Max</label>
                    <input
                      type="number"
                      step="any"
                      className="w-full bg-slate-900 border border-slate-800 rounded px-2 py-1 text-xs focus:outline-none text-slate-200"
                      value={data.rhs?.range?.[1] ?? 0}
                      onChange={(e) => {
                        const low = data.rhs?.range?.[0] ?? 0;
                        const high = Number(e.target.value);
                        handleNestedChange("rhs", "range", [low, high]);
                      }}
                    />
                  </div>
                </div>
              )}

              {data.rhs?.type === "INDICATOR" && (
                <div className="space-y-2 border-t border-slate-800 pt-2 mt-2">
                  <div>
                    <label className="block text-[10px] text-slate-400 mb-0.5">Indicator</label>
                    <select
                      className="w-full bg-slate-900 border border-slate-800 rounded px-2 py-1 text-xs focus:outline-none text-slate-200"
                      value={data.rhs?.indicator?.indicator || "PRICE"}
                      onChange={(e) => {
                        const ind = data.rhs?.indicator || {};
                        handleNestedChange("rhs", "indicator", { ...ind, indicator: e.target.value });
                      }}
                    >
                      {["PRICE", "EMA", "RSI", "MACD", "PIVOT", "VOLUME"].map((ind) => (
                        <option key={ind} value={ind}>{ind}</option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label className="block text-[10px] text-slate-400 mb-0.5">Symbol</label>
                    <input
                      type="text"
                      className="w-full bg-slate-900 border border-slate-800 rounded px-2 py-1 text-xs focus:outline-none text-slate-200"
                      value={data.rhs?.indicator?.symbol || ""}
                      onChange={(e) => {
                        const ind = data.rhs?.indicator || {};
                        handleNestedChange("rhs", "indicator", { ...ind, symbol: e.target.value });
                      }}
                    />
                  </div>
                  {["EMA", "RSI", "MACD"].includes(data.rhs?.indicator?.indicator) && (
                    <div>
                      <label className="block text-[10px] text-slate-400 mb-0.5">Period</label>
                      <input
                        type="number"
                        className="w-full bg-slate-900 border border-slate-800 rounded px-2 py-1 text-xs focus:outline-none text-slate-200"
                        value={data.rhs?.indicator?.params?.period || 14}
                        onChange={(e) => {
                          const ind = data.rhs?.indicator || {};
                          const params = ind.params || {};
                          handleNestedChange("rhs", "indicator", {
                            ...ind,
                            params: { ...params, period: Number(e.target.value) },
                          });
                        }}
                      />
                    </div>
                  )}
                  {data.rhs?.indicator?.indicator === "PIVOT" && (
                    <div>
                      <label className="block text-[10px] text-slate-400 mb-0.5">Pivot Level</label>
                      <select
                        className="w-full bg-slate-900 border border-slate-800 rounded px-2 py-1 text-xs focus:outline-none text-slate-200"
                        value={data.rhs?.indicator?.params?.level || "P"}
                        onChange={(e) => {
                          const ind = data.rhs?.indicator || {};
                          const params = ind.params || {};
                          handleNestedChange("rhs", "indicator", {
                            ...ind,
                            params: { ...params, level: e.target.value },
                          });
                        }}
                      >
                        {["P", "R1", "R2", "R3", "S1", "S2", "S3"].map((lvl) => (
                          <option key={lvl} value={lvl}>{lvl}</option>
                        ))}
                      </select>
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {type === "action" && (
        <div className="space-y-3">
          <div>
            <label className="block text-xs font-semibold text-slate-400 mb-1">Action Type</label>
            <select
              className="w-full bg-slate-900 border border-slate-800 rounded px-2.5 py-1.5 text-xs focus:outline-none text-slate-300 font-bold"
              value={data.type || "PAPER_TRADE"}
              disabled
            >
              <option value="PAPER_TRADE">PAPER_TRADE (Simulated)</option>
            </select>
          </div>
          <div>
            <label className="block text-xs font-semibold text-slate-400 mb-1">Max Position Size (₹)</label>
            <input
              type="number"
              className="w-full bg-slate-900 border border-slate-800 rounded px-2.5 py-1.5 text-xs focus:outline-none focus:border-indigo-500 text-slate-200"
              value={data.risk_config?.max_position_size ?? 100000}
              onChange={(e) => handleNestedChange("risk_config", "max_position_size", Number(e.target.value))}
            />
          </div>
          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className="block text-xs font-semibold text-slate-400 mb-1">Stop Loss (%)</label>
              <input
                type="number"
                step="0.1"
                className="w-full bg-slate-900 border border-slate-800 rounded px-2.5 py-1.5 text-xs focus:outline-none focus:border-indigo-500 text-slate-200"
                value={data.risk_config?.stop_loss_pct ?? 2.5}
                onChange={(e) => handleNestedChange("risk_config", "stop_loss_pct", Number(e.target.value))}
              />
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-400 mb-1">Take Profit (%)</label>
              <input
                type="number"
                step="0.1"
                className="w-full bg-slate-900 border border-slate-800 rounded px-2.5 py-1.5 text-xs focus:outline-none focus:border-indigo-500 text-slate-200"
                value={data.risk_config?.take_profit_pct ?? 5.0}
                onChange={(e) => handleNestedChange("risk_config", "take_profit_pct", Number(e.target.value))}
              />
            </div>
          </div>
          <div>
            <label className="block text-xs font-semibold text-slate-400 mb-1">Validity Window (Completed Candles)</label>
            <input
              type="number"
              className="w-full bg-slate-900 border border-slate-800 rounded px-2.5 py-1.5 text-xs focus:outline-none focus:border-indigo-500 text-slate-200"
              value={data.risk_config?.validity_window ?? 5}
              onChange={(e) => handleNestedChange("risk_config", "validity_window", Number(e.target.value))}
            />
          </div>
        </div>
      )}
    </div>
  );
}
