"use client";

import React, { useEffect, useRef } from "react";
import { Order } from "../../lib/api";

interface Props {
  order: Order | null;
  onClose: () => void;
}

export const AuditTimelineModal: React.FC<Props> = ({ order, onClose }) => {
  const modalRef = useRef<HTMLDivElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const previousActiveElement = useRef<Element | null>(null);

  useEffect(() => {
    previousActiveElement.current = document.activeElement;
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

  if (!order) return null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="audit-timeline-title"
      aria-describedby="audit-timeline-description"
      className="fixed inset-0 z-50 bg-slate-950/80 flex items-center justify-center p-4"
    >
      <div
        ref={modalRef}
        className="bg-slate-900 border border-slate-800 rounded-xl max-w-lg w-full p-5 space-y-4 shadow-2xl"
      >
        <div className="flex items-center justify-between border-b border-slate-800 pb-3">
          <div>
            <h3 id="audit-timeline-title" className="text-sm font-bold text-white">
              Order Audit Trail #{order.order_sequence_number}
            </h3>
            <p id="audit-timeline-description" className="text-xs text-slate-400 font-mono">
              Immutable state history for order ID: {order.id}
            </p>
          </div>
          <button
            ref={closeButtonRef}
            onClick={onClose}
            aria-label="Close audit trail dialog"
            className="text-slate-400 hover:text-white text-lg font-bold"
          >
            ✕
          </button>
        </div>

        {/* Order Details Header */}
        <div className="grid grid-cols-2 gap-2 text-xs bg-slate-950/50 p-3 rounded border border-slate-800">
          <div><span className="text-slate-400">Instrument:</span> {order.instrument_id}</div>
          <div><span className="text-slate-400">Side:</span> <span className="font-bold">{order.side}</span></div>
          <div><span className="text-slate-400">Quantity:</span> {order.quantity}</div>
          <div><span className="text-slate-400">Filled:</span> {order.filled_quantity}</div>
          <div><span className="text-slate-400">Status:</span> <span className="font-semibold">{order.status}</span></div>
          <div><span className="text-slate-400">Limit Price:</span> {order.limit_price ? `₹${order.limit_price}` : "MARKET"}</div>
        </div>

        {/* State Transitions Timeline */}
        <div className="space-y-2">
          <h4 className="text-xs font-semibold text-slate-300 uppercase tracking-wider">Immutable Transition Events</h4>
          {(!order.events || order.events.length === 0) ? (
            <div className="text-xs text-slate-400 py-3 text-center">No transition events recorded.</div>
          ) : (
            <div className="space-y-2 max-h-60 overflow-y-auto pr-1">
              {order.events.map((evt) => (
                <div key={evt.id} className="text-xs bg-slate-800/40 p-2.5 rounded border border-slate-800 flex items-start justify-between">
                  <div>
                    <div className="flex items-center gap-2 font-mono text-[11px]">
                      <span className="text-slate-400">{evt.previous_status}</span>
                      <span className="text-slate-600">→</span>
                      <span className="font-bold text-emerald-400">{evt.new_status}</span>
                    </div>
                    <div className="text-[11px] text-slate-400 mt-1">
                      Reason: <span className="text-slate-300 font-mono">{evt.reason_code}</span> | Actor: {evt.actor}
                    </div>
                  </div>
                  <span className="text-[10px] text-slate-400">
                    {new Date(evt.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="flex justify-end pt-2">
          <button
            onClick={onClose}
            className="px-4 py-1.5 bg-slate-800 hover:bg-slate-700 text-white text-xs rounded font-medium"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
};
