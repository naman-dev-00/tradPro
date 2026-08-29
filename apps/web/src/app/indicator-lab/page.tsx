"use client";

import React from "react";
import Link from "next/link";
import { IndicatorLab } from "../../components/IndicatorLab";
import { FlaskConical } from "lucide-react";

export default function IndicatorLabPage() {
  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col font-sans">
      {/* Header Navbar */}
      <header className="border-b border-slate-900 bg-slate-950/80 backdrop-blur-md sticky top-0 z-50">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="bg-indigo-600 p-2 rounded-lg text-white font-extrabold text-lg tracking-wider">
              TP
            </div>
            <div>
              <span className="font-extrabold text-white text-base tracking-wide">TradePro</span>
              <span className="text-[10px] bg-slate-900 border border-slate-800 text-slate-400 px-1.5 py-0.5 rounded font-mono ml-2">Indicator Lab</span>
            </div>
          </div>

          <nav className="flex items-center gap-3 text-xs font-semibold text-slate-300">
            <Link href="/" className="hover:text-white transition px-3 py-1.5">
              Strategies
            </Link>
            <Link href="/builder" className="hover:text-white transition px-3 py-1.5">
              Builder
            </Link>
            <Link
              href="/indicator-lab"
              className="text-white bg-indigo-600/20 border border-indigo-500/30 px-3 py-1.5 rounded-lg flex items-center gap-1.5 font-bold"
            >
              <FlaskConical size={14} className="text-indigo-400" />
              Indicator Lab
            </Link>
            <Link href="/rule-lab" className="hover:text-white transition px-3 py-1.5">
              Rule Lab
            </Link>
            <Link href="/multi-series-lab" className="hover:text-white transition px-3 py-1.5">
              Multi-Series Lab
            </Link>
          </nav>
        </div>
      </header>

      {/* Lab Interface */}
      <main className="flex-1">
        <IndicatorLab />
      </main>
    </div>
  );
}
