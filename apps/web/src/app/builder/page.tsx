"use client";

import React, { useState, useEffect, Suspense } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { getStrategyById } from "../../lib/api";
import { StrategyBuilder } from "../../components/StrategyBuilder";
import { ArrowLeft } from "lucide-react";
import Link from "next/link";

function BuilderContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const strategyId = searchParams.get("id");

  const [strategy, setStrategy] = useState<any | null>(null);
  const [loading, setLoading] = useState(!!strategyId);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (strategyId) {
      getStrategyById(strategyId)
        .then((data) => {
          // Extract payload dict stored in database Strategy row
          setStrategy(data.payload || data);
          setLoading(false);
        })
        .catch((err) => {
          setError(`Failed to load strategy blueprint: ${err.message}`);
          setLoading(false);
        });
    }
  }, [strategyId]);

  const handleSaveSuccess = () => {
    // Redirect back to dashboard after save succeeds
    router.push("/");
  };

  if (loading) {
    return (
      <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col items-center justify-center gap-3 font-sans">
        <div className="w-8 h-8 border-2 border-indigo-600 border-t-transparent rounded-full animate-spin"></div>
        <span className="text-xs text-slate-400 font-medium">Loading strategy node tree...</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col items-center justify-center gap-4 font-sans p-6 text-center">
        <div className="bg-red-950/20 border border-red-900/50 text-red-400 p-4 rounded-xl max-w-md">
          <h4 className="font-bold text-sm">Error Loading Strategy</h4>
          <p className="text-xs text-red-300 mt-1 leading-relaxed">{error}</p>
        </div>
        <Link href="/" className="text-xs text-slate-400 hover:text-white flex items-center gap-1.5 transition">
          <ArrowLeft size={14} />
          Return to Dashboard
        </Link>
      </div>
    );
  }

  return (
    <div className="h-screen flex flex-col bg-slate-950">
      {/* Sub-Header Navbar */}
      <div className="bg-slate-950 border-b border-slate-900 px-4 py-2 flex items-center justify-between shrink-0">
        <div className="flex items-center gap-3">
          <Link
            href="/"
            className="text-slate-400 hover:text-white hover:bg-slate-900 border border-slate-900 hover:border-slate-800 p-1.5 rounded-lg transition"
            title="Back to Dashboard"
          >
            <ArrowLeft size={16} />
          </Link>
          <span className="text-xs text-slate-500 font-medium">Dashboard / Strategy Builder Workspace</span>
        </div>
        <div className="flex items-center gap-3 text-xs font-semibold">
          <Link href="/indicator-lab" className="text-slate-400 hover:text-white transition">
            Indicator Lab
          </Link>
          <Link href="/rule-lab" className="text-slate-400 hover:text-white transition">
            Rule Lab
          </Link>
          <Link href="/multi-series-lab" className="text-sky-400 hover:text-sky-300 font-bold transition">
            Multi-Series Lab →
          </Link>
        </div>
      </div>

      {/* Strategy Builder Container */}
      <div className="flex-1 min-h-0">
        <StrategyBuilder initialStrategy={strategy} onSaveSuccess={handleSaveSuccess} />
      </div>
    </div>
  );
}

export default function BuilderPage() {
  return (
    <Suspense fallback={
      <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col items-center justify-center gap-3 font-sans">
        <div className="w-8 h-8 border-2 border-indigo-600 border-t-transparent rounded-full animate-spin"></div>
        <span className="text-xs text-slate-400 font-medium font-mono">Initializing workspace...</span>
      </div>
    }>
      <BuilderContent />
    </Suspense>
  );
}
