"use client";

import React, { useState, useEffect, useRef } from "react";
import { KillSwitchStatus } from "../../lib/api";

interface Props {
  status: KillSwitchStatus | null;
  onEngage: (scope: "GLOBAL" | "USER", reason: string) => Promise<void>;
  onReset: (scope: "GLOBAL" | "USER", reason: string) => Promise<void>;
  onClose: () => void;
  isAdmin?: boolean;
}

export const KillSwitchModal: React.FC<Props> = ({
  status,
  onEngage,
  onReset,
  onClose,
  isAdmin = false,
}) => {
  const [scope, setScope] = useState<"GLOBAL" | "USER">("USER");
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState<string>("");

  const modalRef = useRef<HTMLDivElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const previousActiveElement = useRef<Element | null>(null);

  useEffect(() => {
    previousActiveElement.current = document.activeElement;
    // Set initial focus
    closeButtonRef.current?.focus();

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
        return;
      }
      if (e.key === "Tab" && modalRef.current) {
        const focusableElements = modalRef.current.querySelectorAll<HTMLElement>(
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
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      if (previousActiveElement.current instanceof HTMLElement) {
        previousActiveElement.current.focus();
      }
    };
  }, [onClose]);

  const effectiveScope = isAdmin ? scope : "USER";
  const isActive = effectiveScope === "GLOBAL" ? status?.global_active : status?.user_active;

  const handleAction = async () => {
    if (!reason.trim() || reason.length < 3) {
      const err = "Please provide a valid justification reason (at least 3 characters).";
      setError(err);
      setAnnouncement(err);
      return;
    }
    if (!confirmed) {
      const err = "Please check the confirmation box to proceed.";
      setError(err);
      setAnnouncement(err);
      return;
    }

    setError(null);
    setLoading(true);
    setAnnouncement("Processing emergency kill switch update...");
    try {
      if (isActive) {
        await onReset(effectiveScope, reason);
        setAnnouncement("Kill switch successfully reset.");
      } else {
        await onEngage(effectiveScope, reason);
        setAnnouncement("Emergency kill switch successfully engaged.");
      }
      onClose();
    } catch (err: any) {
      const msg = err.message || "Operation failed.";
      setError(msg);
      setAnnouncement(`Error: ${msg}`);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div
      id="kill-switch-modal"
      role="dialog"
      aria-modal="true"
      aria-labelledby="kill-switch-title"
      aria-describedby="kill-switch-description"
      className="fixed inset-0 z-50 bg-slate-950/80 flex items-center justify-center p-4"
    >
      <div
        ref={modalRef}
        className="bg-slate-900 border border-slate-800 rounded-xl max-w-md w-full p-5 space-y-4 shadow-2xl"
      >
        <div className="flex items-center justify-between border-b border-slate-800 pb-3">
          <div className="flex items-center gap-2">
            <span className="w-3 h-3 rounded-full bg-rose-500 animate-pulse" aria-hidden="true" />
            <h3 id="kill-switch-title" className="text-sm font-bold text-white">
              Emergency Kill Switch Control
            </h3>
          </div>
          <button
            ref={closeButtonRef}
            onClick={onClose}
            aria-label="Close kill switch dialog"
            className="text-slate-400 hover:text-white text-lg font-bold"
          >
            ✕
          </button>
        </div>

        <p id="kill-switch-description" className="text-xs text-slate-400">
          Engaging halts execution and cancels in-flight orders. Resetting restores system readiness.
        </p>

        {/* Live region for screen-reader announcements */}
        <div aria-live="polite" aria-atomic="true" className="sr-only">
          {announcement}
        </div>

        {error && (
          <div role="alert" className="bg-rose-950/50 border border-rose-800 text-rose-300 text-xs p-2.5 rounded">
            {error}
          </div>
        )}

        <div className="space-y-3 text-xs">
          <div>
            <label className="block text-slate-400 mb-1 font-medium">Control Scope</label>
            <div className={isAdmin ? "grid grid-cols-2 gap-2" : "grid grid-cols-1 gap-2"}>
              <button
                id="scope-user-btn"
                type="button"
                onClick={() => setScope("USER")}
                className={`py-2 px-3 rounded border text-center font-medium transition-colors ${
                  effectiveScope === "USER"
                    ? "bg-slate-800 border-slate-600 text-white"
                    : "bg-slate-950 border-slate-800 text-slate-400"
                }`}
              >
                My Account Runtimes
              </button>
              {isAdmin && (
                <button
                  id="scope-global-btn"
                  type="button"
                  onClick={() => setScope("GLOBAL")}
                  className={`py-2 px-3 rounded border text-center font-medium transition-colors ${
                    effectiveScope === "GLOBAL"
                      ? "bg-rose-950 border-rose-700 text-rose-300"
                      : "bg-slate-950 border-slate-800 text-slate-400"
                  }`}
                >
                  GLOBAL (All Users)
                </button>
              )}
            </div>
          </div>

          <div className="bg-slate-950 p-3 rounded border border-slate-800">
            <span className="text-slate-400">Current Status:</span>{" "}
            <span className={`font-bold ${isActive ? "text-rose-400" : "text-emerald-400"}`}>
              {isActive ? "ACTIVE (ENGAGED)" : "INACTIVE (NORMAL TRADING)"}
            </span>
            {isActive && (
              <p className="text-[11px] text-slate-400 mt-1">
                Reason: {effectiveScope === "GLOBAL" ? status?.global_reason : status?.user_reason}
              </p>
            )}
          </div>

          <div>
            <label htmlFor="kill-reason-input" className="block text-slate-400 mb-1 font-medium">
              Audit Justification Reason <span className="text-rose-400">*</span>
            </label>
            <textarea
              id="kill-reason-input"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="Enter mandatory justification for audit trail..."
              rows={3}
              className="w-full bg-slate-950 border border-slate-700 rounded p-2 text-slate-200 focus:outline-none focus:border-slate-500 resize-none text-xs"
            />
          </div>

          <div className="flex items-start gap-2 pt-1">
            <input
              id="kill-switch-confirm-check"
              type="checkbox"
              checked={confirmed}
              onChange={(e) => setConfirmed(e.target.checked)}
              className="mt-0.5 rounded border-slate-700 bg-slate-950 text-rose-500 focus:ring-rose-400"
            />
            <label htmlFor="kill-switch-confirm-check" className="text-slate-300 text-xs">
              I acknowledge that this action will immediately alter runtime state and cancel open orders.
            </label>
          </div>
        </div>

        <div className="flex justify-end gap-2 pt-2 border-t border-slate-800">
          <button
            onClick={onClose}
            className="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs rounded"
          >
            Cancel
          </button>
          <button
            id="confirm-kill-switch-btn"
            disabled={loading || !confirmed}
            onClick={handleAction}
            className={`px-4 py-1.5 text-xs font-bold rounded transition-colors text-white ${
              loading || !confirmed
                ? "bg-slate-700 opacity-50 cursor-not-allowed"
                : isActive
                ? "bg-emerald-600 hover:bg-emerald-500"
                : "bg-rose-600 hover:bg-rose-500"
            }`}
          >
            {loading ? "Processing..." : isActive ? "Reset Kill Switch" : "ENGAGE KILL SWITCH"}
          </button>
        </div>
      </div>
    </div>
  );
};
