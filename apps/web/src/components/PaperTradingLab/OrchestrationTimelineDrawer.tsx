"use client";

import React, { useEffect, useState, useRef } from "react";
import {
  RuntimeEvaluationSummaryResponse,
  RuntimeEvaluationDetailResponse,
  fetchEvaluationHistory,
  fetchEvaluationDetail,
} from "../../lib/api";

interface Props {
  runtimeId: string | null;
  isOpen: boolean;
  onClose: () => void;
}

export const OrchestrationTimelineDrawer: React.FC<Props> = ({
  runtimeId,
  isOpen,
  onClose,
}) => {
  const [evaluations, setEvaluations] = useState<RuntimeEvaluationSummaryResponse[]>([]);
  const [selectedEval, setSelectedEval] = useState<RuntimeEvaluationDetailResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const drawerRef = useRef<HTMLDivElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const previousActiveElement = useRef<Element | null>(null);

  useEffect(() => {
    if (!isOpen || !runtimeId) {
      setEvaluations([]);
      setSelectedEval(null);
      return;
    }

    previousActiveElement.current = document.activeElement;
    closeButtonRef.current?.focus();

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
        return;
      }
      if (e.key === "Tab" && drawerRef.current) {
        const focusableElements = drawerRef.current.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
        );
        if (focusableElements.length === 0) return;

        const firstElement = focusableElements[0];
        const lastElement = focusableElements[focusableElements.length - 1];

        if (e.shiftKey) {
          if (document.activeElement === firstElement) {
            e.preventDefault();
            lastElement.focus();
          }
        } else {
          if (document.activeElement === lastElement) {
            e.preventDefault();
            firstElement.focus();
          }
        }
      }
    };

    window.addEventListener("keydown", handleKeyDown);

    setLoading(true);
    setError(null);
    fetchEvaluationHistory(runtimeId, 50, 0)
      .then((data) => {
        setEvaluations(data.evaluations || []);
        if (data.evaluations && data.evaluations.length > 0) {
          fetchEvaluationDetail(runtimeId, data.evaluations[0].id)
            .then(setSelectedEval)
            .catch(() => {});
        }
      })
      .catch((err) => {
        setError(err.message || "Failed to load orchestration evaluations");
      })
      .finally(() => setLoading(false));

    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      if (previousActiveElement.current instanceof HTMLElement) {
        previousActiveElement.current.focus();
      }
    };
  }, [isOpen, runtimeId, onClose]);

  const handleSelectEval = (evalId: string) => {
    if (!runtimeId) return;
    fetchEvaluationDetail(runtimeId, evalId)
      .then(setSelectedEval)
      .catch((err) => setError(err.message));
  };

  const renderStatusBadge = (status: string) => {
    switch (status) {
      case "TRUE":
        return (
          <span
            data-testid="status-badge-true"
            className="px-2 py-0.5 text-[10px] font-bold rounded bg-emerald-950 text-emerald-300 border border-emerald-700"
          >
            TRUE
          </span>
        );
      case "FALSE":
        return (
          <span
            data-testid="status-badge-false"
            className="px-2 py-0.5 text-[10px] font-bold rounded bg-slate-800 text-slate-300 border border-slate-700"
          >
            FALSE
          </span>
        );
      case "UNAVAILABLE":
        return (
          <span
            data-testid="status-badge-unavailable"
            className="px-2 py-0.5 text-[10px] font-bold rounded bg-amber-950 text-amber-300 border border-amber-700"
          >
            UNAVAILABLE
          </span>
        );
      case "INVALID":
        return (
          <span
            data-testid="status-badge-invalid"
            className="px-2 py-0.5 text-[10px] font-bold rounded bg-rose-950 text-rose-300 border border-rose-700"
          >
            INVALID
          </span>
        );
      default:
        return (
          <span className="px-2 py-0.5 text-[10px] font-bold rounded bg-slate-800 text-slate-400 border border-slate-700">
            {status}
          </span>
        );
    }
  };

  if (!isOpen) return null;

  return (
    <div
      ref={drawerRef}
      role="dialog"
      aria-modal="true"
      aria-label="Orchestration Timeline Drawer"
      className="fixed inset-y-0 right-0 z-50 w-full max-w-xl bg-slate-900 border-l border-slate-800 shadow-2xl flex flex-col focus:outline-none"
    >
      <div className="p-4 border-b border-slate-800 flex items-center justify-between">
        <div>
          <h2 className="text-sm font-bold text-white">Automated Orchestration Timeline</h2>
          <p className="text-xs text-slate-400 font-mono">Runtime ID: {runtimeId}</p>
        </div>
        <button
          ref={closeButtonRef}
          onClick={onClose}
          aria-label="Close drawer"
          className="text-slate-400 hover:text-white text-lg font-bold p-1 rounded focus:outline-none focus:ring-2 focus:ring-indigo-500"
        >
          ✕
        </button>
      </div>

      {loading && (
        <div className="p-6 text-center text-xs text-slate-400">Loading evaluations...</div>
      )}

      {error && (
        <div
          role="alert"
          className="m-4 p-3 bg-rose-950/50 border border-rose-800 rounded text-xs text-rose-300"
        >
          {error}
        </div>
      )}

      {!loading && !error && evaluations.length === 0 && (
        <div className="p-6 text-center text-xs text-slate-400">
          No evaluations recorded yet for this runtime.
        </div>
      )}

      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {evaluations.length > 0 && (
          <div className="space-y-2">
            <h3 className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Evaluation Steps</h3>
            <div className="space-y-1.5 max-h-48 overflow-y-auto border border-slate-800 rounded p-2 bg-slate-950/40">
              {evaluations.map((ev) => {
                const isSelected = selectedEval?.id === ev.id;
                return (
                  <button
                    key={ev.id}
                    onClick={() => handleSelectEval(ev.id)}
                    className={`w-full text-left p-2 rounded text-xs transition-colors flex items-center justify-between ${
                      isSelected
                        ? "bg-slate-800 text-white font-medium border border-indigo-500/50"
                        : "text-slate-300 hover:bg-slate-800/50"
                    }`}
                  >
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-slate-300">
                        {new Date(ev.close_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
                      </span>
                      {renderStatusBadge(ev.evaluation_status)}
                    </div>
                    <span
                      className={`px-1.5 py-0.5 text-[10px] rounded ${
                        ev.action_outcome === "ACCEPTED_INTERNAL"
                          ? "bg-emerald-950 text-emerald-300 border border-emerald-800"
                          : ev.action_outcome === "REJECTED"
                          ? "bg-rose-950 text-rose-300 border border-rose-800"
                          : "bg-slate-800 text-slate-400"
                      }`}
                    >
                      {ev.action_outcome}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>
        )}

        {selectedEval && (
          <div className="space-y-3 border-t border-slate-800 pt-3">
            <h3 className="text-xs font-semibold text-slate-300">Evaluation Detail</h3>
            <div className="grid grid-cols-2 gap-2 text-xs bg-slate-950/60 p-3 rounded border border-slate-800">
              <div><span className="text-slate-400">Close Boundary:</span> {new Date(selectedEval.close_at).toISOString()}</div>
              <div><span className="text-slate-400">Timeframe:</span> {selectedEval.timeframe}</div>
              <div className="flex items-center gap-1.5">
                <span className="text-slate-400">Status:</span>
                {renderStatusBadge(selectedEval.evaluation_status)}
              </div>
              <div><span className="text-slate-400">Risk Outcome:</span> <span className="font-bold">{selectedEval.risk_outcome}</span></div>
              {selectedEval.no_order_reason && (
                <div className="col-span-2"><span className="text-slate-400">Reason:</span> <span className="text-amber-300 font-mono">{selectedEval.no_order_reason}</span></div>
              )}
              <div className="col-span-2 font-mono text-[10px] text-slate-400 truncate">
                Fingerprint: {selectedEval.evaluation_fingerprint}
              </div>
            </div>

            {selectedEval.audit_json && (
              <div>
                <span className="text-[11px] text-slate-400 font-semibold">Rule Evaluation Audit Evidence:</span>
                <pre
                  data-testid="audit-json-evidence"
                  className="mt-1 p-2 bg-slate-950 rounded border border-slate-800 text-[10px] font-mono text-slate-300 overflow-x-auto max-h-36"
                >
                  {(() => {
                    try {
                      return JSON.stringify(JSON.parse(selectedEval.audit_json), null, 2);
                    } catch {
                      return selectedEval.audit_json;
                    }
                  })()}
                </pre>
              </div>
            )}

            {selectedEval.risk_summary_json && (
              <div>
                <span className="text-[11px] text-slate-400 font-semibold">Risk Summary Evidence:</span>
                <pre
                  data-testid="risk-summary-evidence"
                  className="mt-1 p-2 bg-slate-950 rounded border border-slate-800 text-[10px] font-mono text-slate-300 overflow-x-auto max-h-36"
                >
                  {(() => {
                    try {
                      return JSON.stringify(JSON.parse(selectedEval.risk_summary_json), null, 2);
                    } catch {
                      return selectedEval.risk_summary_json;
                    }
                  })()}
                </pre>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
};
