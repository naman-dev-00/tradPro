"use client";

import React from "react";
import { StrategyRuntime, KillSwitchStatus, SandboxReadinessResponse } from "../../lib/api";

interface Props {
  runtime: StrategyRuntime | null;
  killSwitch: KillSwitchStatus | null;
  readiness?: SandboxReadinessResponse | null;
  onStart: () => void;
  onPause: () => void;
  onResume: () => void;
  onStop: () => void;
  onStep: (steps: number) => void;
  onOpenKillSwitch: () => void;
  loading: boolean;
}

export const RuntimeControlHeader: React.FC<Props> = ({
  runtime,
  killSwitch,
  readiness,
  onStart,
  onPause,
  onResume,
  onStop,
  onStep,
  onOpenKillSwitch,
  loading,
}) => {
  const isKillSwitchActive = killSwitch?.global_active || killSwitch?.user_active;
  const isSandbox = runtime?.trading_mode === "BROKER_SANDBOX";
  const isFixture = runtime?.trading_mode === "BROKER_SANDBOX_RECORDED_FIXTURE";
  const submissionBlocked = isKillSwitchActive || (isSandbox && readiness !== null && readiness !== undefined && !readiness.ready_for_submission);

  return (
    <div className="space-y-4">
      {/* Prominent Mandatory Disclosure Banner */}
      {isSandbox ? (
        <div
          id="sandbox-transmission-banner"
          className="bg-amber-950/40 border border-amber-600/70 rounded-lg p-3 text-center space-y-1.5"
        >
          <span className="text-amber-300 font-bold tracking-wider text-sm sm:text-base flex items-center justify-center gap-2">
            <svg className="w-5 h-5 text-amber-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
            </svg>
            UPSTOX SANDBOX MODE — EXTERNAL ORDER TRANSMISSION
          </span>
          <div className="text-xs text-amber-200/90 space-y-1 max-w-4xl mx-auto">
            <p>
              External network transmission is{" "}
              <span className={`font-bold ${readiness?.network_enabled ? "text-emerald-400" : "text-amber-400"}`}>
                {readiness?.network_enabled ? "ENABLED" : "DISABLED"}
              </span>
              . The operator is responsible for supplying a Sandbox Apps token.
            </p>
            <p className="text-amber-300/70 text-[11px]">
              TradePro cannot independently verify the token’s sandbox scope. A wrongly provisioned token could create unintended live-market exposure. The current environment is restricted to local/test execution.
            </p>
          </div>
        </div>
      ) : isFixture ? (
        <div
          id="sandbox-fixture-banner"
          className="bg-blue-950/40 border border-blue-600/60 rounded-lg p-3 text-center"
        >
          <span className="text-blue-300 font-bold tracking-wider text-sm sm:text-base flex items-center justify-center gap-2">
            UPSTOX SANDBOX FIXTURE MODE — LOCAL SIMULATION ONLY
          </span>
          <p className="text-xs text-blue-200/70 mt-1">
            Replaying recorded Upstox sandbox order responses offline. Zero external network transmission.
          </p>
        </div>
      ) : (
        <div
          id="paper-simulation-banner"
          className="bg-slate-900/60 border border-slate-700/60 rounded-lg p-3 text-center"
        >
          <span className="text-slate-300 font-bold tracking-wider text-sm sm:text-base flex items-center justify-center gap-2">
            <svg className="w-5 h-5 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            PAPER SIMULATION — LOCAL IN-MEMORY EXECUTION
          </span>
          <p className="text-xs text-slate-400 mt-1">
            Simulated in-memory execution environment. Zero external broker connectivity.
          </p>
        </div>
      )}

      {/* Kill Switch Alert Bar if engaged */}
      {isKillSwitchActive && (
        <div
          id="kill-switch-active-alert"
          className="bg-rose-950/50 border border-rose-600 rounded-lg p-3 flex items-center justify-between"
        >
          <div className="flex items-center gap-2 text-rose-300 font-semibold text-sm">
            <span className="inline-block w-2.5 h-2.5 rounded-full bg-rose-500 animate-ping" />
            <span>EMERGENCY KILL SWITCH ENGAGED ({killSwitch?.global_active ? "GLOBAL" : "USER"})</span>
          </div>
          <button
            onClick={onOpenKillSwitch}
            className="px-3 py-1 bg-rose-700 hover:bg-rose-600 text-white text-xs rounded transition-colors"
          >
            Manage / Reset
          </button>
        </div>
      )}

      {/* Controls and Status Row */}
      <div className="bg-slate-900 border border-slate-800 rounded-lg p-4 flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <span className="text-sm text-slate-400">Runtime Status:</span>
          {runtime ? (
            <span
              id="runtime-status-badge"
              className={`px-2.5 py-1 text-xs font-semibold rounded-full ${
                runtime.status === "RUNNING"
                  ? "bg-emerald-950 text-emerald-300 border border-emerald-700"
                  : runtime.status === "READY"
                  ? "bg-blue-950 text-blue-300 border border-blue-700"
                  : runtime.status === "PAUSED"
                  ? "bg-amber-950 text-amber-300 border border-amber-700"
                  : runtime.status === "HALTED" || runtime.status === "ERROR"
                  ? "bg-rose-950 text-rose-300 border border-rose-700"
                  : "bg-slate-800 text-slate-300 border border-slate-700"
              }`}
            >
              {runtime.status}
            </span>
          ) : (
            <span className="text-xs text-slate-400">No Runtime Selected</span>
          )}

          {runtime?.trading_mode && (
            <span
              id="trading-mode-badge"
              className={`px-2 py-0.5 text-[11px] font-mono rounded border ${
                isSandbox
                  ? "bg-amber-950/60 text-amber-300 border-amber-700"
                  : isFixture
                  ? "bg-blue-950/60 text-blue-300 border-blue-700"
                  : "bg-slate-800 text-slate-300 border-slate-700"
              }`}
            >
              {runtime.trading_mode}
            </span>
          )}

          {runtime?.last_processed_candle_timestamp && (
            <span className="text-xs text-slate-400 hidden sm:inline">
              Last Bar: {new Date(runtime.last_processed_candle_timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
            </span>
          )}
        </div>

        {/* Action Buttons */}
        <div className="flex flex-wrap items-center gap-2">
          {runtime?.status === "READY" && (
            <button
              id="start-runtime-btn"
              disabled={loading || submissionBlocked}
              onClick={onStart}
              className="px-3 py-1.5 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white text-xs font-medium rounded transition-colors"
            >
              Start
            </button>
          )}

          {runtime?.status === "RUNNING" && (
            <>
              <button
                id="pause-runtime-btn"
                disabled={loading}
                onClick={onPause}
                className="px-3 py-1.5 bg-amber-600 hover:bg-amber-500 text-white text-xs font-medium rounded transition-colors"
              >
                Pause
              </button>

              <button
                id="step-1-btn"
                disabled={loading || submissionBlocked}
                onClick={() => onStep(1)}
                className="px-3 py-1.5 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white text-xs font-medium rounded transition-colors"
              >
                Step 1
              </button>

              <button
                id="step-5-btn"
                disabled={loading || submissionBlocked}
                onClick={() => onStep(5)}
                className="px-3 py-1.5 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white text-xs font-medium rounded transition-colors hidden sm:inline-block"
              >
                Step 5
              </button>
            </>
          )}

          {runtime?.status === "PAUSED" && (
            <button
              id="resume-runtime-btn"
              disabled={loading || submissionBlocked}
              onClick={onResume}
              className="px-3 py-1.5 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white text-xs font-medium rounded transition-colors"
            >
              Resume
            </button>
          )}

          {(runtime?.status === "RUNNING" || runtime?.status === "PAUSED" || runtime?.status === "HALTED") && (
            <button
              id="stop-runtime-btn"
              disabled={loading}
              onClick={onStop}
              className="px-3 py-1.5 bg-slate-700 hover:bg-slate-600 text-white text-xs font-medium rounded transition-colors"
            >
              Stop
            </button>
          )}

          <button
            id="kill-switch-btn"
            onClick={onOpenKillSwitch}
            className="px-3 py-1.5 bg-rose-600 hover:bg-rose-500 text-white text-xs font-bold rounded transition-colors ml-2 flex items-center gap-1.5"
          >
            <span className="w-2 h-2 rounded-full bg-white" />
            Kill Switch
          </button>
        </div>
      </div>
    </div>
  );
};
