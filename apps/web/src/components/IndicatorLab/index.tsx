"use client";

import React, { useState, useEffect } from "react";
import {
  DatasetMetadata,
  DatasetDetailResponse,
  SupportedIndicatorMetadata,
  IndicatorResultOutput,
  getSyntheticDatasets,
  getSyntheticDatasetById,
  getSupportedIndicators,
  calculateIndicator,
} from "../../lib/api";
import { EducationalNotice } from "./EducationalNotice";
import { DatasetSelector } from "./DatasetSelector";
import { IndicatorSelector } from "./IndicatorSelector";
import { CandlestickChart, ComparisonConfig } from "./CandlestickChart";
import { ResultsTable } from "./ResultsTable";
import { ComparisonPanel } from "./ComparisonPanel";
import { AlertCircle } from "lucide-react";

const COLOR_PALETTE = ["#6366f1", "#10b981", "#f59e0b", "#ec4899", "#8b5cf6"];

export function IndicatorLab() {
  const [datasets, setDatasets] = useState<DatasetMetadata[]>([]);
  const [selectedDatasetId, setSelectedDatasetId] = useState<string>("");
  const [datasetDetail, setDatasetDetail] = useState<DatasetDetailResponse | null>(null);

  const [supportedIndicators, setSupportedIndicators] = useState<SupportedIndicatorMetadata[]>([]);
  const [selectedIndicatorName, setSelectedIndicatorName] = useState<string>("SMA");
  const [params, setParams] = useState<Record<string, any>>({ period: 20 });
  const [paramErrors, setParamErrors] = useState<Record<string, string>>({});

  const [primaryCalculation, setPrimaryCalculation] = useState<{
    indicator: string;
    params: Record<string, any>;
    results: IndicatorResultOutput[];
  } | null>(null);

  const [comparisons, setComparisons] = useState<ComparisonConfig[]>([]);

  const [loadingDataset, setLoadingDataset] = useState<boolean>(true);
  const [loadingCalculation, setLoadingCalculation] = useState<boolean>(false);
  const [apiError, setApiError] = useState<string | null>(null);

  // 2. Load Dataset Detail
  const loadDataset = async (id: string) => {
    setLoadingDataset(true);
    setApiError(null);
    try {
      const data = await getSyntheticDatasetById(id);
      setDatasetDetail(data);
      setLoadingDataset(false);
      // Reset calculations on dataset change
      setPrimaryCalculation(null);
      setComparisons([]);
    } catch (err: any) {
      setApiError(`Error loading dataset: ${err.message}`);
      setLoadingDataset(false);
    }
  };

  // 1. Initial Load: Datasets & Supported Indicators
  useEffect(() => {
    Promise.all([getSyntheticDatasets(), getSupportedIndicators()])
      .then(([datasetsRes, indicatorsRes]) => {
        setDatasets(datasetsRes.datasets);
        setSupportedIndicators(indicatorsRes.indicators);

        if (datasetsRes.datasets.length > 0) {
          const firstId = datasetsRes.datasets[0].id;
          setSelectedDatasetId(firstId);
          loadDataset(firstId);
        }
      })
      .catch((err) => {
        setApiError(`Failed to initialize Indicator Lab: ${err.message}`);
        setLoadingDataset(false);
      });
  }, []);

  // 3. Handle Dataset Change
  const handleSelectDataset = (id: string) => {
    setSelectedDatasetId(id);
    loadDataset(id);
  };

  // 4. Handle Indicator Change
  const handleSelectIndicator = (name: string) => {
    setSelectedIndicatorName(name);
    const meta = supportedIndicators.find((i) => i.name === name);
    if (meta) {
      const defaultParams: Record<string, any> = {};
      Object.entries(meta.parameters).forEach(([k, v]) => {
        defaultParams[k] = v.default;
      });
      setParams(defaultParams);
      validateParams(name, defaultParams);
    }
  };

  // 5. Handle Parameter Change
  const handleChangeParam = (key: string, val: number) => {
    const updated = { ...params, [key]: val };
    setParams(updated);
    validateParams(selectedIndicatorName, updated);
  };

  // 6. Pre-submission Parameter Validation Bounds
  const validateParams = (indName: string, currentParams: Record<string, any>) => {
    const errors: Record<string, string> = {};

    if (["SMA", "EMA", "RSI", "AVERAGE_VOLUME"].includes(indName)) {
      const period = currentParams.period;
      if (period === undefined || period < 1) {
        errors.period = "Period must be an integer greater than or equal to 1.";
      }
    } else if (indName === "MACD") {
      const fast = currentParams.fast_period;
      const slow = currentParams.slow_period;
      const sig = currentParams.signal_period;

      if (fast === undefined || fast < 1) errors.fast_period = "Fast period must be >= 1.";
      if (slow === undefined || slow < 1) errors.slow_period = "Slow period must be >= 1.";
      if (sig === undefined || sig < 1) errors.signal_period = "Signal period must be >= 1.";
      if (fast && slow && slow <= fast) {
        errors.slow_period = "Slow period must be strictly greater than fast period.";
      }
    }

    setParamErrors(errors);
    return Object.keys(errors).length === 0;
  };

  // 7. Trigger Primary Calculation
  const handleCalculate = async () => {
    if (!datasetDetail) return;
    if (!validateParams(selectedIndicatorName, params)) return;

    setLoadingCalculation(true);
    setApiError(null);

    try {
      const res = await calculateIndicator({
        candles: datasetDetail.candles,
        indicator: selectedIndicatorName,
        params,
      });

      setPrimaryCalculation({
        indicator: res.indicator,
        params: res.params,
        results: res.results,
      });
      setLoadingCalculation(false);
    } catch (err: any) {
      setApiError(err.message || "Failed to calculate indicator.");
      setLoadingCalculation(false);
    }
  };

  // 8. Add to Comparison Mode
  const handleAddComparison = async () => {
    if (!datasetDetail || comparisons.length >= 3) return;
    if (!validateParams(selectedIndicatorName, params)) return;

    setLoadingCalculation(true);
    setApiError(null);

    try {
      const res = await calculateIndicator({
        candles: datasetDetail.candles,
        indicator: selectedIndicatorName,
        params,
      });

      const nextColor = COLOR_PALETTE[comparisons.length % COLOR_PALETTE.length];
      const newConfig: ComparisonConfig = {
        id: `${res.indicator}-${Date.now()}`,
        indicator: res.indicator,
        params: res.params,
        results: res.results,
        color: nextColor,
      };

      setComparisons((prev) => [...prev, newConfig]);
      setLoadingCalculation(false);
    } catch (err: any) {
      setApiError(err.message || "Failed to add comparison indicator.");
      setLoadingCalculation(false);
    }
  };

  // 9. Remove Comparison
  const handleRemoveComparison = (id: string) => {
    setComparisons((prev) => prev.filter((c) => c.id !== id));
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 font-sans flex flex-col">
      <div className="max-w-7xl mx-auto w-full px-4 sm:px-6 lg:px-8 py-6 space-y-6 flex-1">
        {/* Educational Disclaimer Banner */}
        <EducationalNotice />

        {/* Structured API Error Banner */}
        {apiError && (
          <div className="bg-red-950/30 border border-red-900/60 rounded-xl p-4 flex items-start gap-3 text-xs text-red-300">
            <AlertCircle className="text-red-400 shrink-0 mt-0.5" size={18} />
            <div>
              <h4 className="font-bold text-red-200">Calculation / API Error</h4>
              <p className="mt-0.5 leading-relaxed">{apiError}</p>
            </div>
          </div>
        )}

        {/* Main Controls Grid */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
          {/* Left Column: Dataset Selector */}
          <div>
            <DatasetSelector
              datasets={datasets}
              selectedDatasetId={selectedDatasetId}
              datasetDetail={datasetDetail}
              loading={loadingDataset}
              onSelectDataset={handleSelectDataset}
            />
          </div>

          {/* Center Column: Indicator Configurator */}
          <div>
            <IndicatorSelector
              supportedIndicators={supportedIndicators}
              selectedIndicatorName={selectedIndicatorName}
              params={params}
              paramErrors={paramErrors}
              loading={loadingCalculation || loadingDataset}
              onSelectIndicator={handleSelectIndicator}
              onChangeParam={handleChangeParam}
              onCalculate={handleCalculate}
              onAddComparison={handleAddComparison}
              comparisonCount={comparisons.length}
            />
          </div>

          {/* Right Column: Educational Comparison Panel */}
          <div>
            <ComparisonPanel
              comparisons={comparisons}
              onRemoveComparison={handleRemoveComparison}
            />
          </div>
        </div>

        {/* Main Visualization Section */}
        {datasetDetail && (
          <div className="space-y-6 pt-2">
            <CandlestickChart
              candles={datasetDetail.candles}
              primaryIndicator={primaryCalculation}
              comparisons={comparisons}
            />

            {/* Results Table */}
            {primaryCalculation && (
              <ResultsTable
                results={primaryCalculation.results}
                indicatorName={primaryCalculation.indicator}
                params={primaryCalculation.params}
              />
            )}
          </div>
        )}
      </div>
    </div>
  );
}
