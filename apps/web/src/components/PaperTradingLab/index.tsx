"use client";

import React, { useState, useEffect, useCallback } from "react";
import {
  PaperAccount,
  StrategyRuntime,
  Order,
  Fill,
  PaperPosition,
  KillSwitchStatus,
  StrategyResponse,
  fetchPaperAccounts,
  createPaperAccount,
  fetchPaperRuntimes,
  createPaperRuntime,
  startPaperRuntime,
  pausePaperRuntime,
  resumePaperRuntime,
  stopPaperRuntime,
  stepPaperRuntime,
  fetchPaperOrders,
  cancelPaperOrder,
  fetchPaperPositions,
  fetchPaperFills,
  fetchKillSwitchStatus,
  engageKillSwitch,
  resetKillSwitch,
  getStrategies,
  getSyntheticDatasets,
} from "../../lib/api";
import { RuntimeControlHeader } from "./RuntimeControlHeader";
import { ActivePositionsTable } from "./ActivePositionsTable";
import { OrderBookPanel } from "./OrderBookPanel";
import { AuditTimelineModal } from "./AuditTimelineModal";
import { KillSwitchModal } from "./KillSwitchModal";
import { useAuth } from "@/context/AuthContext";

export const PaperTradingLab: React.FC = () => {
  const { user } = useAuth();
  const isAdmin = user?.role === "ADMIN";

  // State

  const [accounts, setAccounts] = useState<PaperAccount[]>([]);
  const [selectedAccountId, setSelectedAccountId] = useState<string>("");
  const [runtimes, setRuntimes] = useState<StrategyRuntime[]>([]);
  const [selectedRuntimeId, setSelectedRuntimeId] = useState<string>("");
  const [positions, setPositions] = useState<PaperPosition[]>([]);
  const [orders, setOrders] = useState<Order[]>([]);
  const [fills, setFills] = useState<Fill[]>([]);
  const [killSwitch, setKillSwitch] = useState<KillSwitchStatus | null>(null);

  // External data for runtime creation
  const [strategies, setStrategies] = useState<StrategyResponse[]>([]);
  const [datasets, setDatasets] = useState<{ dataset_id: string; display_name: string }[]>([]);

  // Selection & UI modals
  const [inspectedOrder, setInspectedOrder] = useState<Order | null>(null);
  const [isKillSwitchModalOpen, setIsKillSwitchModalOpen] = useState(false);
  const [isNewAccountModalOpen, setIsNewAccountModalOpen] = useState(false);
  const [isNewRuntimeModalOpen, setIsNewRuntimeModalOpen] = useState(false);

  // Form states
  const [newAccountName, setNewAccountName] = useState("");
  const [newAccountBalance, setNewAccountBalance] = useState("100000.00");
  const [newRuntimeStrategyId, setNewRuntimeStrategyId] = useState("");
  const [newRuntimeDatasetId, setNewRuntimeDatasetId] = useState("");

  // Loading & notification states
  const [loading, setLoading] = useState(false);
  const [feedbackMessage, setFeedbackMessage] = useState<{ type: "success" | "error" | "info"; text: string } | null>(null);

  // Selected account & runtime objects
  const currentAccount = accounts.find((a) => a.id === selectedAccountId) || null;
  const currentRuntime = runtimes.find((r) => r.id === selectedRuntimeId) || null;

  // Notification helper
  const showFeedback = (text: string, type: "success" | "error" | "info" = "info") => {
    setFeedbackMessage({ type, text });
    setTimeout(() => {
      setFeedbackMessage(null);
    }, 6000);
  };

  // Initial load
  const loadInitialData = useCallback(async () => {
    try {
      setLoading(true);
      const [accts, rts, ks] = await Promise.all([
        fetchPaperAccounts(),
        fetchPaperRuntimes(),
        fetchKillSwitchStatus().catch(() => null),
      ]);
      setAccounts(accts);
      if (accts.length > 0 && !selectedAccountId) {
        setSelectedAccountId(accts[0].id);
      }
      setRuntimes(rts);
      if (rts.length > 0 && !selectedRuntimeId) {
        setSelectedRuntimeId(rts[0].id);
      }
      setKillSwitch(ks);

      // Load strategies and datasets asynchronously
      getStrategies().then((s) => setStrategies(s)).catch(() => {});
      getSyntheticDatasets().then((d) => {
        setDatasets(d.datasets.map((item: any) => ({ dataset_id: item.dataset_id, display_name: item.display_name })));
      }).catch(() => {});
    } catch (err: any) {
      showFeedback(`Failed to load data: ${err.message || err}`, "error");
    } finally {
      setLoading(false);
    }
  }, [selectedAccountId, selectedRuntimeId]);

  useEffect(() => {
    loadInitialData();
  }, [loadInitialData]);

  // Refresh Account-specific state (positions, orders, fills)
  const refreshAccountData = useCallback(async () => {
    if (!selectedAccountId) return;
    try {
      const [pos, ords, fls, accts, ks] = await Promise.all([
        fetchPaperPositions(selectedAccountId),
        fetchPaperOrders(selectedRuntimeId || undefined),
        fetchPaperFills(selectedAccountId),
        fetchPaperAccounts(),
        fetchKillSwitchStatus().catch(() => null),
      ]);
      setPositions(pos);
      setOrders(ords);
      setFills(fls);
      setAccounts(accts);
      setKillSwitch(ks);
    } catch (err: any) {
      console.error("Failed to refresh account data:", err);
    }
  }, [selectedAccountId, selectedRuntimeId]);

  useEffect(() => {
    refreshAccountData();
  }, [refreshAccountData]);

  // Runtime Controls
  const handleStartRuntime = async () => {
    if (!currentRuntime) return;
    try {
      setLoading(true);
      const updated = await startPaperRuntime(currentRuntime.id);
      setRuntimes((prev) => prev.map((r) => (r.id === updated.id ? updated : r)));
      showFeedback(`Runtime '${updated.id.slice(0, 8)}' is now RUNNING.`, "success");
      await refreshAccountData();
    } catch (err: any) {
      showFeedback(err.message || "Failed to start runtime", "error");
    } finally {
      setLoading(false);
    }
  };

  const handlePauseRuntime = async () => {
    if (!currentRuntime) return;
    try {
      setLoading(true);
      const updated = await pausePaperRuntime(currentRuntime.id);
      setRuntimes((prev) => prev.map((r) => (r.id === updated.id ? updated : r)));
      showFeedback(`Runtime '${updated.id.slice(0, 8)}' PAUSED.`, "info");
      await refreshAccountData();
    } catch (err: any) {
      showFeedback(err.message || "Failed to pause runtime", "error");
    } finally {
      setLoading(false);
    }
  };

  const handleResumeRuntime = async () => {
    if (!currentRuntime) return;
    try {
      setLoading(true);
      const updated = await resumePaperRuntime(currentRuntime.id);
      setRuntimes((prev) => prev.map((r) => (r.id === updated.id ? updated : r)));
      showFeedback(`Runtime '${updated.id.slice(0, 8)}' RESUMED.`, "success");
      await refreshAccountData();
    } catch (err: any) {
      showFeedback(err.message || "Failed to resume runtime", "error");
    } finally {
      setLoading(false);
    }
  };

  const handleStopRuntime = async () => {
    if (!currentRuntime) return;
    try {
      setLoading(true);
      const updated = await stopPaperRuntime(currentRuntime.id);
      setRuntimes((prev) => prev.map((r) => (r.id === updated.id ? updated : r)));
      showFeedback(`Runtime '${updated.id.slice(0, 8)}' STOPPED. Open orders cancelled.`, "info");
      await refreshAccountData();
    } catch (err: any) {
      showFeedback(err.message || "Failed to stop runtime", "error");
    } finally {
      setLoading(false);
    }
  };

  const handleStepRuntime = async (stepCount: number) => {
    if (!currentRuntime) return;
    try {
      setLoading(true);
      const res = await stepPaperRuntime(currentRuntime.id, stepCount);
      showFeedback(
        `Stepped ${res.steps_executed} bars. Generated ${res.intents_created} intents, ${res.fills_executed} fills.`,
        "success"
      );
      // Reload runtime and account data
      const updatedRuntimes = await fetchPaperRuntimes();
      setRuntimes(updatedRuntimes);
      await refreshAccountData();
    } catch (err: any) {
      showFeedback(err.message || "Step execution failed", "error");
    } finally {
      setLoading(false);
    }
  };

  const handleCancelOrder = async (orderId: string) => {
    try {
      setLoading(true);
      await cancelPaperOrder(orderId);
      showFeedback(`Order cancelled successfully.`, "success");
      await refreshAccountData();
    } catch (err: any) {
      showFeedback(err.message || "Failed to cancel order", "error");
    } finally {
      setLoading(false);
    }
  };

  const handleEngageKillSwitch = async (scope: "GLOBAL" | "USER", reason: string) => {
    try {
      setLoading(true);
      await engageKillSwitch(scope, reason);
      showFeedback(`Emergency kill switch engaged (${scope}). All runtimes halted.`, "info");
      const ks = await fetchKillSwitchStatus();
      setKillSwitch(ks);
      const updatedRuntimes = await fetchPaperRuntimes();
      setRuntimes(updatedRuntimes);
      await refreshAccountData();
    } catch (err: any) {
      showFeedback(err.message || "Failed to engage kill switch", "error");
    } finally {
      setLoading(false);
    }
  };

  const handleResetKillSwitch = async (scope: "GLOBAL" | "USER", reason: string) => {
    try {
      setLoading(true);
      await resetKillSwitch(scope, reason);
      showFeedback(`Emergency kill switch reset (${scope}).`, "success");
      const ks = await fetchKillSwitchStatus();
      setKillSwitch(ks);
      await refreshAccountData();
    } catch (err: any) {
      showFeedback(err.message || "Failed to reset kill switch", "error");
    } finally {
      setLoading(false);
    }
  };

  const handleCreateAccount = async () => {
    if (!newAccountName) return;
    try {
      setLoading(true);
      const acct = await createPaperAccount(newAccountName, newAccountBalance);
      setAccounts((prev) => [...prev, acct]);
      setSelectedAccountId(acct.id);
      setIsNewAccountModalOpen(false);
      setNewAccountName("");
      showFeedback(`Created Paper Account '${acct.name}'.`, "success");
    } catch (err: any) {
      showFeedback(err.message || "Failed to create paper account", "error");
    } finally {
      setLoading(false);
    }
  };

  const handleCreateRuntime = async () => {
    if (!newRuntimeStrategyId || !selectedAccountId || !newRuntimeDatasetId) return;
    try {
      setLoading(true);
      const rt = await createPaperRuntime({
        strategy_id: newRuntimeStrategyId,
        account_id: selectedAccountId,
        dataset_id: newRuntimeDatasetId,
      });
      setRuntimes((prev) => [rt, ...prev]);
      setSelectedRuntimeId(rt.id);
      setIsNewRuntimeModalOpen(false);
      showFeedback(`Created Strategy Runtime in DRAFT status.`, "success");
    } catch (err: any) {
      showFeedback(err.message || "Failed to create runtime", "error");
    } finally {
      setLoading(false);
    }
  };

  // Portfolio metrics
  const totalCash = currentAccount ? Number(currentAccount.total_cash) : 0;
  const reservedCash = currentAccount ? Number(currentAccount.reserved_cash) : 0;
  const availableCash = currentAccount ? Number(currentAccount.available_cash) : 0;
  const totalUnrealizedPnl = positions.reduce((acc, p) => acc + Number(p.unrealized_pnl), 0);
  const totalRealizedPnl = positions.reduce((acc, p) => acc + Number(p.net_realized_pnl), 0);
  const portfolioNetValue = totalCash + totalUnrealizedPnl;

  return (
    <div className="space-y-6 max-w-7xl mx-auto px-4 sm:px-6 py-6 font-sans text-slate-100">
      {/* Top Header & Account Overview */}
      <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-4 bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-lg">
        <div>
          <h1 className="text-xl sm:text-2xl font-black tracking-tight text-white flex items-center gap-2.5">
            <span className="p-1.5 rounded-lg bg-indigo-600/20 text-indigo-400 border border-indigo-500/30">
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
              </svg>
            </span>
            Paper Trading Runtime & OMS
          </h1>
          <p className="text-xs text-slate-400 mt-1">
            Deterministic paper execution engine with pre-trade risk controls and immutable double-entry accounting.
          </p>
        </div>

        {/* Account and Runtime Selectors */}
        <div className="flex flex-wrap items-center gap-3">
          {/* Account Dropdown */}
          <div className="flex items-center gap-2">
            <select
              id="account-selector"
              aria-label="Select paper account"
              className="bg-slate-950 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-indigo-500"
              value={selectedAccountId}
              onChange={(e) => setSelectedAccountId(e.target.value)}
            >
              {accounts.length === 0 ? (
                <option value="">No Paper Accounts</option>
              ) : (
                accounts.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name} (₹{Number(a.available_cash).toLocaleString()})
                  </option>
                ))
              )}
            </select>
            <button
              id="create-account-btn"
              onClick={() => setIsNewAccountModalOpen(true)}
              className="px-2.5 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-semibold rounded-lg border border-slate-700 transition-colors"
              title="Create new paper account"
            >
              + Account
            </button>
          </div>

          {/* Runtime Dropdown */}
          <div className="flex items-center gap-2">
            <select
              id="runtime-selector"
              aria-label="Select strategy runtime"
              className="bg-slate-950 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-indigo-500 max-w-[200px] truncate"
              value={selectedRuntimeId}
              onChange={(e) => setSelectedRuntimeId(e.target.value)}
            >
              {runtimes.length === 0 ? (
                <option value="">No Runtimes Active</option>
              ) : (
                runtimes.map((r) => (
                  <option key={r.id} value={r.id}>
                    [{r.status}] {r.dataset_id} ({r.id.slice(0, 8)})
                  </option>
                ))
              )}
            </select>
            <button
              id="create-runtime-btn"
              onClick={() => setIsNewRuntimeModalOpen(true)}
              className="px-2.5 py-1.5 bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-semibold rounded-lg shadow transition-colors"
              title="Instantiate strategy runtime"
            >
              + Runtime
            </button>
          </div>
        </div>
      </div>

      {/* Account Portfolio Financial Metrics */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
        <div className="bg-slate-900 border border-slate-800 rounded-xl p-3.5">
          <span className="text-[10px] uppercase font-bold tracking-wider text-slate-400">Available Cash</span>
          <div id="account-available-cash" className="text-lg font-bold text-white mt-0.5">
            ₹{availableCash.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
          </div>
          <span className="text-[10px] text-slate-400">Unreserved balance</span>
        </div>

        <div className="bg-slate-900 border border-slate-800 rounded-xl p-3.5">
          <span className="text-[10px] uppercase font-bold tracking-wider text-slate-400">Reserved Cash</span>
          <div id="account-reserved-cash" className="text-lg font-bold text-amber-400 mt-0.5">
            ₹{reservedCash.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
          </div>
          <span className="text-[10px] text-slate-400">Open order collateral</span>
        </div>

        <div className="bg-slate-900 border border-slate-800 rounded-xl p-3.5">
          <span className="text-[10px] uppercase font-bold tracking-wider text-slate-400">Total Cash</span>
          <div id="account-total-cash" className="text-lg font-bold text-slate-200 mt-0.5">
            ₹{totalCash.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
          </div>
          <span className="text-[10px] text-slate-400">Settled cash ledger</span>
        </div>

        <div className="bg-slate-900 border border-slate-800 rounded-xl p-3.5">
          <span className="text-[10px] uppercase font-bold tracking-wider text-slate-400">Unrealized P&L</span>
          <div
            id="account-unrealized-pnl"
            className={`text-lg font-bold mt-0.5 ${totalUnrealizedPnl >= 0 ? "text-emerald-400" : "text-rose-400"}`}
          >
            ₹{totalUnrealizedPnl.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
          </div>
          <span className="text-[10px] text-slate-400">Mark-to-market</span>
        </div>

        <div className="bg-slate-900 border border-slate-800 rounded-xl p-3.5">
          <span className="text-[10px] uppercase font-bold tracking-wider text-slate-400">Realized P&L</span>
          <div
            id="account-realized-pnl"
            className={`text-lg font-bold mt-0.5 ${totalRealizedPnl >= 0 ? "text-emerald-400" : "text-rose-400"}`}
          >
            ₹{totalRealizedPnl.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
          </div>
          <span className="text-[10px] text-slate-400">Net closed P&L</span>
        </div>

        <div className="bg-slate-900 border border-slate-800 rounded-xl p-3.5">
          <span className="text-[10px] uppercase font-bold tracking-wider text-slate-400">Portfolio Net Value</span>
          <div id="portfolio-net-value" className="text-lg font-black text-indigo-300 mt-0.5">
            ₹{portfolioNetValue.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
          </div>
          <span className="text-[10px] text-slate-400">Cash + Open MTM</span>
        </div>
      </div>

      {/* Feedback Alert */}
      {feedbackMessage && (
        <div
          className={`p-3 rounded-lg text-xs font-medium border flex items-center justify-between ${
            feedbackMessage.type === "error"
              ? "bg-rose-950/60 border-rose-700 text-rose-300"
              : feedbackMessage.type === "success"
              ? "bg-emerald-950/60 border-emerald-700 text-emerald-300"
              : "bg-blue-950/60 border-blue-700 text-blue-300"
          }`}
        >
          <span>{feedbackMessage.text}</span>
          <button onClick={() => setFeedbackMessage(null)} className="text-slate-400 hover:text-white ml-2">
            ✕
          </button>
        </div>
      )}

      {/* Runtime Control Header (Simulation Banner, Status, Stepper, Kill Switch Button) */}
      <RuntimeControlHeader
        runtime={currentRuntime}
        killSwitch={killSwitch}
        onStart={handleStartRuntime}
        onPause={handlePauseRuntime}
        onResume={handleResumeRuntime}
        onStop={handleStopRuntime}
        onStep={handleStepRuntime}
        onOpenKillSwitch={() => setIsKillSwitchModalOpen(true)}
        loading={loading}
      />

      {/* Active Positions Table */}
      <ActivePositionsTable positions={positions} />

      {/* Order Book & Fills Panel */}
      <OrderBookPanel
        orders={orders}
        fills={fills}
        onCancelOrder={handleCancelOrder}
        onInspectOrder={(ord) => setInspectedOrder(ord)}
      />

      {/* Audit Timeline Modal */}
      {inspectedOrder && (
        <AuditTimelineModal
          order={inspectedOrder}
          onClose={() => setInspectedOrder(null)}
        />
      )}

      {/* Emergency Kill Switch Modal */}
      {isKillSwitchModalOpen && (
        <KillSwitchModal
          status={killSwitch}
          onEngage={handleEngageKillSwitch}
          onReset={handleResetKillSwitch}
          onClose={() => setIsKillSwitchModalOpen(false)}
          isAdmin={isAdmin}
        />
      )}


      {/* Create Account Modal */}
      {isNewAccountModalOpen && (
        <div className="fixed inset-0 z-50 bg-slate-950/80 flex items-center justify-center p-4">
          <div className="bg-slate-900 border border-slate-800 rounded-xl max-w-sm w-full p-5 space-y-4 shadow-2xl">
            <h3 className="text-sm font-bold text-white">Create New Paper Account</h3>
            <div>
              <label className="block text-xs text-slate-400 mb-1">Account Name</label>
              <input
                id="new-account-name-input"
                type="text"
                placeholder="e.g. Alpha Momentum Paper"
                className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-indigo-500"
                value={newAccountName}
                onChange={(e) => setNewAccountName(e.target.value)}
              />
            </div>
            <div>
              <label className="block text-xs text-slate-400 mb-1">Initial Balance (₹)</label>
              <input
                id="new-account-balance-input"
                type="number"
                step="1000"
                className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-indigo-500"
                value={newAccountBalance}
                onChange={(e) => setNewAccountBalance(e.target.value)}
              />
            </div>
            <div className="flex justify-end gap-2 pt-2">
              <button
                onClick={() => setIsNewAccountModalOpen(false)}
                className="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs rounded"
              >
                Cancel
              </button>
              <button
                id="submit-create-account-btn"
                onClick={handleCreateAccount}
                disabled={!newAccountName || loading}
                className="px-4 py-1.5 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white text-xs font-semibold rounded shadow"
              >
                Create Account
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Create Runtime Modal */}
      {isNewRuntimeModalOpen && (
        <div className="fixed inset-0 z-50 bg-slate-950/80 flex items-center justify-center p-4">
          <div className="bg-slate-900 border border-slate-800 rounded-xl max-w-md w-full p-5 space-y-4 shadow-2xl">
            <h3 className="text-sm font-bold text-white">Instantiate Strategy Runtime</h3>
            <p className="text-xs text-slate-400">
              Binds a strategy definition and deterministic paper execution engine to a dataset.
            </p>

            <div>
              <label className="block text-xs text-slate-400 mb-1">Select Strategy</label>
              <select
                id="runtime-strategy-select"
                className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-xs text-slate-200 focus:outline-none focus:border-indigo-500"
                value={newRuntimeStrategyId}
                onChange={(e) => setNewRuntimeStrategyId(e.target.value)}
              >
                <option value="">-- Select Strategy --</option>
                {strategies.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name} ({s.timeframe})
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label className="block text-xs text-slate-400 mb-1">Select Packaged Dataset</label>
              <select
                id="runtime-dataset-select"
                className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-xs text-slate-200 focus:outline-none focus:border-indigo-500"
                value={newRuntimeDatasetId}
                onChange={(e) => setNewRuntimeDatasetId(e.target.value)}
              >
                <option value="">-- Select Dataset --</option>
                {datasets.map((d) => (
                  <option key={d.dataset_id} value={d.dataset_id}>
                    {d.display_name} ({d.dataset_id})
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label className="block text-xs text-slate-400 mb-1">Assigned Paper Account</label>
              <input
                type="text"
                disabled
                className="w-full bg-slate-950/50 border border-slate-800 rounded px-3 py-1.5 text-xs text-slate-400"
                value={currentAccount ? `${currentAccount.name} (${currentAccount.currency})` : "No account selected"}
              />
            </div>

            <div className="flex justify-end gap-2 pt-2">
              <button
                onClick={() => setIsNewRuntimeModalOpen(false)}
                className="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs rounded"
              >
                Cancel
              </button>
              <button
                id="submit-create-runtime-btn"
                onClick={handleCreateRuntime}
                disabled={!newRuntimeStrategyId || !newRuntimeDatasetId || !selectedAccountId || loading}
                className="px-4 py-1.5 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white text-xs font-semibold rounded shadow"
              >
                Instantiate Runtime
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
