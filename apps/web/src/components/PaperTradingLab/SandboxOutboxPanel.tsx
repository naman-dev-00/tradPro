"use client";

import React, { useState } from "react";
import {
  SandboxReadinessResponse,
  SubmissionOutboxResponse,
  ReconciliationRecordResponse,
  ProviderInstrumentMappingResponse,
  Order,
} from "../../lib/api";

interface Props {
  readiness: SandboxReadinessResponse | null;
  outboxItems: SubmissionOutboxResponse[];
  reconciliations: ReconciliationRecordResponse[];
  mappings: ProviderInstrumentMappingResponse[];
  reconciliationOrders: Order[];
  isAdmin: boolean;
  onRefresh: () => void;
  onResolveReconciliation: (
    orderId: string,
    type: "PLACE_CONFIRMED" | "PLACE_REJECTED" | "CANCEL_CONFIRMED" | "CANCEL_NOT_CONFIRMED",
    notes: string,
    ref?: string
  ) => Promise<void>;
  onVerifyMapping: (mappingId: string, status: "VERIFIED" | "REJECTED", reason: string) => Promise<void>;
}

export const SandboxOutboxPanel: React.FC<Props> = ({
  readiness,
  outboxItems,
  reconciliations,
  mappings,
  reconciliationOrders,
  isAdmin,
  onRefresh,
  onResolveReconciliation,
  onVerifyMapping,
}) => {
  const [selectedOrderToResolve, setSelectedOrderToResolve] = useState<string | null>(null);
  const [resolutionType, setResolutionType] = useState<"PLACE_CONFIRMED" | "PLACE_REJECTED" | "CANCEL_CONFIRMED" | "CANCEL_NOT_CONFIRMED">("PLACE_CONFIRMED");
  const [providerOrderRef, setProviderOrderRef] = useState("");
  const [resolutionNotes, setResolutionNotes] = useState("");
  const [resolving, setResolving] = useState(false);

  const [verifyMappingId, setVerifyMappingId] = useState<string | null>(null);
  const [verifyReason, setVerifyReason] = useState("");
  const [verifying, setVerifying] = useState(false);

  const handleResolveSubmit = async () => {
    if (!selectedOrderToResolve || !resolutionNotes) return;
    try {
      setResolving(true);
      await onResolveReconciliation(
        selectedOrderToResolve,
        resolutionType,
        resolutionNotes,
        providerOrderRef || undefined
      );
      setSelectedOrderToResolve(null);
      setResolutionNotes("");
      setProviderOrderRef("");
    } catch (err) {
      console.error(err);
    } finally {
      setResolving(false);
    }
  };

  const handleVerifySubmit = async (status: "VERIFIED" | "REJECTED") => {
    if (!verifyMappingId || !verifyReason) return;
    try {
      setVerifying(true);
      await onVerifyMapping(verifyMappingId, status, verifyReason);
      setVerifyMappingId(null);
      setVerifyReason("");
    } catch (err) {
      console.error(err);
    } finally {
      setVerifying(false);
    }
  };

  return (
    <div className="space-y-6">
      {/* 1. Runtime-Specific Readiness Panel */}
      <div className="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-lg space-y-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2.5">
            <span className="p-1.5 rounded-lg bg-indigo-600/20 text-indigo-400 border border-indigo-500/30">
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
            </span>
            <h3 className="text-sm font-bold text-white tracking-wide">Runtime Sandbox Readiness</h3>
          </div>
          <button
            onClick={onRefresh}
            className="text-xs px-2.5 py-1 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded border border-slate-700 transition-colors"
          >
            Refresh Gates
          </button>
        </div>

        {readiness ? (
          <div className="space-y-4">
            {/* Overall Submission Readiness Status */}
            <div className="flex items-center justify-between p-3 rounded-lg bg-slate-950 border border-slate-800">
              <div className="flex items-center gap-3">
                <span
                  id="sandbox-readiness-indicator"
                  className={`w-3 h-3 rounded-full ${
                    readiness.ready_for_submission ? "bg-emerald-500" : "bg-rose-500"
                  }`}
                />
                <span className="text-xs font-semibold text-slate-200">
                  {readiness.ready_for_submission
                    ? "Ready for Order Submission"
                    : "Submission Blocked (Safety Fail-Closed)"}
                </span>
              </div>
              <div className="flex items-center gap-3 text-xs">
                <span className={`font-mono px-2 py-0.5 rounded border ${
                  readiness.cancel_available
                    ? "bg-emerald-950/60 text-emerald-300 border-emerald-800"
                    : "bg-rose-950/60 text-rose-300 border-rose-800"
                }`}>
                  Cancel: {readiness.cancel_available ? "AVAILABLE" : "UNAVAILABLE"}
                </span>
                <span className={`font-mono px-2 py-0.5 rounded border ${
                  readiness.network_enabled
                    ? "bg-emerald-950/60 text-emerald-300 border-emerald-800"
                    : "bg-amber-950/60 text-amber-300 border-amber-800"
                }`}>
                  Network: {readiness.network_enabled ? "ENABLED" : "OFFLINE"}
                </span>
              </div>
            </div>

            {/* 12 Individual Gate Badges */}
            <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-2 text-[11px]">
              {[
                { key: "environment_allowed", label: "Env Allowed", val: readiness.environment_allowed },
                { key: "network_enabled", label: "Network Active", val: readiness.network_enabled },
                { key: "credential_present", label: "Token Present", val: readiness.credential_present },
                { key: "owner_matches", label: "Owner Match", val: readiness.owner_matches },
                { key: "provider_matches", label: "Upstox Config", val: readiness.provider_matches },
                { key: "mapping_verified", label: "Mapping Verified", val: readiness.mapping_verified },
                { key: "mapping_unexpired", label: "Mapping Valid", val: readiness.mapping_unexpired },
                { key: "global_kill_switch_clear", label: "Global KS Clear", val: readiness.global_kill_switch_clear },
                { key: "user_kill_switch_clear", label: "User KS Clear", val: readiness.user_kill_switch_clear },
                { key: "worker_available", label: "Worker Heartbeat", val: readiness.worker_available },
                { key: "ready_for_submission", label: "Submission Gate", val: readiness.ready_for_submission },
                { key: "cancel_available", label: "Cancel Gate", val: readiness.cancel_available },
              ].map((g) => (
                <div
                  key={g.key}
                  id={`gate-${g.key}`}
                  className={`p-2 rounded border flex items-center justify-between ${
                    g.val
                      ? "bg-emerald-950/20 border-emerald-800/40 text-emerald-300"
                      : "bg-rose-950/20 border-rose-800/40 text-rose-300"
                  }`}
                >
                  <span className="truncate">{g.label}</span>
                  <span className="font-bold ml-1">{g.val ? "✓" : "✗"}</span>
                </div>
              ))}
            </div>

            {readiness.reasons.length > 0 && (
              <div className="p-2.5 rounded bg-rose-950/30 border border-rose-800/50 text-xs text-rose-300 space-y-1">
                <span className="font-semibold block text-[11px] uppercase tracking-wider text-rose-400">
                  Block Reasons:
                </span>
                {readiness.reasons.map((r, i) => (
                  <div key={i} className="flex items-start gap-1.5 text-[11px]">
                    <span className="text-rose-400">•</span>
                    <span>{r}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        ) : (
          <div className="text-xs text-slate-500 italic p-3 text-center">
            Select an active strategy runtime to inspect its sandbox readiness gates.
          </div>
        )}
      </div>

      {/* 2. Reconciliation Required Alert Banner & Manual Resolution */}
      {reconciliationOrders.length > 0 && (
        <div id="reconciliation-alert-panel" className="bg-rose-950/40 border border-rose-600 rounded-xl p-5 space-y-4 shadow-xl">
          <div className="flex items-center gap-3">
            <span className="p-2 rounded-lg bg-rose-900/40 text-rose-300 border border-rose-700/50">
              <svg className="w-5 h-5 animate-pulse" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
              </svg>
            </span>
            <div>
              <h4 className="text-sm font-bold text-rose-200">
                RECONCILIATION REQUIRED ({reconciliationOrders.length} Order{reconciliationOrders.length > 1 ? "s" : ""})
              </h4>
              <p className="text-xs text-rose-300/80">
                Network timeout, ambiguous HTTP response, or unconfirmed cancellation detected. Automated retries halted to prevent double transmission.
              </p>
            </div>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-xs text-left text-slate-300">
              <thead className="text-[11px] text-slate-400 uppercase bg-slate-950/60 border-b border-rose-900/40">
                <tr>
                  <th className="py-2 px-3">Order ID</th>
                  <th className="py-2 px-3">Side</th>
                  <th className="py-2 px-3">Qty</th>
                  <th className="py-2 px-3">Status</th>
                  <th className="py-2 px-3 text-right">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-rose-950/60">
                {reconciliationOrders.map((ord) => (
                  <tr key={ord.id} className="hover:bg-rose-950/20">
                    <td className="py-2 px-3 font-mono">{ord.id.slice(0, 10)}...</td>
                    <td className="py-2 px-3 font-semibold">{ord.side}</td>
                    <td className="py-2 px-3">{ord.quantity}</td>
                    <td className="py-2 px-3">
                      <span className="px-2 py-0.5 rounded text-[10px] bg-rose-900/60 text-rose-200 border border-rose-700">
                        {ord.status}
                      </span>
                    </td>
                    <td className="py-2 px-3 text-right">
                      <button
                        onClick={() => setSelectedOrderToResolve(ord.id)}
                        className="px-2.5 py-1 bg-rose-800 hover:bg-rose-700 text-white rounded text-xs transition-colors"
                      >
                        Resolve...
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Manual Resolution Modal / Form */}
          {selectedOrderToResolve && (
            <div className="bg-slate-900 border border-rose-700 rounded-lg p-4 space-y-3 mt-3">
              <h5 className="text-xs font-bold text-white uppercase tracking-wider">
                Resolve Order {selectedOrderToResolve.slice(0, 10)}...
              </h5>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <label className="block text-[11px] text-slate-400 mb-1">Resolution Decision</label>
                  <select
                    value={resolutionType}
                    onChange={(e: any) => setResolutionType(e.target.value)}
                    className="w-full bg-slate-950 border border-slate-700 rounded px-2.5 py-1.5 text-xs text-slate-200"
                  >
                    <option value="PLACE_CONFIRMED">PLACE_CONFIRMED (Valid for PLACE; Provider Ref Required)</option>
                    <option value="PLACE_REJECTED">PLACE_REJECTED (Valid for PLACE; Releases cash reservation)</option>
                    <option value="CANCEL_CONFIRMED">CANCEL_CONFIRMED (Valid for CANCEL; Releases cash reservation)</option>
                    <option value="CANCEL_NOT_CONFIRMED">CANCEL_NOT_CONFIRMED (Valid for CANCEL; Return to ACKNOWLEDGED)</option>
                  </select>
                </div>
                <div>
                  <label className="block text-[11px] text-slate-400 mb-1">Upstox Order Reference (optional)</label>
                  <input
                    type="text"
                    placeholder="e.g. 240913000123456"
                    value={providerOrderRef}
                    onChange={(e) => setProviderOrderRef(e.target.value)}
                    className="w-full bg-slate-950 border border-slate-700 rounded px-2.5 py-1.5 text-xs text-slate-200"
                  />
                </div>
              </div>
              <div>
                <label className="block text-[11px] text-slate-400 mb-1">Audit Notes (Mandatory, min 5 chars)</label>
                <textarea
                  rows={2}
                  placeholder="Describe portal inspection results and justification..."
                  value={resolutionNotes}
                  onChange={(e) => setResolutionNotes(e.target.value)}
                  className="w-full bg-slate-950 border border-slate-700 rounded px-2.5 py-1.5 text-xs text-slate-200"
                />
              </div>
              <div className="flex justify-end gap-2">
                <button
                  onClick={() => setSelectedOrderToResolve(null)}
                  className="px-3 py-1 bg-slate-800 text-slate-300 rounded text-xs"
                >
                  Cancel
                </button>
                <button
                  disabled={resolving || resolutionNotes.length < 5}
                  onClick={handleResolveSubmit}
                  className="px-3 py-1 bg-rose-700 hover:bg-rose-600 disabled:opacity-50 text-white rounded text-xs font-semibold"
                >
                  {resolving ? "Resolving..." : "Submit Resolution"}
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* 3. Submission Outbox Queue */}
      <div className="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-lg space-y-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-bold text-white tracking-wide flex items-center gap-2">
            <span>Transactional Submission Outbox</span>
            <span className="text-xs px-2 py-0.5 rounded-full bg-slate-800 text-slate-300 font-normal">
              {outboxItems.length} Entries
            </span>
          </h3>
          <span className="text-[11px] text-slate-400">Lease-based worker delivery</span>
        </div>

        <div className="overflow-x-auto">
          {outboxItems.length === 0 ? (
            <div className="text-center py-6 text-xs text-slate-500">Outbox queue is currently empty.</div>
          ) : (
            <table className="w-full text-xs text-left text-slate-300">
              <thead className="text-[11px] text-slate-400 uppercase bg-slate-950/60 border-b border-slate-800">
                <tr>
                  <th className="py-2.5 px-3">Action</th>
                  <th className="py-2.5 px-3">Order ID</th>
                  <th className="py-2.5 px-3">Status</th>
                  <th className="py-2.5 px-3">Attempts</th>
                  <th className="py-2.5 px-3">Next Attempt</th>
                  <th className="py-2.5 px-3">Last Error</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/60">
                {outboxItems.map((item) => (
                  <tr key={item.id} className="hover:bg-slate-800/30 font-mono text-[11px]">
                    <td className="py-2.5 px-3">
                      <span className={`px-1.5 py-0.5 rounded font-bold ${
                        item.action_type === "CANCEL" ? "bg-amber-950 text-amber-300 border border-amber-800" : "bg-blue-950 text-blue-300 border border-blue-800"
                      }`}>
                        {item.action_type}
                      </span>
                    </td>
                    <td className="py-2.5 px-3">{item.order_id.slice(0, 10)}...</td>
                    <td className="py-2.5 px-3">
                      <span className={`px-2 py-0.5 rounded text-[10px] ${
                        item.status === "DELIVERED"
                          ? "bg-emerald-950 text-emerald-300 border border-emerald-800"
                          : item.status === "PENDING"
                          ? "bg-blue-950 text-blue-300 border border-blue-800"
                          : item.status === "RETRY_SCHEDULED"
                          ? "bg-amber-950 text-amber-300 border border-amber-800"
                          : "bg-rose-950 text-rose-300 border border-rose-800"
                      }`}>
                        {item.status}
                      </span>
                    </td>
                    <td className="py-2.5 px-3">{item.attempts} / {item.max_attempts}</td>
                    <td className="py-2.5 px-3 text-slate-400">
                      {new Date(item.next_attempt_at).toLocaleTimeString()}
                    </td>
                    <td className="py-2.5 px-3 text-rose-300 max-w-xs truncate" title={item.last_error_message || ""}>
                      {item.last_error_code ? `[${item.last_error_code}] ` : ""}
                      {item.last_error_message || "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {/* 4. Instrument Mappings Table & Verification */}
      <div className="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-lg space-y-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-bold text-white tracking-wide">
            Provider Instrument Mappings (NSE_FO / NIFTY)
          </h3>
          <span className="text-[11px] text-slate-400">Frozen per-runtime mapping context</span>
        </div>

        <div className="overflow-x-auto">
          {mappings.length === 0 ? (
            <div className="text-center py-6 text-xs text-slate-500">No instrument mappings registered.</div>
          ) : (
            <table className="w-full text-xs text-left text-slate-300">
              <thead className="text-[11px] text-slate-400 uppercase bg-slate-950/60 border-b border-slate-800">
                <tr>
                  <th className="py-2.5 px-3">TradePro ID</th>
                  <th className="py-2.5 px-3">Provider Token</th>
                  <th className="py-2.5 px-3">Symbol</th>
                  <th className="py-2.5 px-3">Status</th>
                  <th className="py-2.5 px-3 text-right">Verification</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/60">
                {mappings.map((m) => (
                  <tr key={m.id} className="hover:bg-slate-800/30">
                    <td className="py-2.5 px-3 font-mono text-[11px]">{m.tradepro_instrument_id}</td>
                    <td className="py-2.5 px-3 font-mono text-[11px] text-amber-300">{m.provider_instrument_token}</td>
                    <td className="py-2.5 px-3 font-semibold">{m.symbol}</td>
                    <td className="py-2.5 px-3">
                      <span className={`px-2 py-0.5 rounded text-[10px] ${
                        m.verification_status === "VERIFIED"
                          ? "bg-emerald-950 text-emerald-300 border border-emerald-800"
                          : m.verification_status === "UNVERIFIED"
                          ? "bg-amber-950 text-amber-300 border border-amber-800"
                          : "bg-rose-950 text-rose-300 border border-rose-800"
                      }`}>
                        {m.verification_status}
                      </span>
                    </td>
                    <td className="py-2.5 px-3 text-right">
                      {isAdmin && m.verification_status === "UNVERIFIED" && (
                        <button
                          onClick={() => {
                            setVerifyMappingId(m.id);
                            setVerifyReason("Administrative verification against Upstox master instrument file");
                          }}
                          className="px-2 py-1 bg-indigo-600 hover:bg-indigo-500 text-white rounded text-[11px] transition-colors"
                        >
                          Verify (Admin)
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        {/* Admin Verification Modal */}
        {verifyMappingId && (
          <div className="bg-slate-950 border border-indigo-500/50 rounded-lg p-4 space-y-3 mt-3">
            <h5 className="text-xs font-bold text-white uppercase tracking-wider">
              Cross-Owner Administrative Verification
            </h5>
            <p className="text-[11px] text-slate-400">
              Verifying mapping for configured sandbox owner. Action will be permanently recorded in verification audit trail.
            </p>
            <input
              type="text"
              placeholder="Verification justification / source..."
              value={verifyReason}
              onChange={(e) => setVerifyReason(e.target.value)}
              className="w-full bg-slate-900 border border-slate-700 rounded px-2.5 py-1.5 text-xs text-slate-200"
            />
            <div className="flex justify-end gap-2">
              <button
                onClick={() => setVerifyMappingId(null)}
                className="px-3 py-1 bg-slate-800 text-slate-300 rounded text-xs"
              >
                Cancel
              </button>
              <button
                disabled={verifying || !verifyReason}
                onClick={() => handleVerifySubmit("REJECTED")}
                className="px-3 py-1 bg-rose-800 hover:bg-rose-700 text-white rounded text-xs"
              >
                Reject
              </button>
              <button
                disabled={verifying || !verifyReason}
                onClick={() => handleVerifySubmit("VERIFIED")}
                className="px-3 py-1 bg-emerald-700 hover:bg-emerald-600 text-white rounded text-xs font-semibold"
              >
                {verifying ? "Verifying..." : "Confirm Verification"}
              </button>
            </div>
          </div>
        )}
      </div>

      {/* 5. Historical Reconciliation Audit Trail */}
      {reconciliations.length > 0 && (
        <div className="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-lg space-y-4">
          <h3 className="text-sm font-bold text-white tracking-wide">
            Resolved Reconciliation Audit Trail
          </h3>
          <div className="overflow-x-auto">
            <table className="w-full text-xs text-left text-slate-300">
              <thead className="text-[11px] text-slate-400 uppercase bg-slate-950/60 border-b border-slate-800">
                <tr>
                  <th className="py-2 px-3">Order ID</th>
                  <th className="py-2 px-3">Outbox ID</th>
                  <th className="py-2 px-3">Resolution Type</th>
                  <th className="py-2 px-3">Resolved By</th>
                  <th className="py-2 px-3">Provider Ref</th>
                  <th className="py-2 px-3">Resolved At</th>
                  <th className="py-2 px-3">Audit Notes</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/60">
                {reconciliations.map((r) => (
                  <tr key={r.id} className="hover:bg-slate-800/20 text-[11px]">
                    <td className="py-2 px-3 font-mono">{r.order_id.slice(0, 10)}...</td>
                    <td className="py-2 px-3 font-mono text-slate-400">{r.outbox_id ? r.outbox_id.slice(0, 10) + "..." : "—"}</td>
                    <td className="py-2 px-3 font-semibold">{r.resolution_type}</td>
                    <td className="py-2 px-3">{r.resolved_by}</td>
                    <td className="py-2 px-3 font-mono text-slate-400">{r.provider_order_reference || "—"}</td>
                    <td className="py-2 px-3 text-slate-400">{new Date(r.resolved_at).toLocaleTimeString()}</td>
                    <td className="py-2 px-3 text-slate-300 max-w-xs truncate" title={r.notes}>{r.notes}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
};
