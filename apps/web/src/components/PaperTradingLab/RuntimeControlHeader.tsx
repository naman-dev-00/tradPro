"use client";

import React from "react";
import { StrategyRuntime, KillSwitchStatus } from "../../lib/api";

interface Props {
  runtime: StrategyRuntime | null;
  killSwitch: KillSwitchStatus | null;
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
  onStart,
  onPause,
  onResume,
  onStop,
  onStep,
  onOpenKillSwitch,
  loading,
}) => {
  const isKillSwitchActive = killSwitch?.global_active || killSwitch?.user_active;

  return (
    <div className="space-y-4">
      {/* Prominent Mandatory Simulation Disclaimer */}
      <div
        id="paper-simulation-banner"
        className="bg-amber-950/40 border border-amber-600/50 rounded-lg p-3 text-center"
      >
        <span className="text-amber-300 font-bold tracking-wider text-sm sm:text-base flex items-center justify-center gap-2">
          <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
          </svg>
          PAPER SIMULATION — NO LIVE ORDERS
        </span>
        <p className="text-xs text-amber-200/70 mt-1">
          Simulated execution environment. Zero external broker connectivity. Results are educational and do not predict live market performance.
        </p>
      </div>

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
              disabled={loading || isKillSwitchActive}
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
                disabled={loading || isKillSwitchActive}
                onClick={() => onStep(1)}
                className="px-3 py-1.5 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white text-xs font-medium rounded transition-colors"
              >
                Step 1
              </button>

              <button
                id="step-5-btn"
                disabled={loading || isKillSwitchActive}
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
              disabled={loading || isKillSwitchActive}
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
