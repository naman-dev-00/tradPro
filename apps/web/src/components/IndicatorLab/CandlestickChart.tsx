"use client";

import React, { useEffect, useRef } from "react";
import {
  createChart,
  ColorType,
  IChartApi,
  Time,
  CandlestickSeries,
  LineSeries,
  HistogramSeries,
} from "lightweight-charts";
import { CandleData, IndicatorResultOutput } from "../../lib/api";

export interface ComparisonConfig {
  id: string;
  indicator: string;
  params: Record<string, any>;
  results: IndicatorResultOutput[];
  color: string;
}

interface CandlestickChartProps {
  candles: CandleData[];
  primaryIndicator: {
    indicator: string;
    params: Record<string, any>;
    results: IndicatorResultOutput[];
  } | null;
  comparisons?: ComparisonConfig[];
}

const COLOR_PALETTE = ["#6366f1", "#10b981", "#f59e0b", "#ec4899", "#8b5cf6"];

export function CandlestickChart({
  candles,
  primaryIndicator,
  comparisons = [],
}: CandlestickChartProps) {
  const mainChartContainerRef = useRef<HTMLDivElement>(null);
  const rsiChartContainerRef = useRef<HTMLDivElement>(null);
  const macdChartContainerRef = useRef<HTMLDivElement>(null);
  const volChartContainerRef = useRef<HTMLDivElement>(null);

  const mainChartRef = useRef<IChartApi | null>(null);
  const rsiChartRef = useRef<IChartApi | null>(null);
  const macdChartRef = useRef<IChartApi | null>(null);
  const volChartRef = useRef<IChartApi | null>(null);

  // Combine primary + comparisons into a single list
  const activeSeriesConfigs: ComparisonConfig[] = [];
  if (primaryIndicator && primaryIndicator.results.length > 0) {
    activeSeriesConfigs.push({
      id: "primary",
      indicator: primaryIndicator.indicator,
      params: primaryIndicator.params,
      results: primaryIndicator.results,
      color: "#6366f1",
    });
  }
  comparisons.forEach((comp, idx) => {
    activeSeriesConfigs.push({
      ...comp,
      color: comp.color || COLOR_PALETTE[(idx + 1) % COLOR_PALETTE.length],
    });
  });

  const hasRsi = activeSeriesConfigs.some((c) => c.indicator === "RSI");
  const hasMacd = activeSeriesConfigs.some((c) => c.indicator === "MACD");
  const hasVol = activeSeriesConfigs.some((c) => c.indicator === "VOLUME" || c.indicator === "AVERAGE_VOLUME");

  useEffect(() => {
    if (!mainChartContainerRef.current || candles.length === 0) return;

    // Clean up previous charts
    if (mainChartRef.current) mainChartRef.current.remove();
    if (rsiChartRef.current) rsiChartRef.current.remove();
    if (macdChartRef.current) macdChartRef.current.remove();
    if (volChartRef.current) volChartRef.current.remove();

    const chartOptions = {
      layout: {
        background: { type: ColorType.Solid, color: "#020617" },
        textColor: "#94a3b8",
      },
      grid: {
        vertLines: { color: "#0f172a" },
        horzLines: { color: "#0f172a" },
      },
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
        borderColor: "#1e293b",
      },
      crosshair: {
        mode: 1,
      },
    };

    // 1. Create Main Candlestick Chart
    const mainChart = createChart(mainChartContainerRef.current, {
      ...chartOptions,
      height: 320,
    });
    mainChartRef.current = mainChart;

    const candleSeries = mainChart.addSeries(CandlestickSeries, {
      upColor: "#10b981",
      downColor: "#ef4444",
      borderUpColor: "#10b981",
      borderDownColor: "#ef4444",
      wickUpColor: "#10b981",
      wickDownColor: "#ef4444",
    });

    const formattedCandles = candles.map((c) => ({
      time: (new Date(c.timestamp).getTime() / 1000) as Time,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    }));
    candleSeries.setData(formattedCandles);

    // Render Price Overlays (PRICE, SMA, EMA, PIVOT) on Main Chart
    activeSeriesConfigs.forEach((cfg) => {
      const { indicator, results, color } = cfg;

      if (["PRICE", "SMA", "EMA"].includes(indicator)) {
        const lineSeries = mainChart.addSeries(LineSeries, {
          color,
          lineWidth: 2,
          title: `${indicator} ${cfg.params.period ? `(${cfg.params.period})` : ""}`,
        });
        const dataPoints = results
          .filter((r) => r.available && r.value !== null && typeof r.value === "number")
          .map((r) => ({
            time: (new Date(r.timestamp).getTime() / 1000) as Time,
            value: r.value as number,
          }));
        lineSeries.setData(dataPoints);
      } else if (indicator === "PIVOT") {
        // PIVOT renders Pivot, S1, R1 lines
        const pSeries = mainChart.addSeries(LineSeries, { color: "#8b5cf6", lineWidth: 1, title: "Pivot" });
        const s1Series = mainChart.addSeries(LineSeries, { color: "#10b981", lineWidth: 1, title: "S1" });
        const r1Series = mainChart.addSeries(LineSeries, { color: "#f43f5e", lineWidth: 1, title: "R1" });

        const pData: any[] = [];
        const s1Data: any[] = [];
        const r1Data: any[] = [];

        results.forEach((r) => {
          if (r.available && r.value && typeof r.value === "object") {
            const t = (new Date(r.timestamp).getTime() / 1000) as Time;
            if (r.value.pivot != null) pData.push({ time: t, value: r.value.pivot });
            if (r.value.s1 != null) s1Data.push({ time: t, value: r.value.s1 });
            if (r.value.r1 != null) r1Data.push({ time: t, value: r.value.r1 });
          }
        });

        pSeries.setData(pData);
        s1Series.setData(s1Data);
        r1Series.setData(r1Data);
      }
    });

    // 2. Create RSI Chart Panel
    if (hasRsi && rsiChartContainerRef.current) {
      const rsiChart = createChart(rsiChartContainerRef.current, {
        ...chartOptions,
        height: 160,
      });
      rsiChartRef.current = rsiChart;

      activeSeriesConfigs
        .filter((c) => c.indicator === "RSI")
        .forEach((cfg) => {
          const rsiSeries = rsiChart.addSeries(LineSeries, {
            color: cfg.color,
            lineWidth: 2,
            title: `RSI (${cfg.params.period || 14})`,
          });
          const points = cfg.results
            .filter((r) => r.available && r.value !== null && typeof r.value === "number")
            .map((r) => ({
              time: (new Date(r.timestamp).getTime() / 1000) as Time,
              value: r.value as number,
            }));
          rsiSeries.setData(points);
        });
    }

    // 3. Create MACD Chart Panel
    if (hasMacd && macdChartContainerRef.current) {
      const macdChart = createChart(macdChartContainerRef.current, {
        ...chartOptions,
        height: 180,
      });
      macdChartRef.current = macdChart;

      activeSeriesConfigs
        .filter((c) => c.indicator === "MACD")
        .forEach((cfg) => {
          const macdLine = macdChart.addSeries(LineSeries, { color: cfg.color, lineWidth: 1, title: "MACD" });
          const signalLine = macdChart.addSeries(LineSeries, { color: "#f59e0b", lineWidth: 1, title: "Signal" });
          const histSeries = macdChart.addSeries(HistogramSeries, { title: "Histogram" });

          const macdData: any[] = [];
          const sigData: any[] = [];
          const histData: any[] = [];

          cfg.results.forEach((r) => {
            if (r.value && typeof r.value === "object") {
              const t = (new Date(r.timestamp).getTime() / 1000) as Time;
              if (r.value.macd != null) macdData.push({ time: t, value: r.value.macd });
              if (r.value.signal != null) sigData.push({ time: t, value: r.value.signal });
              if (r.value.histogram != null) {
                histData.push({
                  time: t,
                  value: r.value.histogram,
                  color: r.value.histogram >= 0 ? "#10b981" : "#ef4444",
                });
              }
            }
          });

          macdLine.setData(macdData);
          signalLine.setData(sigData);
          histSeries.setData(histData);
        });
    }

    // 4. Create Volume Chart Panel
    if (hasVol && volChartContainerRef.current) {
      const volChart = createChart(volChartContainerRef.current, {
        ...chartOptions,
        height: 160,
      });
      volChartRef.current = volChart;

      const volHist = volChart.addSeries(HistogramSeries, {
        color: "#3b82f6",
        title: "Volume",
      });
      const volData = candles.map((c) => ({
        time: (new Date(c.timestamp).getTime() / 1000) as Time,
        value: c.volume,
      }));
      volHist.setData(volData);

      activeSeriesConfigs
        .filter((c) => c.indicator === "AVERAGE_VOLUME")
        .forEach((cfg) => {
          const avgVolLine = volChart.addSeries(LineSeries, {
            color: cfg.color,
            lineWidth: 2,
            title: `Avg Volume (${cfg.params.period || 20})`,
          });
          const avgVolData = cfg.results
            .filter((r) => r.available && r.value !== null && typeof r.value === "number")
            .map((r) => ({
              time: (new Date(r.timestamp).getTime() / 1000) as Time,
              value: r.value as number,
            }));
          avgVolLine.setData(avgVolData);
        });
    }

    // Handle Window Resize
    const handleResize = () => {
      if (mainChartContainerRef.current && mainChartRef.current) {
        mainChartRef.current.applyOptions({ width: mainChartContainerRef.current.clientWidth });
      }
      if (rsiChartContainerRef.current && rsiChartRef.current) {
        rsiChartRef.current.applyOptions({ width: rsiChartContainerRef.current.clientWidth });
      }
      if (macdChartContainerRef.current && macdChartRef.current) {
        macdChartRef.current.applyOptions({ width: macdChartContainerRef.current.clientWidth });
      }
      if (volChartContainerRef.current && volChartRef.current) {
        volChartRef.current.applyOptions({ width: volChartContainerRef.current.clientWidth });
      }
    };

    window.addEventListener("resize", handleResize);

    return () => {
      window.removeEventListener("resize", handleResize);
      if (mainChartRef.current) mainChartRef.current.remove();
      if (rsiChartRef.current) rsiChartRef.current.remove();
      if (macdChartRef.current) macdChartRef.current.remove();
      if (volChartRef.current) volChartRef.current.remove();
    };
  }, [candles, primaryIndicator, comparisons, hasRsi, hasMacd, hasVol]);

  return (
    <div className="bg-slate-950 border border-slate-900 rounded-xl p-4 space-y-4 font-sans">
      <div className="flex justify-between items-center border-b border-slate-900 pb-2">
        <h3 className="font-bold text-xs text-indigo-400 uppercase tracking-wider">Visual Inspection Charts</h3>
        <span className="text-[10px] text-slate-500 font-mono">TradingView Lightweight Charts • UTC</span>
      </div>

      {/* Main Candlestick & Overlays Container */}
      <div className="relative rounded-lg overflow-hidden border border-slate-900">
        <div className="absolute top-2 left-2 z-10 text-[10px] bg-slate-900/80 px-2 py-0.5 rounded font-mono text-slate-300 backdrop-blur">
          OHLC Price Chart & Overlays
        </div>
        <div ref={mainChartContainerRef} className="w-full" />
      </div>

      {/* RSI Panel */}
      {hasRsi && (
        <div className="relative rounded-lg overflow-hidden border border-slate-900">
          <div className="absolute top-2 left-2 z-10 text-[10px] bg-slate-900/80 px-2 py-0.5 rounded font-mono text-purple-300 backdrop-blur">
            RSI Panel (30 / 70 Thresholds)
          </div>
          <div ref={rsiChartContainerRef} className="w-full" />
        </div>
      )}

      {/* MACD Panel */}
      {hasMacd && (
        <div className="relative rounded-lg overflow-hidden border border-slate-900">
          <div className="absolute top-2 left-2 z-10 text-[10px] bg-slate-900/80 px-2 py-0.5 rounded font-mono text-amber-300 backdrop-blur">
            MACD Panel (MACD Line, Signal & Histogram)
          </div>
          <div ref={macdChartContainerRef} className="w-full" />
        </div>
      )}

      {/* Volume Panel */}
      {hasVol && (
        <div className="relative rounded-lg overflow-hidden border border-slate-900">
          <div className="absolute top-2 left-2 z-10 text-[10px] bg-slate-900/80 px-2 py-0.5 rounded font-mono text-blue-300 backdrop-blur">
            Volume & Average Volume Panel
          </div>
          <div ref={volChartContainerRef} className="w-full" />
        </div>
      )}
    </div>
  );
}
