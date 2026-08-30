"use client";

import React, { useState } from "react";
import { CheckCircle2, AlertTriangle, HelpCircle, XCircle, ChevronDown, ChevronUp } from "lucide-react";
import { ReplayVerificationResult } from "@/lib/api";

interface VerificationCardProps {
  label: string;
  runId: string;
  verification: ReplayVerificationResult | null;
  loading: boolean;
  error?: string | null;
}

export const VerificationCard: React.FC<VerificationCardProps> = ({
  label,
  runId,
  verification,
  loading,
  error,
}) => {
  const [expanded, setExpanded] = useState(false);

  if (loading) {
    return (
      <div className="p-4 bg-slate-900/60 border border-slate-800 rounded-xl animate-pulse">
        <div className="h-4 bg-slate-700 rounded w-1/3 mb-2"></div>
        <div className="h-6 bg-slate-800 rounded w-1/2"></div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-4 bg-red-950/40 border border-red-800/60 rounded-xl text-red-300">
        <h4 className="font-semibold text-sm text-red-400 mb-1">{label} Verification</h4>
        <p className="text-xs">{error}</p>
      </div>
    );
  }

  if (!verification) {
    return null;
  }

  const status = verification.verification_status;

  const getStatusBadge = () => {
    switch (status) {
      case "VERIFIED":
        return (
          <span className="inline-flex items-center space-x-1 px-2.5 py-1 rounded-full text-xs font-semibold bg-emerald-950 text-emerald-300 border border-emerald-500/50">
            <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" aria-hidden="true" />
            <span>VERIFIED</span>
          </span>
        );
      case "MISMATCH":
        return (
          <span className="inline-flex items-center space-x-1 px-2.5 py-1 rounded-full text-xs font-semibold bg-amber-950 text-amber-300 border border-amber-500/50">
            <AlertTriangle className="w-3.5 h-3.5 text-amber-400" aria-hidden="true" />
            <span>MISMATCH</span>
          </span>
        );
      case "UNVERIFIABLE":
        return (
          <span className="inline-flex items-center space-x-1 px-2.5 py-1 rounded-full text-xs font-semibold bg-slate-800 text-slate-300 border border-slate-600/50">
            <HelpCircle className="w-3.5 h-3.5 text-slate-400" aria-hidden="true" />
            <span>UNVERIFIABLE</span>
          </span>
        );
      case "INVALID":
      default:
        return (
          <span className="inline-flex items-center space-x-1 px-2.5 py-1 rounded-full text-xs font-semibold bg-red-950 text-red-300 border border-red-500/50">
            <XCircle className="w-3.5 h-3.5 text-red-400" aria-hidden="true" />
            <span>INVALID</span>
          </span>
        );
    }
  };

  return (
    <div className="p-4 bg-slate-900/60 border border-slate-800 rounded-xl shadow-lg min-w-0">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2.5 mb-3 min-w-0">
        <div className="min-w-0 flex-1">
          <span className="text-xs uppercase font-medium text-slate-400 tracking-wider block">
            {label} Run
          </span>
          <span className="font-mono text-xs text-slate-300 truncate max-w-full block" title={runId}>
            {runId}
          </span>
        </div>
        <div className="shrink-0 self-start sm:self-center">
          {getStatusBadge()}
        </div>
      </div>

      <div className="space-y-1.5 text-xs text-slate-300 border-t border-slate-800/80 pt-2.5 min-w-0">
        <div className="flex flex-col sm:flex-row sm:justify-between sm:items-center py-0.5 gap-0.5 sm:gap-2 min-w-0">
          <span className="text-slate-400 break-words">Request Fingerprint Match:</span>
          <span className={verification.fingerprint_matches ? "text-emerald-400 font-medium shrink-0" : "text-amber-400 font-medium shrink-0"}>
            {verification.fingerprint_matches ? "Matches" : "Mismatch / Unavailable"}
          </span>
        </div>
        <div className="flex flex-col sm:flex-row sm:justify-between sm:items-center py-0.5 gap-0.5 sm:gap-2 min-w-0">
          <span className="text-slate-400 break-words">Manifest Version ({verification.current_manifest_version}):</span>
          <span className={verification.manifest_version_matches ? "text-emerald-400 font-medium shrink-0" : "text-amber-400 font-medium shrink-0"}>
            {verification.manifest_version_matches ? "Matches" : "Mismatch"}
          </span>
        </div>
        <div className="flex flex-col sm:flex-row sm:justify-between sm:items-center py-0.5 gap-0.5 sm:gap-2 min-w-0">
          <span className="text-slate-400 break-words">Engine Version ({verification.current_engine_version}):</span>
          <span className={verification.engine_version_matches ? "text-emerald-400 font-medium shrink-0" : "text-amber-400 font-medium shrink-0"}>
            {verification.engine_version_matches ? "Matches" : "Mismatch"}
          </span>
        </div>
        <div className="flex flex-col sm:flex-row sm:justify-between sm:items-center py-0.5 gap-0.5 sm:gap-2 min-w-0">
          <span className="text-slate-400 break-words">Replay Schema Version ({verification.current_replay_schema_version}):</span>
          <span className={verification.replay_schema_version_matches ? "text-emerald-400 font-medium shrink-0" : "text-amber-400 font-medium shrink-0"}>
            {verification.replay_schema_version_matches ? "Matches" : "Mismatch"}
          </span>
        </div>
      </div>

      {verification.reasons && verification.reasons.length > 0 && (
        <div className="mt-3 pt-2 border-t border-slate-800/80 min-w-0">
          <button
            type="button"
            onClick={() => setExpanded(!expanded)}
            aria-expanded={expanded}
            aria-controls={`verification-details-${runId}`}
            className="flex items-center justify-between w-full text-xs font-medium text-slate-300 hover:text-white transition-colors py-1 focus:outline-none focus:ring-1 focus:ring-sky-500 rounded"
          >
            <span>Verification Notes ({verification.reasons.length})</span>
            {expanded ? (
              <ChevronUp className="w-3.5 h-3.5 text-slate-400 shrink-0" />
            ) : (
              <ChevronDown className="w-3.5 h-3.5 text-slate-400 shrink-0" />
            )}
          </button>

          {expanded && (
            <ul
              id={`verification-details-${runId}`}
              className="mt-2 space-y-1 text-xs text-slate-400 bg-slate-950/60 p-2.5 rounded-lg border border-slate-800 font-mono min-w-0"
            >
              {verification.reasons.map((reason, idx) => (
                <li key={idx} className="flex items-start space-x-1.5 leading-tight min-w-0">
                  <span className="text-slate-500 shrink-0">•</span>
                  <span className="break-words min-w-0">{reason}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
};
