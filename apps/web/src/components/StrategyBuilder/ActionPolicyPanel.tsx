"use client";

import React, { useState } from "react";
import { StrategyActionPolicy, createActionPolicy } from "../../lib/api";

interface ActionPolicyPanelProps {
  policies?: StrategyActionPolicy[];
  selectedPolicyId?: string | null;
  onSelectPolicy?: (policyId: string) => void;
  onPolicyCreated?: (policy: StrategyActionPolicy) => void;
}

export const ActionPolicyPanel: React.FC<ActionPolicyPanelProps> = ({
  policies = [],
  selectedPolicyId,
  onSelectPolicy,
  onPolicyCreated,
}) => {
  const [name, setName] = useState("Default Paper Action Policy");
  const [allowedOrderTypes, setAllowedOrderTypes] = useState<string[]>(["MARKET", "LIMIT"]);
  const [allowedTimeInForce, setAllowedTimeInForce] = useState<string[]>(["DAY", "IOC", "FOK"]);
  const [sizingModel, setSizingModel] = useState<"FIXED_QUANTITY" | "PCT_EQUITY">("FIXED_QUANTITY");
  const [defaultQuantity, setDefaultQuantity] = useState<number>(10);
  const [pctEquity, setPctEquity] = useState<number>(2.0);
  const [maxPositionSize, setMaxPositionSize] = useState<number>(100000);
  const [maxSlippageBps, setMaxSlippageBps] = useState<number>(50);
  const [isCreating, setIsCreating] = useState(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);

  const toggleOrderType = (type: string) => {
    setAllowedOrderTypes((prev) =>
      prev.includes(type) ? prev.filter((t) => t !== type) : [...prev, type]
    );
  };

  const toggleTimeInForce = (tif: string) => {
    setAllowedTimeInForce((prev) =>
      prev.includes(tif) ? prev.filter((t) => t !== tif) : [...prev, tif]
    );
  };

  const handleSavePolicy = async () => {
    try {
      setIsCreating(true);
      setStatusMessage(null);

      const payload = {
        name,
        payload: {
          allowed_order_types: allowedOrderTypes,
          allowed_time_in_force: allowedTimeInForce,
          sizing_model: sizingModel,
          default_quantity: sizingModel === "FIXED_QUANTITY" ? defaultQuantity : undefined,
          pct_equity: sizingModel === "PCT_EQUITY" ? pctEquity : undefined,
          max_position_size: maxPositionSize,
          max_slippage_bps: maxSlippageBps,
          execution_mode: "PAPER",
        },
        is_default: policies.length === 0,
      };

      const newPolicy = await createActionPolicy(payload);
      if (onPolicyCreated) {
        onPolicyCreated(newPolicy);
      }
      if (onSelectPolicy) {
        onSelectPolicy(newPolicy.id);
      }
      setStatusMessage("Action policy created successfully!");
    } catch (err: any) {
      setStatusMessage(`Failed to create policy: ${err.message || err}`);
    } finally {
      setIsCreating(false);
    }
  };

  return (
    <div className="bg-slate-900 border border-slate-800 rounded-xl p-4 space-y-4 text-slate-200">
      {/* Header and Badge */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800 pb-3">
        <div>
          <h3 className="text-sm font-bold text-slate-100 uppercase tracking-wider">Strategy Action Policy</h3>
          <p className="text-xs text-slate-400">Order generation, sizing rules, and execution constraints</p>
        </div>
        <span
          id="action-policy-paper-badge"
          className="px-2.5 py-1 text-[11px] font-bold rounded-full bg-amber-950 text-amber-300 border border-amber-600/60 flex items-center gap-1.5"
        >
          <span className="w-1.5 h-1.5 rounded-full bg-amber-400"></span>
          PAPER EXECUTION ONLY
        </span>
      </div>

      {/* Select Existing Policy */}
      {policies.length > 0 && (
        <div className="space-y-1.5">
          <label className="block text-xs font-semibold text-slate-300">Active Action Policy</label>
          <select
            id="policy-selector"
            className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-xs focus:outline-none focus:border-indigo-500 text-slate-200"
            value={selectedPolicyId || ""}
            onChange={(e) => onSelectPolicy && onSelectPolicy(e.target.value)}
          >
            {policies.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} (v{p.version}) {p.is_active ? "— Active" : ""}
              </option>
            ))}
          </select>
        </div>
      )}

      {/* Configuration Form */}
      <div className="space-y-3 bg-slate-950/60 p-3.5 rounded-lg border border-slate-800/80">
        <h4 className="text-xs font-semibold text-slate-300 uppercase tracking-wider">Policy Configuration</h4>

        <div>
          <label className="block text-xs text-slate-400 mb-1">Policy Name</label>
          <input
            id="policy-name-input"
            type="text"
            className="w-full bg-slate-900 border border-slate-800 rounded px-3 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-indigo-500"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </div>

        {/* Allowed Order Types */}
        <div>
          <label className="block text-xs text-slate-400 mb-1.5">Allowed Order Types</label>
          <div className="flex gap-2">
            {["MARKET", "LIMIT"].map((type) => (
              <button
                key={type}
                type="button"
                onClick={() => toggleOrderType(type)}
                className={`px-3 py-1.5 text-xs font-semibold rounded border transition-colors ${
                  allowedOrderTypes.includes(type)
                    ? "bg-indigo-950 text-indigo-300 border-indigo-600"
                    : "bg-slate-900 text-slate-500 border-slate-800 hover:border-slate-700"
                }`}
              >
                {type}
              </button>
            ))}
          </div>
          <p className="text-[10px] text-slate-500 mt-1">
            Note: Stop orders are disabled in 6A due to candle-level intra-bar execution indeterminacy.
          </p>
        </div>

        {/* Allowed Time In Force */}
        <div>
          <label className="block text-xs text-slate-400 mb-1.5">Allowed Time In Force (TIF)</label>
          <div className="flex gap-2">
            {["DAY", "IOC", "FOK"].map((tif) => (
              <button
                key={tif}
                type="button"
                onClick={() => toggleTimeInForce(tif)}
                className={`px-3 py-1.5 text-xs font-semibold rounded border transition-colors ${
                  allowedTimeInForce.includes(tif)
                    ? "bg-indigo-950 text-indigo-300 border-indigo-600"
                    : "bg-slate-900 text-slate-500 border-slate-800 hover:border-slate-700"
                }`}
              >
                {tif}
              </button>
            ))}
          </div>
        </div>

        {/* Sizing Model */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div>
            <label className="block text-xs text-slate-400 mb-1">Sizing Model</label>
            <select
              className="w-full bg-slate-900 border border-slate-800 rounded px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-indigo-500"
              value={sizingModel}
              onChange={(e) => setSizingModel(e.target.value as any)}
            >
              <option value="FIXED_QUANTITY">Fixed Quantity (Shares/Units)</option>
              <option value="PCT_EQUITY">Percent of Account Equity (%)</option>
            </select>
          </div>

          <div>
            {sizingModel === "FIXED_QUANTITY" ? (
              <>
                <label className="block text-xs text-slate-400 mb-1">Default Quantity</label>
                <input
                  type="number"
                  min="1"
                  className="w-full bg-slate-900 border border-slate-800 rounded px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-indigo-500"
                  value={defaultQuantity}
                  onChange={(e) => setDefaultQuantity(Math.max(1, Number(e.target.value)))}
                />
              </>
            ) : (
              <>
                <label className="block text-xs text-slate-400 mb-1">Equity Allocation (%)</label>
                <input
                  type="number"
                  step="0.1"
                  min="0.1"
                  max="100"
                  className="w-full bg-slate-900 border border-slate-800 rounded px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-indigo-500"
                  value={pctEquity}
                  onChange={(e) => setPctEquity(Number(e.target.value))}
                />
              </>
            )}
          </div>
        </div>

        {/* Max Position Size and Slippage */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div>
            <label className="block text-xs text-slate-400 mb-1">Max Position Size (₹)</label>
            <input
              type="number"
              min="1000"
              className="w-full bg-slate-900 border border-slate-800 rounded px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-indigo-500"
              value={maxPositionSize}
              onChange={(e) => setMaxPositionSize(Number(e.target.value))}
            />
          </div>

          <div>
            <label className="block text-xs text-slate-400 mb-1">Max Slippage (bps)</label>
            <input
              type="number"
              min="0"
              max="500"
              className="w-full bg-slate-900 border border-slate-800 rounded px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-indigo-500"
              value={maxSlippageBps}
              onChange={(e) => setMaxSlippageBps(Number(e.target.value))}
            />
          </div>
        </div>

        {/* Save button */}
        <div className="pt-2 flex items-center justify-between">
          <button
            id="save-action-policy-btn"
            type="button"
            disabled={isCreating || allowedOrderTypes.length === 0}
            onClick={handleSavePolicy}
            className="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white text-xs font-semibold rounded shadow transition-colors"
          >
            {isCreating ? "Saving..." : "Save New Action Policy"}
          </button>
          {statusMessage && (
            <span className="text-xs text-slate-300 font-mono">{statusMessage}</span>
          )}
        </div>
      </div>
    </div>
  );
};
