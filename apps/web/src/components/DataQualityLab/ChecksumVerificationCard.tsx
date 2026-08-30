import React from "react";
import { DatasetQualitySummary } from "@/lib/api";
import { Hash, CheckCircle2, XCircle, HelpCircle } from "lucide-react";

interface ChecksumVerificationCardProps {
  summary: DatasetQualitySummary;
}

export function ChecksumVerificationCard({ summary }: ChecksumVerificationCardProps) {
  const { calculated_checksum, manifest_checksum, checksum_matches } = summary;

  return (
    <div className="bg-slate-900/70 border border-slate-800 rounded-xl p-5 mb-6">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center space-x-2">
          <Hash className="w-4 h-4 text-sky-400" aria-hidden="true" />
          <h2 className="text-sm font-semibold text-slate-200 uppercase tracking-wider">
            SHA-256 Checksum Verification
          </h2>
        </div>

        {checksum_matches === true && (
          <span className="inline-flex items-center space-x-1.5 px-2.5 py-1 rounded bg-emerald-950/70 border border-emerald-500/50 text-emerald-300 text-xs font-bold">
            <CheckCircle2 className="w-3.5 h-3.5" aria-hidden="true" />
            <span>EXACT CHECKSUM MATCH</span>
          </span>
        )}

        {checksum_matches === false && (
          <span className="inline-flex items-center space-x-1.5 px-2.5 py-1 rounded bg-rose-950/70 border border-rose-500/50 text-rose-300 text-xs font-bold">
            <XCircle className="w-3.5 h-3.5" aria-hidden="true" />
            <span>CHECKSUM MISMATCH</span>
          </span>
        )}

        {checksum_matches === null && (
          <span className="inline-flex items-center space-x-1.5 px-2.5 py-1 rounded bg-slate-800 border border-slate-700 text-slate-400 text-xs font-bold">
            <HelpCircle className="w-3.5 h-3.5" aria-hidden="true" />
            <span>NO MANIFEST CHECKSUM</span>
          </span>
        )}
      </div>

      <div className="space-y-2 text-xs font-mono">
        <div className="bg-slate-800/60 p-2.5 rounded border border-slate-800 flex flex-col md:flex-row md:items-center justify-between gap-1">
          <span className="text-slate-400 font-sans text-xs">Calculated Raw Byte Hash:</span>
          <span className="text-slate-200 text-[11px] break-all">{calculated_checksum || "N/A"}</span>
        </div>

        <div className="bg-slate-800/60 p-2.5 rounded border border-slate-800 flex flex-col md:flex-row md:items-center justify-between gap-1">
          <span className="text-slate-400 font-sans text-xs">Manifest Expected Hash:</span>
          <span className="text-slate-200 text-[11px] break-all">{manifest_checksum || "N/A"}</span>
        </div>
      </div>
    </div>
  );
}
