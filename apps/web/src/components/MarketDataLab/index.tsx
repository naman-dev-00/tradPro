"use client";

import React, { useState, useEffect, useCallback, useRef } from "react";
import Link from "next/link";
import {
  fetchMarketDataReadiness,
  fetchMarketDataInstruments,
  fetchMarketDataCandles,
  MarketDataReadinessResponse,
  MarketDataInstrument,
  MarketDataCandle,
  MarketDataProvenance,
} from "../../lib/api";
import { AuthHeaderBadge } from "@/components/AuthHeaderBadge";
import {
  Activity,
  AlertTriangle,
  Calendar,
  CheckCircle2,
  Clock,
  Copy,
  Database,
  Globe,
  Lock,
  RefreshCw,
  Search,
  ShieldAlert,
  ShieldCheck,
  TrendingDown,
  TrendingUp,
} from "lucide-react";

interface LoadedMetadata {
  instrument_key: string;
  timeframe: string;
  mode: "intraday" | "historical";
  tradepro_instrument_id?: string | null;
}

export function MarketDataLab() {
  const [readiness, setReadiness] = useState<MarketDataReadinessResponse | null>(null);
  const [readinessError, setReadinessError] = useState<string | null>(null);
  const [instruments, setInstruments] = useState<MarketDataInstrument[]>([]);
  const [selectedInstrument, setSelectedInstrument] = useState<string>("NSE_INDEX|Nifty 50");
  const [timeframe, setTimeframe] = useState<string>("5m");
  const [mode, setMode] = useState<"intraday" | "historical">("intraday");
  const [fromDate, setFromDate] = useState<string>("2026-10-01");
  const [toDate, setToDate] = useState<string>("2026-10-05");

  const [candles, setCandles] = useState<MarketDataCandle[]>([]);
  const [provenance, setProvenance] = useState<MarketDataProvenance | null>(null);
  const [loadedMetadata, setLoadedMetadata] = useState<LoadedMetadata | null>(null);

  const [loading, setLoading] = useState<boolean>(true);
  const [fetching, setFetching] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [copiedFingerprint, setCopiedFingerprint] = useState<boolean>(false);

  const requestSequenceRef = useRef<number>(0);

  const loadInitialData = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      setReadinessError(null);

      let readinessData: MarketDataReadinessResponse | null = null;
      try {
        readinessData = await fetchMarketDataReadiness();
      } catch (err: any) {
        console.error("Readiness check failed:", err);
        setReadinessError(err.message || "Failed to check market data readiness.");
      }

      const instrumentsData = await fetchMarketDataInstruments().catch((e) => {
        console.warn("Could not fetch instruments:", e);
        return [];
      });

      setReadiness(readinessData);
      setInstruments(instrumentsData);
      if (instrumentsData.length > 0 && !instrumentsData.find((i) => i.instrument_key === selectedInstrument)) {
        setSelectedInstrument(instrumentsData[0].instrument_key);
      }
    } catch (err: any) {
      setError(err.message || "Failed to initialize Market Data Lab.");
    } finally {
      setLoading(false);
    }
  }, [selectedInstrument]);

  useEffect(() => {
    loadInitialData();
  }, [loadInitialData]);

  // Strict enablement: Acquisition is strictly disabled until readiness explicitly permits it
  const canAcquire =
    !loading &&
    !fetching &&
    readiness !== null &&
    readiness.status === "CONFIGURED_AND_ENABLED" &&
    readiness.network_enabled === true &&
    readiness.is_authorized_operator === true;

  const handleFetchCandles = async () => {
    if (!canAcquire) return;

    const currentSeq = ++requestSequenceRef.current;
    try {
      setFetching(true);
      setError(null);
      const res = await fetchMarketDataCandles({
        instrument_key: selectedInstrument,
        timeframe,
        mode,
        from_date: mode === "historical" ? fromDate : undefined,
        to_date: mode === "historical" ? toDate : undefined,
      });

      // Stale response guard: ignore if another request was initiated
      if (currentSeq !== requestSequenceRef.current) {
        return;
      }

      setCandles(res.candles);
      setProvenance(res.provenance);
      setLoadedMetadata({
        instrument_key: res.instrument_key,
        timeframe: res.timeframe,
        mode: res.mode,
        tradepro_instrument_id: res.tradepro_instrument_id,
      });
    } catch (err: any) {
      if (currentSeq === requestSequenceRef.current) {
        setError(err.message || "Failed to fetch market data candles.");
        setCandles([]);
        setProvenance(null);
        setLoadedMetadata(null);
      }
    } finally {
      if (currentSeq === requestSequenceRef.current) {
        setFetching(false);
      }
    }
  };

  const copyToClipboard = (text: string) => {
    if (navigator.clipboard) {
      navigator.clipboard.writeText(text);
      setCopiedFingerprint(true);
      setTimeout(() => setCopiedFingerprint(false), 2000);
    }
  };

  const formatPrice = (val: string | number) => {
    const num = typeof val === "string" ? parseFloat(val) : val;
    return isNaN(num) ? "—" : num.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 4 });
  };

  const renderCompletenessBadge = () => {
    if (!provenance) return null;
    if (provenance.completeness === "COMPLETE") {
      return (
        <span className="text-[11px] px-2 py-0.5 rounded font-bold bg-emerald-950/60 border border-emerald-500/40 text-emerald-300">
          Complete Series
        </span>
      );
    }
    if (provenance.completeness === "INCOMPLETE") {
      return (
        <span className="text-[11px] px-2 py-0.5 rounded font-bold bg-rose-950/60 border border-rose-500/40 text-rose-300">
          Gaps / Conflicts Detected
        </span>
      );
    }
    return (
      <span className="text-[11px] px-2 py-0.5 rounded font-bold bg-amber-950/60 border border-amber-500/40 text-amber-300">
        Unknown Completeness (Unverified Calendar/Bounds)
      </span>
    );
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col font-sans">
      {/* Header */}
      <header className="border-b border-slate-900 bg-slate-950/80 backdrop-blur-md sticky top-0 z-50">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="bg-teal-600 p-2 rounded-lg text-white font-extrabold text-lg tracking-wider">
              MD
            </div>
            <div>
              <span className="font-extrabold text-white text-base tracking-wide">TradePro</span>
              <span className="text-[10px] bg-slate-900 border border-slate-800 text-teal-400 px-1.5 py-0.5 rounded font-mono ml-2">Market Data Lab</span>
            </div>
          </div>
          <nav className="flex items-center gap-3 text-xs font-semibold text-slate-300 overflow-x-auto py-2">
            <Link href="/" className="hover:text-white transition px-2 py-1">Strategies</Link>
            <Link href="/builder" className="hover:text-white transition px-2 py-1">Builder</Link>
            <Link href="/indicator-lab" className="hover:text-white transition px-2 py-1">Indicator Lab</Link>
            <Link href="/rule-lab" className="hover:text-white transition px-2 py-1">Rule Lab</Link>
            <Link href="/multi-series-lab" className="hover:text-white transition px-2 py-1">Multi-Series</Link>
            <Link href="/historical-replay-lab" className="hover:text-white transition px-2 py-1">Replay</Link>
            <Link href="/replay-comparison-lab" className="hover:text-white transition px-2 py-1 text-sky-400">Comparison</Link>
            <Link href="/data-quality-lab" className="hover:text-white transition px-2 py-1 text-emerald-400">Data Quality</Link>
            <Link href="/paper-trading-lab" className="hover:text-white transition px-2 py-1 text-amber-400">Paper Trading</Link>
            <Link href="/market-data-lab" className="text-white bg-slate-900 border border-teal-500/50 text-teal-400 px-2 py-1 rounded-lg">Market Data</Link>
            <Link href="/inspection-history" className="hover:text-white transition px-2 py-1">History</Link>
            <AuthHeaderBadge />
          </nav>
        </div>
      </header>

      {/* Main Workspace */}
      <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8 flex-1 w-full space-y-6">
        {/* Banner Title */}
        <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 border-b border-slate-900 pb-5">
          <div>
            <h1 className="text-2xl font-extrabold tracking-tight text-white flex items-center gap-2">
              <Database className="text-teal-400" size={24} />
              Market Data & Provider Inspection Lab
            </h1>
            <p className="text-sm text-slate-400 mt-1">
              Read-only acquisition of Upstox API V3 historical & intraday completed candles with SHA-256 dataset provenance.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={loadInitialData}
              disabled={loading}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-slate-800 bg-slate-900 text-xs text-slate-300 hover:text-white hover:bg-slate-800 transition"
              title="Refresh connection status"
            >
              <RefreshCw size={13} className={loading ? "animate-spin" : ""} />
              Refresh Status
            </button>
          </div>
        </div>

        {/* Safety & Isolation Disclosure Notice */}
        <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-4 text-xs text-slate-300 flex items-start gap-3">
          <ShieldCheck className="text-teal-400 shrink-0 mt-0.5" size={18} />
          <div className="space-y-1">
            <span className="font-bold text-white">Phase 5 Read-Only Operational Boundary:</span>
            <p className="text-slate-400 leading-relaxed">
              This inspection lab operates strictly read-only. External market data network access is separate from broker order execution credentials.
              No broker orders, order intents, outbox entries, or financial ledger mutations can be generated from this workspace.
              Only completed candles closing on or before the current clock are admitted.
            </p>
          </div>
        </div>

        {/* Readiness Error Alert if any */}
        {readinessError && (
          <div className="bg-rose-950/30 border border-rose-900/60 rounded-xl p-4 text-xs text-rose-300 flex items-center gap-3">
            <ShieldAlert size={18} className="shrink-0 text-rose-400" />
            <div>
              <span className="font-bold">Readiness Verification Failure: </span>
              <span>{readinessError}</span>
            </div>
          </div>
        )}

        {/* Connection & Readiness Status Card */}
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
          <div className="bg-slate-900/40 border border-slate-800/80 rounded-xl p-4 space-y-2">
            <span className="text-xs text-slate-400 font-medium">Provider Status</span>
            <div className="flex items-center gap-2">
              {readiness?.status === "CONFIGURED_AND_ENABLED" ? (
                <>
                  <CheckCircle2 className="text-emerald-400" size={16} />
                  <span className="text-sm font-bold text-emerald-300">Configured & Enabled</span>
                </>
              ) : readiness?.status === "NETWORK_DISABLED" ? (
                <>
                  <AlertTriangle className="text-amber-400" size={16} />
                  <span className="text-sm font-bold text-amber-300">Network Disabled</span>
                </>
              ) : readiness?.status === "CREDENTIALS_MISSING" ? (
                <>
                  <ShieldAlert className="text-rose-400" size={16} />
                  <span className="text-sm font-bold text-rose-300">Token Missing</span>
                </>
              ) : readiness?.status === "OPERATOR_NOT_CONFIGURED" ? (
                <>
                  <Lock className="text-amber-400" size={16} />
                  <span className="text-sm font-bold text-amber-300">Operator Not Configured</span>
                </>
              ) : readiness?.status === "FORBIDDEN_OPERATOR" ? (
                <>
                  <Lock className="text-rose-400" size={16} />
                  <span className="text-sm font-bold text-rose-300">Operator Restricted</span>
                </>
              ) : readiness?.status === "INVALID_ENDPOINT_CONFIGURATION" ? (
                <>
                  <ShieldAlert className="text-rose-400" size={16} />
                  <span className="text-sm font-bold text-rose-300">Invalid Endpoint</span>
                </>
              ) : (
                <span className="text-sm font-bold text-slate-400">Checking...</span>
              )}
            </div>
            <p className="text-[11px] text-slate-500">
              {readiness?.status === "INVALID_ENDPOINT_CONFIGURATION"
                ? "Provider endpoint invalid or unapproved"
                : readiness?.network_enabled
                ? "UPSTOX_MARKET_DATA_ENABLED=true"
                : "UPSTOX_MARKET_DATA_ENABLED=false (Default Offline)"}
            </p>
          </div>

          <div className="bg-slate-900/40 border border-slate-800/80 rounded-xl p-4 space-y-2">
            <span className="text-xs text-slate-400 font-medium">Server Credential</span>
            <div className="flex items-center gap-2">
              {readiness?.credential_configured ? (
                <>
                  <Lock className="text-emerald-400" size={16} />
                  <span className="text-sm font-bold text-emerald-300">Configured (Server-Side)</span>
                </>
              ) : (
                <>
                  <ShieldAlert className="text-rose-400" size={16} />
                  <span className="text-sm font-bold text-rose-300">Not Configured</span>
                </>
              )}
            </div>
            <p className="text-[11px] text-slate-500">Token kept strictly server-side</p>
          </div>

          <div className="bg-slate-900/40 border border-slate-800/80 rounded-xl p-4 space-y-2">
            <span className="text-xs text-slate-400 font-medium">Approved Endpoint Host</span>
            <div className="flex items-center gap-2">
              <Globe className="text-teal-400" size={16} />
              <span className="text-sm font-mono font-semibold text-slate-200">api.upstox.com</span>
            </div>
            <p className="text-[11px] text-slate-500">Strict HTTPS /v3 paths only</p>
          </div>

          <div className="bg-slate-900/40 border border-slate-800/80 rounded-xl p-4 space-y-2">
            <span className="text-xs text-slate-400 font-medium">Completed Timeframes</span>
            <div className="flex items-center gap-2">
              <Clock className="text-sky-400" size={16} />
              <span className="text-sm font-bold text-sky-300">5m, 15m Supported</span>
            </div>
            <p className="text-[11px] text-slate-500">Tick/streaming excluded from Phase 5</p>
          </div>
        </div>

        {/* Parameter Selection Controls */}
        <div className="bg-slate-900/50 border border-slate-800 rounded-2xl p-6 space-y-5">
          <h2 className="text-sm font-bold text-white uppercase tracking-wider flex items-center gap-2">
            <Search size={16} className="text-teal-400" />
            Market Data Query Parameters
          </h2>

          <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
            {/* Instrument Selector */}
            <div className="space-y-1.5">
              <label htmlFor="instrument-select" className="text-xs font-semibold text-slate-300">
                Instrument
              </label>
              <select
                id="instrument-select"
                value={selectedInstrument}
                onChange={(e) => setSelectedInstrument(e.target.value)}
                className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-xs text-slate-200 focus:outline-none focus:ring-1 focus:ring-teal-500"
              >
                {instruments.map((inst) => (
                  <option key={inst.instrument_key} value={inst.instrument_key}>
                    {inst.name} ({inst.exchange} - {inst.segment})
                  </option>
                ))}
              </select>
            </div>

            {/* Timeframe Selector */}
            <div className="space-y-1.5">
              <label htmlFor="timeframe-select" className="text-xs font-semibold text-slate-300">
                Timeframe
              </label>
              <select
                id="timeframe-select"
                value={timeframe}
                onChange={(e) => setTimeframe(e.target.value)}
                className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-xs text-slate-200 focus:outline-none focus:ring-1 focus:ring-teal-500"
              >
                <option value="5m">5 Minutes (5m)</option>
                <option value="15m">15 Minutes (15m)</option>
              </select>
            </div>

            {/* Mode Selector */}
            <div className="space-y-1.5">
              <label htmlFor="mode-select" className="text-xs font-semibold text-slate-300">
                Acquisition Mode
              </label>
              <select
                id="mode-select"
                value={mode}
                onChange={(e) => setMode(e.target.value as "intraday" | "historical")}
                className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-xs text-slate-200 focus:outline-none focus:ring-1 focus:ring-teal-500"
              >
                <option value="intraday">Current-Day Intraday</option>
                <option value="historical">Historical Range</option>
              </select>
            </div>

            {/* Fetch Action Button */}
            <div className="flex items-end">
              <button
                id="fetch-candles-btn"
                onClick={handleFetchCandles}
                disabled={!canAcquire}
                className={`w-full py-2 px-4 rounded-lg font-bold text-xs flex items-center justify-center gap-2 shadow transition ${
                  !canAcquire
                    ? "bg-slate-800 text-slate-500 cursor-not-allowed"
                    : "bg-teal-600 hover:bg-teal-500 text-white"
                }`}
              >
                <Activity size={14} className={fetching ? "animate-spin" : ""} />
                {fetching ? "Acquiring Candles..." : "Acquire Completed Candles"}
              </button>
            </div>
          </div>

          {/* Historical Date Range Pickers (conditionally rendered) */}
          {mode === "historical" && (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4 pt-3 border-t border-slate-800/80">
              <div className="space-y-1.5">
                <label htmlFor="from-date-input" className="text-xs font-semibold text-slate-300 flex items-center gap-1.5">
                  <Calendar size={13} className="text-teal-400" />
                  From Date (YYYY-MM-DD)
                </label>
                <input
                  id="from-date-input"
                  type="date"
                  value={fromDate}
                  onChange={(e) => setFromDate(e.target.value)}
                  className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-xs text-slate-200 focus:outline-none focus:ring-1 focus:ring-teal-500"
                />
              </div>

              <div className="space-y-1.5">
                <label htmlFor="to-date-input" className="text-xs font-semibold text-slate-300 flex items-center gap-1.5">
                  <Calendar size={13} className="text-teal-400" />
                  To Date (YYYY-MM-DD, max 30 days)
                </label>
                <input
                  id="to-date-input"
                  type="date"
                  value={toDate}
                  onChange={(e) => setToDate(e.target.value)}
                  className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-xs text-slate-200 focus:outline-none focus:ring-1 focus:ring-teal-500"
                />
              </div>
            </div>
          )}

          {/* Offline / Operator Restriction Explanatory Notice */}
          {!canAcquire && !loading && (
            <div className="bg-amber-950/20 border border-amber-900/50 rounded-lg p-3 text-xs text-amber-300 flex items-center gap-2">
              <AlertTriangle size={15} className="shrink-0 text-amber-400" />
              <span>
                {readiness?.network_enabled === false
                  ? "External network calls are disabled by server policy (`UPSTOX_MARKET_DATA_ENABLED=false`). Enable to query real provider endpoints."
                  : readiness?.status === "OPERATOR_NOT_CONFIGURED"
                  ? "Market data operator (UPSTOX_MARKET_DATA_OWNER_ID) is not configured on the server."
                  : readiness?.status === "FORBIDDEN_OPERATOR"
                  ? "Current authenticated user is not authorized as the configured market data operator."
                  : readiness?.credential_configured === false
                  ? "Provider access token (UPSTOX_MARKET_DATA_ACCESS_TOKEN) is not configured on the server."
                  : "Acquisition disabled until readiness checks pass."}
              </span>
            </div>
          )}

          {/* Error Message Alert */}
          {error && (
            <div className="bg-rose-950/20 border border-rose-900/50 rounded-lg p-3 text-xs text-rose-300 flex items-center gap-2">
              <ShieldAlert size={15} className="shrink-0 text-rose-400" />
              <span>{error}</span>
            </div>
          )}
        </div>

        {/* Provenance Card (when candles exist) */}
        {provenance && (
          <div className="bg-slate-900/40 border border-slate-800 rounded-xl p-5 space-y-4">
            <div className="flex flex-col md:flex-row md:items-center justify-between gap-3 border-b border-slate-800/80 pb-3">
              <div>
                <h3 className="text-sm font-bold text-white flex items-center gap-2">
                  <ShieldCheck className="text-teal-400" size={16} />
                  Dataset Provenance (SHA-256 Digest)
                </h3>
                <p className="text-xs text-slate-400 mt-0.5">
                  Canonical SHA-256 content digest computed across {provenance.candle_count} completed candles.
                </p>
              </div>
              <div className="flex items-center gap-2">
                <span className="text-[11px] px-2 py-0.5 rounded font-mono font-bold bg-teal-950/60 border border-teal-500/40 text-teal-300">
                  {provenance.source_type}
                </span>
                {renderCompletenessBadge()}
              </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-xs">
              <div className="space-y-1">
                <span className="text-slate-400 font-medium">Content Fingerprint (SHA-256):</span>
                <div className="flex items-center gap-2 bg-slate-950 border border-slate-800 rounded-lg px-3 py-1.5 font-mono text-[11px] text-teal-300 overflow-x-auto">
                  <span className="truncate">{provenance.content_fingerprint}</span>
                  <button
                    onClick={() => copyToClipboard(provenance.content_fingerprint)}
                    className="shrink-0 text-slate-400 hover:text-white transition"
                    title="Copy SHA-256"
                  >
                    <Copy size={12} />
                  </button>
                  {copiedFingerprint && <span className="text-[10px] text-emerald-400">Copied!</span>}
                </div>
              </div>

              <div className="space-y-1">
                <span className="text-slate-400 font-medium">Retrieval Timestamp:</span>
                <div className="bg-slate-950 border border-slate-800 rounded-lg px-3 py-1.5 font-mono text-[11px] text-slate-300">
                  {new Date(provenance.retrieved_at).toUTCString()}
                </div>
              </div>
            </div>

            {/* Warnings list if any */}
            {provenance.warnings.length > 0 && (
              <div className="mt-3 p-3 bg-slate-950 border border-amber-900/30 rounded-lg space-y-1">
                <span className="text-[11px] font-bold text-amber-400">Provenance Warnings & Adjustments:</span>
                <ul className="list-disc list-inside text-[11px] text-slate-400 space-y-0.5">
                  {provenance.warnings.map((w, idx) => (
                    <li key={idx}>{w}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}

        {/* Candle Data Table */}
        <div className="bg-slate-900/40 border border-slate-800 rounded-2xl overflow-hidden shadow-sm">
          <div className="p-4 border-b border-slate-800 flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Clock size={16} className="text-teal-400" />
              <h3 className="text-sm font-bold text-white">Completed Candles Table</h3>
              <span className="text-xs text-slate-400">({candles.length} records)</span>
            </div>
            <div className="text-xs text-slate-400">
              Loaded Timeframe: <span className="font-bold text-white">{loadedMetadata ? loadedMetadata.timeframe : timeframe}</span>
              {loadedMetadata && (
                <span className="ml-3 font-mono text-teal-400">[{loadedMetadata.instrument_key}]</span>
              )}
            </div>
          </div>

          {fetching ? (
            <div className="py-16 flex flex-col items-center justify-center gap-3">
              <div className="w-7 h-7 border-2 border-teal-500 border-t-transparent rounded-full animate-spin"></div>
              <span className="text-xs text-slate-400">Acquiring and validating provider candles...</span>
            </div>
          ) : candles.length === 0 ? (
            <div className="py-16 text-center text-slate-500 text-xs">
              No completed candle data currently loaded. Configure query parameters and select &quot;Acquire Completed Candles&quot;.
            </div>
          ) : (
            <div className="overflow-x-auto max-h-[500px]">
              <table className="w-full text-left text-xs" role="table">
                <thead className="bg-slate-950/70 border-b border-slate-800 sticky top-0 text-[11px] text-slate-400 uppercase tracking-wider">
                  <tr>
                    <th scope="col" className="px-4 py-2.5">Open Time (UTC)</th>
                    <th scope="col" className="px-4 py-2.5 text-right">Open</th>
                    <th scope="col" className="px-4 py-2.5 text-right">High</th>
                    <th scope="col" className="px-4 py-2.5 text-right">Low</th>
                    <th scope="col" className="px-4 py-2.5 text-right">Close</th>
                    <th scope="col" className="px-4 py-2.5 text-right">Change</th>
                    <th scope="col" className="px-4 py-2.5 text-right">Volume</th>
                    <th scope="col" className="px-4 py-2.5 text-center">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800/60 font-mono">
                  {candles.map((c, i) => {
                    const oNum = typeof c.open === "string" ? parseFloat(c.open) : c.open;
                    const cNum = typeof c.close === "string" ? parseFloat(c.close) : c.close;
                    const change = cNum - oNum;
                    const pctChange = oNum > 0 ? (change / oNum) * 100 : 0;
                    const isBull = change >= 0;

                    return (
                      <tr key={i} className="hover:bg-slate-800/30 transition">
                        <td className="px-4 py-2 text-slate-300 font-sans text-xs">
                          {c.timestamp.replace("T", " ").replace("+00:00", "Z")}
                        </td>
                        <td className="px-4 py-2 text-right text-slate-200">{formatPrice(c.open)}</td>
                        <td className="px-4 py-2 text-right text-slate-200">{formatPrice(c.high)}</td>
                        <td className="px-4 py-2 text-right text-slate-200">{formatPrice(c.low)}</td>
                        <td className="px-4 py-2 text-right font-bold text-slate-100">{formatPrice(c.close)}</td>
                        <td className={`px-4 py-2 text-right flex items-center justify-end gap-1 ${
                          isBull ? "text-emerald-400" : "text-rose-400"
                        }`}>
                          {isBull ? <TrendingUp size={12} /> : <TrendingDown size={12} />}
                          <span>{pctChange >= 0 ? "+" : ""}{pctChange.toFixed(2)}%</span>
                        </td>
                        <td className="px-4 py-2 text-right text-slate-400">{c.volume.toLocaleString()}</td>
                        <td className="px-4 py-2 text-center">
                          <span className="text-[10px] font-sans px-1.5 py-0.5 rounded font-semibold bg-emerald-950/60 border border-emerald-500/40 text-emerald-300">
                            Completed
                          </span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
