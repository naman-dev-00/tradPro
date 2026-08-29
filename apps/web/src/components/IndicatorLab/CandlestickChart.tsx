"use client";

import React, { useEffect, useRef, useMemo } from "react";
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
  color?: string;
}

interface CandlestickChartProps {
  candles: CandleData[];
  primaryIndicator?: {
    indicator: string;
    params: Record<string, any>;
    results: IndicatorResultOutput[];
  } | null;
  comparisons?: ComparisonConfig[];
}

const COLOR_PALETTE = ["#ec4899", "#10b981", "#f59e0b", "#8b5cf6", "#06b6d4"];

export const CandlestickChart: React.FC<CandlestickChartProps> = ({
  candles,
  primaryIndicator,
  comparisons = [],
}) => {
  const mainChartContainerRef = useRef<HTMLDivElement>(null);
  const rsiChartContainerRef = useRef<HTMLDivElement>(null);
  const macdChartContainerRef = useRef<HTMLDivElement>(null);
  const volChartContainerRef = useRef<HTMLDivElement>(null);

  const mainChartRef = useRef<IChartApi | null>(null);
  const rsiChartRef = useRef<IChartApi | null>(null);
  const macdChartRef = useRef<IChartApi | null>(null);
  const volChartRef = useRef<IChartApi | null>(null);

  // Combine primary + comparisons into a single list
  const activeSeriesConfigs = useMemo(() => {
    const configs: ComparisonConfig[] = [];
    if (primaryIndicator && primaryIndicator.results.length > 0) {
      configs.push({
        id: "primary",
        indicator: primaryIndicator.indicator,
        params: primaryIndicator.params,
        results: primaryIndicator.results,
        color: "#6366f1",
      });
    }
    comparisons.forEach((comp, idx) => {
      configs.push({
        ...comp,
        color: comp.color || COLOR_PALETTE[(idx + 1) % COLOR_PALETTE.length],
      });
    });
    return configs;
  }, [primaryIndicator, comparisons]);

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

    // Responsive width helper
    const getContainerWidth = () => mainChartContainerRef.current?.clientWidth || 800;

    // 1. Create Main Price Chart
    const mainChart = createChart(mainChartContainerRef.current, {
      width: getContainerWidth(),
      height: 380,
      layout: {
        background: { type: ColorType.Solid, color: "#090d16" },
        textColor: "#94a3b8",
      },
      grid: {
        vertLines: { color: "#1e293b" },
        horzLines: { color: "#1e293b" },
      },
      timeScale: {
        borderColor: "#334155",
        timeVisible: true,
        secondsVisible: false,
      },
    });
    mainChartRef.current = mainChart;

    // Add Candlestick Series using v5 API
    const candlestickSeries = mainChart.addSeries(CandlestickSeries, {
      upColor: "#10b981",
      downColor: "#ef4444",
      borderVisible: false,
      wickUpColor: "#10b981",
      wickDownColor: "#ef4444",
    });

    const candleData = candles.map((c) => ({
      time: (new Date(c.timestamp).getTime() / 1000) as Time,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    }));
    candlestickSeries.setData(candleData);

    // Overlay Main-Chart Indicators (PRICE, SMA, EMA, PIVOT)
    activeSeriesConfigs.forEach((cfg) => {
      const ind = cfg.indicator;
      const color = cfg.color || "#6366f1";

      if (["PRICE", "SMA", "EMA"].includes(ind)) {
        const lineSeries = mainChart.addSeries(LineSeries, {
          color: color,
          lineWidth: 2,
          title: `${ind}${cfg.params.period ? `(${cfg.params.period})` : ""}`,
        });

        const lineData = cfg.results
          .filter((r) => r.available && r.raw_value !== null)
          .map((r) => ({
            time: (new Date(r.timestamp).getTime() / 1000) as Time,
            value: r.raw_value as number,
          }));

        lineSeries.setData(lineData);
      } else if (ind === "PIVOT") {
        // PIVOT lines (P, S1, R1)
        ["P", "S1", "R1"].forEach((levelKey, lIdx) => {
          const pivotColors = [color, "#ef4444", "#10b981"];
          const lineSeries = mainChart.addSeries(LineSeries, {
            color: pivotColors[lIdx],
            lineWidth: 1,
            title: `PIVOT-${levelKey}`,
          });

          const lineData = cfg.results
            .filter((r) => r.available && r.raw_value && r.raw_value[levelKey] !== undefined)
            .map((r) => ({
              time: (new Date(r.timestamp).getTime() / 1000) as Time,
              value: r.raw_value[levelKey] as number,
            }));

          lineSeries.setData(lineData);
        });
      }
    });

    // 2. Create RSI Sub-Chart (if active)
    if (hasRsi && rsiChartContainerRef.current) {
      const rsiChart = createChart(rsiChartContainerRef.current, {
        width: getContainerWidth(),
        height: 160,
        layout: {
          background: { type: ColorType.Solid, color: "#090d16" },
          textColor: "#94a3b8",
        },
        grid: {
          vertLines: { color: "#1e293b" },
          horzLines: { color: "#1e293b" },
        },
        timeScale: { borderColor: "#334155", timeVisible: true },
      });
      rsiChartRef.current = rsiChart;

      activeSeriesConfigs
        .filter((c) => c.indicator === "RSI")
        .forEach((cfg) => {
          const rsiSeries = rsiChart.addSeries(LineSeries, {
            color: cfg.color || "#ec4899",
            lineWidth: 2,
            title: `RSI(${cfg.params.period || 14})`,
          });

          const rsiData = cfg.results
            .filter((r) => r.available && r.raw_value !== null)
            .map((r) => ({
              time: (new Date(r.timestamp).getTime() / 1000) as Time,
              value: r.raw_value as number,
            }));

          rsiSeries.setData(rsiData);
        });
    }

    // 3. Create MACD Sub-Chart (if active)
    if (hasMacd && macdChartContainerRef.current) {
      const macdChart = createChart(macdChartContainerRef.current, {
        width: getContainerWidth(),
        height: 180,
        layout: {
          background: { type: ColorType.Solid, color: "#090d16" },
          textColor: "#94a3b8",
        },
        grid: {
          vertLines: { color: "#1e293b" },
          horzLines: { color: "#1e293b" },
        },
        timeScale: { borderColor: "#334155", timeVisible: true },
      });
      macdChartRef.current = macdChart;

      activeSeriesConfigs
        .filter((c) => c.indicator === "MACD")
        .forEach((cfg) => {
          // MACD Line
          const macdLine = macdChart.addSeries(LineSeries, {
            color: cfg.color || "#3b82f6",
            lineWidth: 2,
            title: "MACD Line",
          });
          macdLine.setData(
            cfg.results
              .filter((r) => r.available && r.raw_value?.macd_line !== undefined)
              .map((r) => ({
                time: (new Date(r.timestamp).getTime() / 1000) as Time,
                value: r.raw_value.macd_line as number,
              }))
          );

          // Signal Line
          const signalLine = macdChart.addSeries(LineSeries, {
            color: "#f59e0b",
            lineWidth: 1,
            title: "Signal Line",
          });
          signalLine.setData(
            cfg.results
              .filter((r) => r.available && r.raw_value?.signal_line !== undefined)
              .map((r) => ({
                time: (new Date(r.timestamp).getTime() / 1000) as Time,
                value: r.raw_value.signal_line as number,
              }))
          );

          // Histogram
          const histSeries = macdChart.addSeries(HistogramSeries, {
            title: "Histogram",
          });
          histSeries.setData(
            cfg.results
              .filter((r) => r.available && r.raw_value?.histogram !== undefined)
              .map((r) => ({
                time: (new Date(r.timestamp).getTime() / 1000) as Time,
                value: r.raw_value.histogram as number,
                color: (r.raw_value.histogram || 0) >= 0 ? "#10b981" : "#ef4444",
              }))
          );
        });
    }

    // 4. Create Volume / Average Volume Sub-Chart (if active)
    if (hasVol && volChartContainerRef.current) {
      const volChart = createChart(volChartContainerRef.current, {
        width: getContainerWidth(),
        height: 160,
        layout: {
          background: { type: ColorType.Solid, color: "#090d16" },
          textColor: "#94a3b8",
        },
        grid: {
          vertLines: { color: "#1e293b" },
          horzLines: { color: "#1e293b" },
        },
        timeScale: { borderColor: "#334155", timeVisible: true },
      });
      volChartRef.current = volChart;

      activeSeriesConfigs
        .filter((c) => c.indicator === "VOLUME" || c.indicator === "AVERAGE_VOLUME")
        .forEach((cfg) => {
          if (cfg.indicator === "VOLUME") {
            const volSeries = volChart.addSeries(HistogramSeries, {
              color: cfg.color || "#8b5cf6",
              title: "Volume",
            });
            volSeries.setData(
              cfg.results
                .filter((r) => r.available && r.raw_value !== null)
                .map((r) => ({
                  time: (new Date(r.timestamp).getTime() / 1000) as Time,
                  value: r.raw_value as number,
                }))
            );
          } else {
            const avgVolSeries = volChart.addSeries(LineSeries, {
              color: cfg.color || "#06b6d4",
              lineWidth: 2,
              title: `Avg Volume(${cfg.params.period || 20})`,
            });
            avgVolSeries.setData(
              cfg.results
                .filter((r) => r.available && r.raw_value !== null)
                .map((r) => ({
                  time: (new Date(r.timestamp).getTime() / 1000) as Time,
                  value: r.raw_value as number,
                }))
            );
          }
        });
    }

    // Auto-fit time scale for all active charts
    const charts = [
      mainChartRef.current,
      rsiChartRef.current,
      macdChartRef.current,
      volChartRef.current,
    ].filter(Boolean) as IChartApi[];

    charts.forEach((ch) => ch.timeScale().fitContent());

    // Window Resize Handler
    const handleResize = () => {
      const newWidth = getContainerWidth();
      charts.forEach((ch) => ch.applyOptions({ width: newWidth }));
    };

    window.addEventListener("resize", handleResize);

    return () => {
      window.removeEventListener("resize", handleResize);
      if (mainChartRef.current) mainChartRef.current.remove();
      if (rsiChartRef.current) rsiChartRef.current.remove();
      if (macdChartRef.current) macdChartRef.current.remove();
      if (volChartRef.current) volChartRef.current.remove();
    };
  }, [candles, activeSeriesConfigs, hasRsi, hasMacd, hasVol]);

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
