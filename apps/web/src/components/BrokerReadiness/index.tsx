"use client";

import React, { useState, useEffect, useCallback, useRef } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import {
  fetchBrokerConnection,
  fetchBrokerAuthorizeUrl,
  submitBrokerOAuthCallback,
  disconnectBroker,
  refreshBrokerSnapshots,
  fetchLatestBrokerSnapshot,
  fetchBrokerReadiness,
  BrokerConnectionResponse,
  BrokerReadinessResponse,
  BrokerSnapshotDetail,
} from "../../lib/api";
import { AuthHeaderBadge } from "@/components/AuthHeaderBadge";
import {
  AlertCircle,
  CheckCircle2,
  Clock,
  ExternalLink,
  Layers,
  RefreshCw,
  Shield,
  ShieldAlert,
  ShieldCheck,
  Unlink,
  Wallet,
  XCircle,
} from "lucide-react";

export function BrokerReadiness() {
  const searchParams = useSearchParams();
  const callbackProcessedRef = useRef(false);

  // State
  const [environment, setEnvironment] = useState<"LIVE" | "SANDBOX">("LIVE");
  const [connection, setConnection] = useState<BrokerConnectionResponse | null>(null);
  const [readiness, setReadiness] = useState<BrokerReadinessResponse | null>(null);
  const [snapshot, setSnapshot] = useState<BrokerSnapshotDetail | null>(null);

  const [loading, setLoading] = useState<boolean>(true);
  const [refreshing, setRefreshing] = useState<boolean>(false);
  const [connecting, setConnecting] = useState<boolean>(false);
  const [disconnecting, setDisconnecting] = useState<boolean>(false);

  const [error, setError] = useState<string | null>(null);
  const [successMsg, setSuccessMsg] = useState<string | null>(null);

  // Manual callback inputs if needed
  const [authCode, setAuthCode] = useState<string>("");
  const [authState, setAuthState] = useState<string>("");
  const [showManualCallback, setShowManualCallback] = useState<boolean>(false);

  // Active view tab for snapshot inspection
  const [activeTab, setActiveTab] = useState<"margins" | "positions" | "holdings" | "orders">("margins");

  // Load all broker state
  const loadBrokerState = useCallback(async (env: "LIVE" | "SANDBOX" = environment) => {
    try {
      setLoading(true);
      setError(null);

      // Fetch connection status
      const connData = await fetchBrokerConnection(env).catch((e) => {
        console.warn("Could not fetch connection status:", e);
        return null;
      });
      setConnection(connData);

      // Fetch readiness evaluation
      const readData = await fetchBrokerReadiness(env).catch((e) => {
        console.warn("Could not fetch broker readiness:", e);
        return null;
      });
      setReadiness(readData);

      // Fetch latest snapshot if connected
      if (connData && connData.status === "CONNECTED") {
        const snapData = await fetchLatestBrokerSnapshot(env).catch(() => null);
        setSnapshot(snapData);
      } else {
        setSnapshot(null);
      }
    } catch (err: any) {
      setError(err.message || "Failed to load broker readiness state.");
    } finally {
      setLoading(false);
    }
  }, [environment]);

  // Handle URL callback params if redirected from Upstox (consumed once, URL cleaned, env preserved)
  useEffect(() => {
    const code = searchParams.get("code");
    const state = searchParams.get("state");

    if (code && state) {
      if (callbackProcessedRef.current) return;
      callbackProcessedRef.current = true;

      const processCallback = async () => {
        // Strip code and state from URL immediately on failure as well as success
        if (typeof window !== "undefined" && window.history?.replaceState) {
          window.history.replaceState({}, document.title, window.location.pathname);
        }

        try {
          setLoading(true);
          setSuccessMsg("Authorizing broker connection...");
          setError(null);
          const conn = await submitBrokerOAuthCallback(code, state);
          setConnection(conn);
          setSuccessMsg(`Successfully connected to ${conn.provider} (Account: ${conn.broker_user_id_masked || "Verified"}).`);

          const targetEnv = (conn.environment as "LIVE" | "SANDBOX") || environment;
          setEnvironment(targetEnv);
          await loadBrokerState(targetEnv);
        } catch (err: any) {
          setSuccessMsg(null);
          setError(err.message || "OAuth callback failed.");
        } finally {
          setLoading(false);
        }
      };
      processCallback();
    } else if (!callbackProcessedRef.current) {
      loadBrokerState(environment);
    }
  }, [searchParams, loadBrokerState, environment]);

  // Initiate OAuth flow
  const handleInitiateOAuth = async () => {
    try {
      setConnecting(true);
      setError(null);
      const authUrlData = await fetchBrokerAuthorizeUrl(environment);
      setAuthState(authUrlData.state);
      // Open Upstox OAuth URL in new window/tab
      window.open(authUrlData.authorization_url, "_blank", "noopener,noreferrer");
      setSuccessMsg("Upstox login opened in a new tab. Paste the redirected code below or complete authorization.");
      setShowManualCallback(true);
    } catch (err: any) {
      setError(err.message || "Failed to generate authorization URL.");
    } finally {
      setConnecting(false);
    }
  };

  // Submit manual code
  const handleSubmitManualCallback = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!authCode.trim() || !authState.trim()) {
      setError("Authorization code and state token are required.");
      return;
    }

    try {
      setConnecting(true);
      setError(null);
      const conn = await submitBrokerOAuthCallback(authCode.trim(), authState.trim());
      setConnection(conn);
      setSuccessMsg(`Successfully connected broker account (${conn.broker_user_id_masked || "Verified"}).`);
      setShowManualCallback(false);
      setAuthCode("");
      await loadBrokerState(environment);
    } catch (err: any) {
      setError(err.message || "Failed to complete authentication with provided code.");
    } finally {
      setConnecting(false);
    }
  };

  // Disconnect broker
  const handleDisconnect = async () => {
    if (!confirm("Are you sure you want to disconnect your broker connection? Session tokens will be wiped.")) {
      return;
    }

    try {
      setDisconnecting(true);
      setError(null);
      const conn = await disconnectBroker(environment);
      setConnection(conn);
      setSnapshot(null);
      setSuccessMsg("Broker connection disconnected successfully.");
      await loadBrokerState(environment);
    } catch (err: any) {
      setError(err.message || "Failed to disconnect broker.");
    } finally {
      setDisconnecting(false);
    }
  };

  // Refresh read-only snapshots
  const handleRefreshSnapshots = async () => {
    try {
      setRefreshing(true);
      setError(null);
      setSuccessMsg(null);
      const summary = await refreshBrokerSnapshots(environment);
      const marginDisplay = summary.available_margin !== null && summary.available_margin !== undefined
        ? `Available margin ₹${Number(summary.available_margin).toLocaleString("en-IN")}`
        : "Snapshot updated";
      setSuccessMsg(`Snapshot refreshed: ${marginDisplay}, ${summary.positions_count} positions.`);
      await loadBrokerState(environment);
    } catch (err: any) {
      setError(err.message || "Failed to refresh broker snapshots.");
    } finally {
      setRefreshing(false);
    }
  };

  const formatCurrencyOrDash = (val: any) => {
    if (val === null || val === undefined || val === "") return "—";
    const num = Number(val);
    if (isNaN(num)) return String(val);
    return `₹${num.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col font-sans">
      {/* Navbar Header */}
      <header className="border-b border-slate-900 bg-slate-950/80 backdrop-blur-md sticky top-0 z-50">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="bg-indigo-600 p-2 rounded-lg text-white font-extrabold text-lg tracking-wider">
              TP
            </div>
            <div>
              <span className="font-extrabold text-white text-base tracking-wide">TradePro</span>
              <span className="text-[10px] bg-slate-900 border border-slate-800 text-slate-400 px-1.5 py-0.5 rounded font-mono ml-2">
                v1.0.0
              </span>
            </div>
          </div>
          <nav className="flex items-center gap-4 text-xs font-semibold text-slate-300">
            <Link href="/" className="hover:text-white transition px-3 py-1.5">Strategies</Link>
            <Link href="/builder" className="hover:text-white transition px-3 py-1.5">Builder</Link>
            <Link href="/indicator-lab" className="hover:text-white transition px-3 py-1.5">Indicator Lab</Link>
            <Link href="/rule-lab" className="hover:text-white transition px-3 py-1.5">Rule Lab</Link>
            <Link href="/multi-series-lab" className="hover:text-white transition px-3 py-1.5">Multi-Series Lab</Link>
            <Link href="/historical-replay-lab" className="hover:text-white transition px-3 py-1.5">Historical Replay</Link>
            <Link href="/replay-comparison-lab" className="hover:text-white transition px-3 py-1.5 text-sky-400 font-bold">Replay Comparison</Link>
            <Link href="/data-quality-lab" className="hover:text-white transition px-3 py-1.5 text-emerald-400 font-bold">Data Quality</Link>
            <Link href="/paper-trading-lab" className="hover:text-white transition px-3 py-1.5 text-amber-400 font-bold">Paper Trading</Link>
            <Link href="/market-data-lab" className="hover:text-white transition px-3 py-1.5 text-teal-400 font-bold">Market Data</Link>
            <Link href="/broker-readiness" className="text-white bg-slate-900 border border-slate-800 px-3 py-1.5 rounded-lg text-violet-400 font-bold">Broker Readiness</Link>
            <Link href="/inspection-history" className="hover:text-white transition px-3 py-1.5">History</Link>
            <AuthHeaderBadge />
          </nav>
        </div>
      </header>

      {/* Main Container */}
      <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8 flex-1 w-full space-y-6">
        {/* Phase 7A Strict Constraint Banner */}
        <div className="bg-amber-950/30 border border-amber-800/60 rounded-xl p-4 flex items-start gap-3 shadow-lg shadow-amber-950/20">
          <ShieldAlert className="text-amber-400 shrink-0 mt-0.5" size={22} />
          <div className="space-y-1">
            <div className="flex items-center gap-2">
              <h2 className="font-bold text-sm text-amber-200">
                Phase 7A: Read-Only Broker Connection & Live Readiness
              </h2>
              <span
                id="live-dispatch-badge"
                className="bg-amber-500/20 text-amber-300 text-[10px] font-mono px-2 py-0.5 rounded border border-amber-500/30 uppercase tracking-wider font-semibold"
              >
                Live Orders Disabled
              </span>
            </div>
            <p className="text-xs text-amber-300/80 leading-relaxed">
              This panel enables secure Upstox OAuth 2.0 authentication, authoritative account binding, and pure read-only balance/position snapshots.
              Live order placement and cancellation remain strictly blocked (<code className="font-mono bg-black/40 px-1 py-0.5 rounded text-amber-200">live_dispatch_enabled: false</code>).
              Readings here are isolated from the paper trading ledger.
            </p>
          </div>
        </div>

        {/* Header Title & Environment Selector */}
        <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4 border-b border-slate-900 pb-5">
          <div>
            <h1 className="text-2xl font-extrabold tracking-tight text-white flex items-center gap-2">
              <Shield className="text-violet-400" size={24} />
              Broker Readiness & Connection Inspection
            </h1>
            <p className="text-xs text-slate-400 mt-1">
              Verify Upstox credentials, review regulatory and session gates, and inspect live account margins.
            </p>
          </div>

          <div className="flex items-center gap-3">
            <div className="bg-slate-900 border border-slate-800 rounded-lg p-1 flex items-center gap-1 text-xs font-semibold">
              <button
                type="button"
                onClick={() => {
                  setEnvironment("LIVE");
                  loadBrokerState("LIVE");
                }}
                className={`px-3 py-1 rounded transition ${
                  environment === "LIVE"
                    ? "bg-violet-600 text-white shadow"
                    : "text-slate-400 hover:text-white"
                }`}
              >
                LIVE
              </button>
              <button
                type="button"
                onClick={() => {
                  setEnvironment("SANDBOX");
                  loadBrokerState("SANDBOX");
                }}
                className={`px-3 py-1 rounded transition ${
                  environment === "SANDBOX"
                    ? "bg-violet-600 text-white shadow"
                    : "text-slate-400 hover:text-white"
                }`}
              >
                SANDBOX
              </button>
            </div>

            <button
              type="button"
              onClick={() => loadBrokerState(environment)}
              disabled={loading || refreshing}
              className="bg-slate-900 hover:bg-slate-800 border border-slate-800 text-slate-200 text-xs px-3 py-2 rounded-lg flex items-center gap-1.5 transition disabled:opacity-50"
              title="Refresh Readiness State"
            >
              <RefreshCw size={13} className={loading ? "animate-spin" : ""} />
              <span>Refresh</span>
            </button>
          </div>
        </div>

        {/* Alerts */}
        {error && (
          <div className="bg-rose-950/30 border border-rose-900/60 rounded-xl p-4 flex items-start gap-3">
            <XCircle className="text-rose-400 shrink-0 mt-0.5" size={18} />
            <div className="text-xs text-rose-300">
              <span className="font-bold">Error:</span> {error}
            </div>
          </div>
        )}

        {successMsg && (
          <div className="bg-emerald-950/30 border border-emerald-900/60 rounded-xl p-4 flex items-start gap-3">
            <CheckCircle2 className="text-emerald-400 shrink-0 mt-0.5" size={18} />
            <div className="text-xs text-emerald-300">
              <span className="font-bold">Success:</span> {successMsg}
            </div>
          </div>
        )}

        {/* Top Grid: Connection Status & Live Readiness Gates */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Card 1: Connection & Authentication (1 Col) */}
          <div className="bg-slate-900/40 border border-slate-800/80 rounded-2xl p-6 flex flex-col justify-between space-y-6">
            <div>
              <div className="flex items-center justify-between border-b border-slate-800 pb-4">
                <div className="flex items-center gap-2">
                  <Wallet className="text-indigo-400" size={18} />
                  <h3 className="font-bold text-sm text-white">Upstox Connection</h3>
                </div>
                <span
                  className={`text-[11px] font-mono font-bold px-2.5 py-0.5 rounded-full border ${
                    connection?.status === "CONNECTED" && connection?.token_valid
                      ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/30"
                      : connection?.status === "PENDING_AUTH"
                      ? "bg-amber-500/10 text-amber-400 border-amber-500/30"
                      : "bg-rose-500/10 text-rose-400 border-rose-500/30"
                  }`}
                >
                  {connection?.status || "DISCONNECTED"}
                </span>
              </div>

              <div className="mt-4 space-y-3 text-xs">
                <div className="flex justify-between items-center py-1 border-b border-slate-800/50">
                  <span className="text-slate-400">Environment</span>
                  <span className="font-mono text-slate-200">{environment}</span>
                </div>
                <div className="flex justify-between items-center py-1 border-b border-slate-800/50">
                  <span className="text-slate-400">Masked UCC</span>
                  <span className="font-mono font-bold text-slate-100">
                    {connection?.broker_user_id_masked || "—"}
                  </span>
                </div>
                <div className="flex justify-between items-center py-1 border-b border-slate-800/50">
                  <span className="text-slate-400">Account Name</span>
                  <span className="text-slate-200">
                    {connection?.account_name || "—"}
                  </span>
                </div>
                <div className="flex justify-between items-center py-1 border-b border-slate-800/50">
                  <span className="text-slate-400">Token Validity</span>
                  <span className="flex items-center gap-1.5">
                    {connection?.token_valid ? (
                      <>
                        <span className="w-2 h-2 rounded-full bg-emerald-400"></span>
                        <span className="text-emerald-400 font-medium">Valid</span>
                      </>
                    ) : (
                      <>
                        <span className="w-2 h-2 rounded-full bg-rose-400"></span>
                        <span className="text-rose-400 font-medium">Inactive / Expired</span>
                      </>
                    )}
                  </span>
                </div>
                <div className="flex justify-between items-center py-1">
                  <span className="text-slate-400">Token Expiry</span>
                  <span className="font-mono text-[11px] text-slate-300">
                    {connection?.token_expires_at
                      ? new Date(connection.token_expires_at).toLocaleString()
                      : "Daily at ~03:30 AM IST"}
                  </span>
                </div>
              </div>
            </div>

            {/* Connection Actions */}
            <div className="pt-4 border-t border-slate-800 space-y-3">
              {connection?.status === "CONNECTED" && connection?.token_valid ? (
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={handleRefreshSnapshots}
                    disabled={refreshing}
                    className="flex-1 bg-violet-600 hover:bg-violet-500 text-white font-semibold text-xs px-3 py-2.5 rounded-lg flex items-center justify-center gap-1.5 transition disabled:opacity-50"
                  >
                    <RefreshCw size={13} className={refreshing ? "animate-spin" : ""} />
                    <span>{refreshing ? "Refreshing..." : "Refresh Snapshot"}</span>
                  </button>
                  <button
                    type="button"
                    onClick={handleDisconnect}
                    disabled={disconnecting}
                    className="bg-slate-900 hover:bg-rose-950/40 text-rose-400 border border-slate-800 hover:border-rose-900 font-semibold text-xs px-3 py-2.5 rounded-lg flex items-center gap-1.5 transition disabled:opacity-50"
                    title="Disconnect broker"
                  >
                    <Unlink size={13} />
                    <span>Disconnect</span>
                  </button>
                </div>
              ) : (
                <button
                  type="button"
                  onClick={handleInitiateOAuth}
                  disabled={connecting}
                  className="w-full bg-indigo-600 hover:bg-indigo-500 text-white font-semibold text-xs px-4 py-2.5 rounded-lg flex items-center justify-center gap-2 shadow-lg shadow-indigo-950/40 transition disabled:opacity-50"
                >
                  <ExternalLink size={14} />
                  <span>Connect Upstox via OAuth 2.0</span>
                </button>
              )}

              {/* Toggle manual code entry */}
              {(!connection || connection.status !== "CONNECTED") && (
                <button
                  type="button"
                  onClick={() => setShowManualCallback(!showManualCallback)}
                  className="w-full text-center text-[11px] text-slate-400 hover:text-slate-200 underline pt-1"
                >
                  {showManualCallback ? "Hide manual code entry" : "Entering authorization code manually?"}
                </button>
              )}
            </div>

            {/* Manual Callback Form Modal/Panel */}
            {showManualCallback && (
              <form onSubmit={handleSubmitManualCallback} className="bg-slate-950/80 border border-slate-800 p-3 rounded-xl space-y-3 text-xs mt-2">
                <div>
                  <label className="block text-slate-400 mb-1">State Token (Pre-filled):</label>
                  <input
                    type="text"
                    value={authState}
                    onChange={(e) => setAuthState(e.target.value)}
                    placeholder="Enter state token"
                    className="w-full bg-slate-900 border border-slate-800 px-2 py-1 rounded text-slate-200 font-mono text-[11px] focus:outline-none focus:border-indigo-500"
                    required
                  />
                </div>
                <div>
                  <label className="block text-slate-400 mb-1">Authorization Code from Upstox redirect:</label>
                  <input
                    type="text"
                    value={authCode}
                    onChange={(e) => setAuthCode(e.target.value)}
                    placeholder="Paste ?code= query param here"
                    className="w-full bg-slate-900 border border-slate-800 px-2 py-1 rounded text-slate-200 font-mono text-[11px] focus:outline-none focus:border-indigo-500"
                    required
                  />
                </div>
                <button
                  type="submit"
                  disabled={connecting}
                  className="w-full bg-emerald-600 hover:bg-emerald-500 text-white font-semibold py-1.5 rounded transition text-xs disabled:opacity-50"
                >
                  {connecting ? "Validating Code..." : "Submit Authorization Code"}
                </button>
              </form>
            )}
          </div>

          {/* Card 2: Readiness Evaluation & Gates (2 Cols) */}
          <div className="bg-slate-900/40 border border-slate-800/80 rounded-2xl p-6 lg:col-span-2 space-y-6">
            <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-2 border-b border-slate-800 pb-4">
              <div className="flex items-center gap-2">
                <ShieldCheck className="text-violet-400" size={18} />
                <h3 className="font-bold text-sm text-white">Live Readiness Gate Multi-Check</h3>
              </div>
              <div className="flex items-center gap-2">
                <span className="text-xs text-slate-400">Overall:</span>
                <span
                  className={`text-xs font-mono font-bold px-3 py-1 rounded-full border ${
                    readiness?.overall_status === "READY_FOR_MONITORING"
                      ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/30"
                      : "bg-rose-500/10 text-rose-400 border-rose-500/30"
                  }`}
                >
                  {readiness?.overall_status || "UNKNOWN"}
                </span>
              </div>
            </div>

            {/* Gates List */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {readiness?.gates?.map((gate, idx) => (
                <div
                  key={idx}
                  className={`p-3 rounded-xl border text-xs flex flex-col justify-between space-y-1.5 transition ${
                    gate.status === "READY"
                      ? "bg-emerald-950/10 border-emerald-900/30 text-emerald-200"
                      : "bg-slate-900/60 border-slate-800 text-slate-300"
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span className="font-bold font-mono uppercase text-[11px] text-slate-200">
                      {gate.gate_name.replace(/_/g, " ")}
                    </span>
                    <span
                      className={`text-[10px] font-mono font-bold px-2 py-0.5 rounded border ${
                        gate.status === "READY"
                          ? "bg-emerald-500/20 text-emerald-400 border-emerald-500/30"
                          : gate.status === "UNKNOWN"
                          ? "bg-amber-500/20 text-amber-400 border-amber-500/30"
                          : "bg-rose-500/20 text-rose-400 border-rose-500/30"
                      }`}
                    >
                      {gate.status}
                    </span>
                  </div>
                  <p className="text-[11px] text-slate-400 leading-relaxed">{gate.details}</p>
                  {gate.actionable_remedy && gate.status !== "READY" && (
                    <div className="text-[10px] text-amber-400/90 bg-amber-950/20 border border-amber-900/30 p-1.5 rounded mt-1">
                      <span className="font-semibold">Remedy: </span>
                      {gate.actionable_remedy}
                    </div>
                  )}
                </div>
              ))}
            </div>

            {/* Actionable Blockers Warning */}
            {readiness?.actionable_blockers && readiness.actionable_blockers.length > 0 && (
              <div className="bg-rose-950/20 border border-rose-900/50 rounded-xl p-3.5 space-y-1.5 text-xs">
                <div className="flex items-center gap-2 text-rose-400 font-bold">
                  <AlertCircle size={15} />
                  <span>Actionable Readiness Blockers ({readiness.actionable_blockers.length})</span>
                </div>
                <ul className="list-disc list-inside text-rose-300/80 text-[11px] space-y-0.5 pl-1">
                  {readiness.actionable_blockers.map((b, i) => (
                    <li key={i}>{b}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </div>

        {/* Snapshot Details Section */}
        <div className="bg-slate-900/40 border border-slate-800/80 rounded-2xl p-6 space-y-6">
          <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4 border-b border-slate-800 pb-4">
            <div>
              <h3 className="font-bold text-sm text-white flex items-center gap-2">
                <Layers className="text-sky-400" size={18} />
                Read-Only Broker Snapshot
              </h3>
              <p className="text-xs text-slate-400 mt-0.5">
                Authoritative portfolio state snapshot fetched directly from Upstox. Does not affect paper accounts.
              </p>
            </div>

            {/* Sub-tabs */}
            <div className="bg-slate-950 border border-slate-800 rounded-lg p-1 flex items-center gap-1 text-xs">
              <button
                type="button"
                onClick={() => setActiveTab("margins")}
                className={`px-3 py-1 rounded transition font-medium ${
                  activeTab === "margins" ? "bg-slate-800 text-white" : "text-slate-400 hover:text-white"
                }`}
              >
                Margins & Funds
              </button>
              <button
                type="button"
                onClick={() => setActiveTab("positions")}
                className={`px-3 py-1 rounded transition font-medium ${
                  activeTab === "positions" ? "bg-slate-800 text-white" : "text-slate-400 hover:text-white"
                }`}
              >
                Positions ({snapshot?.positions?.length || 0})
              </button>
              <button
                type="button"
                onClick={() => setActiveTab("holdings")}
                className={`px-3 py-1 rounded transition font-medium ${
                  activeTab === "holdings" ? "bg-slate-800 text-white" : "text-slate-400 hover:text-white"
                }`}
              >
                Holdings ({snapshot?.holdings?.length || 0})
              </button>
              <button
                type="button"
                onClick={() => setActiveTab("orders")}
                className={`px-3 py-1 rounded transition font-medium ${
                  activeTab === "orders" ? "bg-slate-800 text-white" : "text-slate-400 hover:text-white"
                }`}
              >
                Orders ({snapshot?.orders?.length || 0})
              </button>
            </div>
          </div>

          {/* Sub-tab Content */}
          {!snapshot ? (
            <div className="py-12 text-center text-slate-500 text-xs border border-dashed border-slate-800 rounded-xl">
              <Clock className="mx-auto mb-2 opacity-40" size={24} />
              No active broker snapshot available. Connect and click &quot;Refresh Snapshot&quot; to inspect balances.
            </div>
          ) : (
            <div>
              {activeTab === "margins" && (
                <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                  <div className="bg-slate-950/60 border border-slate-800/80 p-4 rounded-xl">
                    <span className="text-xs text-slate-400">Available Equity Margin</span>
                    <div className="text-xl font-mono font-extrabold text-emerald-400 mt-1">
                      {formatCurrencyOrDash(snapshot.funds_and_margins?.equity?.available_margin)}
                    </div>
                  </div>
                  <div className="bg-slate-950/60 border border-slate-800/80 p-4 rounded-xl">
                    <span className="text-xs text-slate-400">Used Margin</span>
                    <div className="text-xl font-mono font-extrabold text-amber-400 mt-1">
                      {formatCurrencyOrDash(snapshot.funds_and_margins?.equity?.used_margin)}
                    </div>
                  </div>
                  <div className="bg-slate-950/60 border border-slate-800/80 p-4 rounded-xl">
                    <span className="text-xs text-slate-400">Snapshot Timestamp</span>
                    <div className="text-sm font-mono text-slate-300 mt-2">
                      {snapshot.acquired_at_utc ? new Date(snapshot.acquired_at_utc).toLocaleString() : "—"}
                    </div>
                  </div>
                </div>
              )}

              {activeTab === "positions" && (
                <div className="overflow-x-auto">
                  {snapshot.positions?.length === 0 ? (
                    <div className="py-8 text-center text-slate-500 text-xs">Zero open positions reported by broker.</div>
                  ) : (
                    <table className="w-full text-left text-xs text-slate-300">
                      <thead className="bg-slate-950/80 text-slate-400 uppercase font-mono text-[10px] border-b border-slate-800">
                        <tr>
                          <th className="py-2.5 px-3">Symbol</th>
                          <th className="py-2.5 px-3">Product</th>
                          <th className="py-2.5 px-3 text-right">Quantity</th>
                          <th className="py-2.5 px-3 text-right">Avg Price</th>
                          <th className="py-2.5 px-3 text-right">P&L</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-slate-800/60 font-mono">
                        {snapshot.positions.map((p, idx) => (
                          <tr key={idx} className="hover:bg-slate-900/40">
                            <td className="py-2 px-3 font-bold text-white">{p.tradingsymbol || "—"}</td>
                            <td className="py-2 px-3 text-slate-400">{p.product || "CNC/MIS"}</td>
                            <td className="py-2 px-3 text-right">{p.quantity ?? 0}</td>
                            <td className="py-2 px-3 text-right">{p.average_price ? `₹${p.average_price}` : "—"}</td>
                            <td className={`py-2 px-3 text-right ${Number(p.pnl || 0) >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                              {p.pnl ? `₹${p.pnl}` : "—"}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
              )}

              {activeTab === "holdings" && (
                <div className="overflow-x-auto">
                  {snapshot.holdings?.length === 0 ? (
                    <div className="py-8 text-center text-slate-500 text-xs">Zero holdings reported by broker.</div>
                  ) : (
                    <table className="w-full text-left text-xs text-slate-300">
                      <thead className="bg-slate-950/80 text-slate-400 uppercase font-mono text-[10px] border-b border-slate-800">
                        <tr>
                          <th className="py-2.5 px-3">ISIN</th>
                          <th className="py-2.5 px-3">Company / Symbol</th>
                          <th className="py-2.5 px-3 text-right">Quantity</th>
                          <th className="py-2.5 px-3 text-right">Avg Price</th>
                          <th className="py-2.5 px-3 text-right">P&L</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-slate-800/60 font-mono">
                        {snapshot.holdings.map((h, idx) => (
                          <tr key={idx} className="hover:bg-slate-900/40">
                            <td className="py-2 px-3 text-slate-400">{h.isin || "—"}</td>
                            <td className="py-2 px-3 font-bold text-white">{h.company_name || h.tradingsymbol || "—"}</td>
                            <td className="py-2 px-3 text-right">{h.quantity ?? 0}</td>
                            <td className="py-2 px-3 text-right">{h.average_price ? `₹${h.average_price}` : "—"}</td>
                            <td className={`py-2 px-3 text-right ${Number(h.pnl || 0) >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                              {h.pnl ? `₹${h.pnl}` : "—"}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
              )}

              {activeTab === "orders" && (
                <div className="overflow-x-auto">
                  {snapshot.orders?.length === 0 ? (
                    <div className="py-8 text-center text-slate-500 text-xs">Zero orders reported by broker for today.</div>
                  ) : (
                    <table className="w-full text-left text-xs text-slate-300">
                      <thead className="bg-slate-950/80 text-slate-400 uppercase font-mono text-[10px] border-b border-slate-800">
                        <tr>
                          <th className="py-2.5 px-3">Order ID</th>
                          <th className="py-2.5 px-3">Symbol</th>
                          <th className="py-2.5 px-3">Side</th>
                          <th className="py-2.5 px-3 text-right">Qty</th>
                          <th className="py-2.5 px-3 text-right">Price</th>
                          <th className="py-2.5 px-3">Status</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y-divide-slate-800/60 font-mono">
                        {snapshot.orders.map((o, idx) => (
                          <tr key={idx} className="hover:bg-slate-900/40">
                            <td className="py-2 px-3 text-slate-400">{o.order_id || "—"}</td>
                            <td className="py-2 px-3 font-bold text-white">{o.tradingsymbol || "—"}</td>
                            <td className="py-2 px-3">{o.transaction_type || "BUY"}</td>
                            <td className="py-2 px-3 text-right">{o.quantity ?? 0}</td>
                            <td className="py-2 px-3 text-right">{o.price ? `₹${o.price}` : "MKT"}</td>
                            <td className="py-2 px-3">
                              <span className="text-[10px] uppercase font-bold px-2 py-0.5 rounded bg-slate-800 text-slate-300">
                                {o.status || "OPEN"}
                              </span>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
