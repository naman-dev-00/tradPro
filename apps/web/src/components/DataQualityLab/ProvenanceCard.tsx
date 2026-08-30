import React from "react";
import { DatasetProvenance } from "@/lib/api";
import { ShieldCheck, Lock, Tag, Clock, FileText } from "lucide-react";

interface ProvenanceCardProps {
  provenance: DatasetProvenance;
}

export function ProvenanceCard({ provenance }: ProvenanceCardProps) {
  return (
    <div className="bg-slate-900/70 border border-slate-800 rounded-xl p-5 mb-6">
      <div className="flex items-center space-x-2 mb-4">
        <ShieldCheck className="w-4 h-4 text-emerald-400" aria-hidden="true" />
        <h2 className="text-sm font-semibold text-slate-200 uppercase tracking-wider">
          Dataset Provenance & Manifest Integrity
        </h2>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 text-xs">
        <div className="bg-slate-800/40 p-3 rounded-lg border border-slate-800">
          <div className="text-slate-400 flex items-center space-x-1.5 mb-1">
            <Lock className="w-3.5 h-3.5 text-sky-400" aria-hidden="true" />
            <span>Source Type & Mutability</span>
          </div>
          <div className="font-mono text-slate-200 font-medium">
            {provenance.source_type}
          </div>
          <div className="text-[10px] text-emerald-400 mt-1 flex items-center space-x-1">
            <span>✓ Immutable Synthetic Fixture</span>
          </div>
        </div>

        <div className="bg-slate-800/40 p-3 rounded-lg border border-slate-800">
          <div className="text-slate-400 flex items-center space-x-1.5 mb-1">
            <Tag className="w-3.5 h-3.5 text-purple-400" aria-hidden="true" />
            <span>Category & Instrument</span>
          </div>
          <div className="font-mono text-slate-200 font-medium">
            {provenance.category} / {provenance.instrument_id}
          </div>
          <div className="text-[10px] text-slate-400 mt-1">
            Timeframe: <span className="text-sky-300 font-mono">{provenance.timeframe}</span>
          </div>
        </div>

        <div className="bg-slate-800/40 p-3 rounded-lg border border-slate-800">
          <div className="text-slate-400 flex items-center space-x-1.5 mb-1">
            <Clock className="w-3.5 h-3.5 text-amber-400" aria-hidden="true" />
            <span>Manifest Version</span>
          </div>
          <div className="font-mono text-slate-200 font-medium">
            v{provenance.manifest_version}
          </div>
          <div className="text-[10px] text-slate-400 mt-1">
            Whitelisted Server Manifest
          </div>
        </div>

        <div className="bg-slate-800/40 p-3 rounded-lg border border-slate-800">
          <div className="text-slate-400 flex items-center space-x-1.5 mb-1">
            <FileText className="w-3.5 h-3.5 text-indigo-400" aria-hidden="true" />
            <span>Dataset Identifier</span>
          </div>
          <div className="font-mono text-slate-200 font-medium truncate" title={provenance.dataset_id}>
            {provenance.dataset_id}
          </div>
          <div className="text-[10px] text-slate-400 mt-1">
            {provenance.display_name}
          </div>
        </div>
      </div>
    </div>
  );
}
