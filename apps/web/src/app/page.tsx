"use client";

import React, { useState, useEffect } from "react";
import Link from "next/link";
import { getStrategies, StrategyResponse } from "../lib/api";
import { Plus, Sliders, PlayCircle, Layers, Settings, FileJson, Clock, AlertTriangle } from "lucide-react";

export default function Dashboard() {
  const [strategies, setStrategies] = useState<StrategyResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getStrategies()
      .then((data) => {
        setStrategies(data);
        setLoading(false);
      })
      .catch((err) => {
        setError("Could not connect to TradePro API. Make sure the backend server is running.");
        setLoading(false);
      });
  }, []);

  // Helper to extract indicators list from strategy payload to display as visual tags
  const getIndicatorsList = (payload: any): string[] => {
    const list = new Set<string>();
    const scanNode = (node: any) => {
      if (!node) return;
      if (node.type === "CONDITION") {
        if (node.lhs?.indicator) list.add(node.lhs.indicator);
        if (node.rhs?.type === "INDICATOR" && node.rhs.indicator?.indicator) {
          list.add(node.rhs.indicator.indicator);
        }
      } else if (node.conditions) {
        node.conditions.forEach(scanNode);
      }
    };
    scanNode(payload.global_conditions);
    scanNode(payload.candidate_conditions);
    return Array.from(list);
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col font-sans">
      {/* Navbar Header */}
      <header className="border-b border-slate-900 bg-slate-950/80 backdrop-blur-md sticky top-0 z-50">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="bg-indigo-600 p-2 rounded-lg text-white font-extrabold text-lg tracking-wider">
              TP
            </div>
            <div>
              <span className="font-extrabold text-white text-base tracking-wide">TradePro</span>
              <span className="text-[10px] bg-slate-900 border border-slate-800 text-slate-400 px-1.5 py-0.5 rounded font-mono ml-2">v1.0.0</span>
            </div>
          </div>
          <nav className="flex items-center gap-4 text-xs font-semibold text-slate-300">
            <Link href="/" className="text-white bg-slate-900 border border-slate-800 px-3 py-1.5 rounded-lg">Strategies</Link>
            <Link href="/builder" className="hover:text-white transition px-3 py-1.5">Builder</Link>
            <Link href="/indicator-lab" className="hover:text-indigo-300 text-indigo-400 font-bold transition px-3 py-1.5">Indicator Lab</Link>
            <Link href="/rule-lab" className="hover:text-purple-300 text-purple-400 font-bold transition px-3 py-1.5">Rule Lab</Link>
          </nav>
        </div>
      </header>

      {/* Main Container */}
      <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8 flex-1 w-full">
        {/* Banner Section */}
        <div className="flex justify-between items-center mb-8 border-b border-slate-900 pb-5">
          <div>
            <h1 className="text-2xl font-extrabold tracking-tight text-white">Options Strategy Blueprint Registry</h1>
            <p className="text-sm text-slate-400 mt-1">Manage, construct, and validate strategy definitions for paper-trading simulations.</p>
          </div>
          <Link
            href="/builder"
            className="bg-indigo-600 hover:bg-indigo-500 text-white font-semibold text-xs px-4 py-2.5 rounded-lg flex items-center gap-2 shadow transition"
          >
            <Plus size={14} />
            Create Strategy
          </Link>
        </div>

        {/* Backend Unreachable Error */}
        {error && (
          <div className="bg-amber-950/20 border border-amber-900/50 rounded-xl p-4 mb-8 flex items-start gap-3">
            <AlertTriangle className="text-amber-500 shrink-0 mt-0.5" size={20} />
            <div>
              <h4 className="font-bold text-sm text-amber-300">Connection Warning</h4>
              <p className="text-xs text-amber-400/90 mt-1 leading-relaxed">{error}</p>
            </div>
          </div>
        )}

        {/* Strategy list */}
        {loading ? (
          <div className="flex flex-col items-center justify-center py-20 gap-3">
            <div className="w-8 h-8 border-2 border-indigo-600 border-t-transparent rounded-full animate-spin"></div>
            <span className="text-xs text-slate-400 font-medium">Fetching strategy blueprints...</span>
          </div>
        ) : strategies.length === 0 ? (
          <div className="border border-slate-900 border-dashed rounded-2xl p-12 text-center max-w-lg mx-auto mt-10">
            <div className="bg-slate-900 w-12 h-12 rounded-xl flex items-center justify-center text-slate-400 mx-auto mb-4">
              <Sliders size={20} />
            </div>
            <h3 className="font-bold text-sm text-white">No Strategy Blueprints Found</h3>
            <p className="text-xs text-slate-400 mt-1 max-w-sm mx-auto leading-relaxed">
              Create your first visual strategy blueprint using technical indicators, logic operators, and simulated risk configurations.
            </p>
            <Link
              href="/builder"
              className="mt-5 inline-flex bg-indigo-600 hover:bg-indigo-500 text-white font-semibold text-xs px-4 py-2 rounded-lg transition"
            >
              Get Started
            </Link>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
            {strategies.map((strategy) => {
              const indicators = getIndicatorsList(strategy);
              return (
                <div key={strategy.id} className="bg-slate-950 border border-slate-900 hover:border-slate-800 rounded-2xl p-5 flex flex-col justify-between shadow transition">
                  <div>
                    {/* Timeframe & Mode */}
                    <div className="flex justify-between items-center mb-3">
                      <span className="bg-slate-900 border border-slate-800 text-slate-300 text-[10px] font-mono px-2 py-0.5 rounded font-bold flex items-center gap-1">
                        <Clock size={10} />
                        {strategy.timeframe}
                      </span>
                      <span className="text-[10px] text-slate-500 font-mono font-medium">
                        {strategy.candidate_selection_mode}
                      </span>
                    </div>

                    {/* Title */}
                    <h3 className="font-bold text-base text-white truncate mb-1">
                      {strategy.name}
                    </h3>

                    {/* Description */}
                    <p className="text-xs text-slate-400 line-clamp-2 min-h-[32px] leading-relaxed mb-4">
                      {strategy.description || "No description provided."}
                    </p>

                    {/* Indicators list tags */}
                    {indicators.length > 0 && (
                      <div className="flex flex-wrap gap-1.5 mb-4">
                        {indicators.map((ind) => (
                          <span key={ind} className="bg-indigo-950/40 border border-indigo-900/40 text-indigo-300 text-[9px] font-mono px-1.5 py-0.5 rounded font-bold">
                            {ind}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>

                  {/* Actions */}
                  <div className="border-t border-slate-900/60 pt-3 flex justify-between items-center mt-3">
                    <span className="text-[10px] text-slate-500 flex items-center gap-1 font-medium">
                      <Clock size={10} />
                      {new Date(strategy.updated_at).toLocaleDateString()}
                    </span>
                    <Link
                      href={`/builder?id=${strategy.id}`}
                      className="text-xs text-indigo-400 hover:text-indigo-300 font-semibold flex items-center gap-1 transition"
                    >
                      Edit Blueprint
                      <span className="text-[10px]">→</span>
                    </Link>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </main>
    </div>
  );
}
