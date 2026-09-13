import React from "react";
import Link from "next/link";
import { ArrowLeft, AlertCircle } from "lucide-react";
import { AuthHeaderBadge } from "@/components/AuthHeaderBadge";

export default function NotFoundPage() {
  return (
    <div className="min-h-screen flex flex-col bg-slate-950 text-slate-100 font-sans">
      <header className="border-b border-slate-900 bg-slate-950/80 backdrop-blur sticky top-0 z-40 px-4 py-3 flex items-center justify-between">
        <Link href="/" className="flex items-center gap-2 text-sm font-bold text-white tracking-wider uppercase">
          <span className="text-sky-400 font-black">TradePro</span> Labs
        </Link>
        <AuthHeaderBadge />
      </header>

      <div className="flex-1 flex flex-col items-center justify-center p-6 text-center">
        <div className="w-14 h-14 rounded-2xl bg-amber-500/10 border border-amber-500/30 text-amber-400 flex items-center justify-center mb-4 shadow-lg shadow-amber-950/30">
          <AlertCircle size={28} />
        </div>
        <h1 className="text-3xl font-extrabold text-white mb-2">404 - Page Not Found</h1>
        <p className="text-sm text-slate-400 max-w-md mb-6 leading-relaxed">
          The requested educational analytics route or resource does not exist or you do not have permission to view it.
        </p>
        <Link
          href="/"
          className="inline-flex items-center gap-2 px-5 py-2.5 rounded-lg bg-sky-700 hover:bg-sky-600 text-white text-xs font-bold transition shadow-lg shadow-sky-950/50 focus:outline-none focus:ring-2 focus:ring-sky-400 focus:ring-offset-2 focus:ring-offset-slate-950"
        >
          <ArrowLeft size={16} />
          Return to Dashboard
        </Link>
      </div>
    </div>
  );
}
