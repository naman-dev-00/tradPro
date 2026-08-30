import React from "react";
import { DatasetQualityReport } from "@/lib/api";
import { CheckCircle2, AlertTriangle, XCircle, Rows, Calendar, Layers } from "lucide-react";

interface QualitySummaryCardsProps {
  report: DatasetQualityReport;
}

export function QualitySummaryCards({ report }: QualitySummaryCardsProps) {
  const { status, summary, total_issue_count } = report;

  const getStatusConfig = (st: string) => {
    switch (st) {
      case "PASS":
        return {
          icon: <CheckCircle2 className="w-6 h-6 text-emerald-400" aria-hidden="true" />,
          label: "PASS",
          subtitle: "All integrity & schema checks passed cleanly",
          badgeClass: "bg-emerald-950/70 text-emerald-300 border-emerald-500/50",
          cardClass: "border-emerald-500/30 bg-emerald-950/10",
        };
      case "WARN":
        return {
          icon: <AlertTriangle className="w-6 h-6 text-amber-400" aria-hidden="true" />,
          label: "WARN",
          subtitle: "Warnings detected (incomplete candle, warmup limits)",
          badgeClass: "bg-amber-950/70 text-amber-300 border-amber-500/50",
          cardClass: "border-amber-500/30 bg-amber-950/10",
        };
      case "FAIL":
        return {
          icon: <XCircle className="w-6 h-6 text-rose-400" aria-hidden="true" />,
          label: "FAIL",
          subtitle: "Critical schema, timestamp, or checksum errors detected",
          badgeClass: "bg-rose-950/70 text-rose-300 border-rose-500/50",
          cardClass: "border-rose-500/30 bg-rose-950/10",
        };
      default:
        return {
          icon: null,
          label: st,
          subtitle: "",
          badgeClass: "bg-slate-800 text-slate-300 border-slate-700",
          cardClass: "border-slate-800 bg-slate-900/50",
        };
    }
  };

  const statusConfig = getStatusConfig(status);

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
      {/* Overall Quality Status Card */}
      <div className={`rounded-xl border p-4 flex flex-col justify-between ${statusConfig.cardClass}`}>
        <div className="flex items-center justify-between">
          <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Quality Status</span>
          <span className={`px-2.5 py-0.5 rounded text-xs font-bold border ${statusConfig.badgeClass}`}>
            {statusConfig.label}
          </span>
        </div>
        <div className="flex items-center space-x-3 my-2">
          {statusConfig.icon}
          <div>
            <div className="text-xl font-bold text-slate-100">{statusConfig.label}</div>
            <div className="text-[11px] text-slate-400">{statusConfig.subtitle}</div>
          </div>
        </div>
        <div className="text-[10px] font-mono text-slate-500 pt-2 border-t border-slate-800/60">
          Rules Version: {report.audit_rules_version}
        </div>
      </div>

      {/* Row Accounting Card */}
      <div className="bg-slate-900/70 border border-slate-800 rounded-xl p-4 flex flex-col justify-between">
        <div className="flex items-center justify-between">
          <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Row Accounting</span>
          <Rows className="w-4 h-4 text-sky-400" aria-hidden="true" />
        </div>
        <div className="my-2">
          <div className="text-2xl font-bold text-slate-100">{summary.total_rows}</div>
          <div className="text-xs text-slate-400">Total CSV Data Rows</div>
        </div>
        <div className="flex items-center justify-between text-[11px] font-mono pt-2 border-t border-slate-800/60">
          <span className="text-emerald-400">{summary.completed_rows} completed</span>
          <span className={summary.incomplete_rows > 0 ? "text-amber-400" : "text-slate-500"}>
            {summary.incomplete_rows} incomplete
          </span>
          <span className={summary.malformed_rows > 0 ? "text-rose-400" : "text-slate-500"}>
            {summary.malformed_rows} malformed
          </span>
        </div>
      </div>

      {/* Timestamp Continuity Card */}
      <div className="bg-slate-900/70 border border-slate-800 rounded-xl p-4 flex flex-col justify-between">
        <div className="flex items-center justify-between">
          <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Continuity & Order</span>
          <Layers className="w-4 h-4 text-purple-400" aria-hidden="true" />
        </div>
        <div className="my-2">
          <div className="text-2xl font-bold text-slate-100">
            {summary.duplicate_timestamp_count + summary.missing_interval_count}
          </div>
          <div className="text-xs text-slate-400">Sequence Anomalies</div>
        </div>
        <div className="flex items-center justify-between text-[11px] font-mono pt-2 border-t border-slate-800/60">
          <span className={summary.duplicate_timestamp_count > 0 ? "text-rose-400" : "text-slate-400"}>
            {summary.duplicate_timestamp_count} duplicates
          </span>
          <span className={summary.missing_interval_count > 0 ? "text-amber-400" : "text-slate-400"}>
            {summary.missing_interval_count} missing intervals
          </span>
        </div>
      </div>

      {/* Issues & Timestamp Bounds Card */}
      <div className="bg-slate-900/70 border border-slate-800 rounded-xl p-4 flex flex-col justify-between">
        <div className="flex items-center justify-between">
          <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Audit Findings</span>
          <Calendar className="w-4 h-4 text-indigo-400" aria-hidden="true" />
        </div>
        <div className="my-2">
          <div className="text-2xl font-bold text-slate-100">{total_issue_count}</div>
          <div className="text-xs text-slate-400">Total Quality Issues Detected</div>
        </div>
        <div className="text-[10px] font-mono text-slate-400 pt-2 border-t border-slate-800/60 truncate">
          {summary.first_timestamp ? new Date(summary.first_timestamp).toISOString().slice(0, 16) : "N/A"} →{" "}
          {summary.last_timestamp ? new Date(summary.last_timestamp).toISOString().slice(0, 16) : "N/A"}
        </div>
      </div>
    </div>
  );
}
