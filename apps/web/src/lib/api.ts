const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export interface ValidationResponse {
  valid: boolean;
  errors: string[];
}

export interface StrategyResponse {
  id: string;
  name: string;
  description?: string;
  timeframe: string;
  candidate_selection_mode: string;
  payload: any;
  created_at: string;
  updated_at: string;
}

export interface DatasetMetadata {
  id: string;
  name: string;
  description: string;
  instrument_id: string;
  timeframe: string;
}

export interface CandleData {
  timestamp: string;
  instrument_id: string;
  timeframe: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  is_closed: boolean;
}

export interface DatasetDetailResponse {
  id: string;
  name: string;
  description: string;
  instrument_id: string;
  timeframe: string;
  total_candles: number;
  completed_candles: number;
  excluded_incomplete_candles: number;
  candles: CandleData[];
}

export interface SupportedIndicatorParamMetadata {
  type: string;
  default: number;
  minimum?: number;
}

export interface SupportedIndicatorMetadata {
  name: string;
  description: string;
  parameters: Record<string, SupportedIndicatorParamMetadata>;
}

export interface IndicatorResultOutput {
  timestamp: string;
  indicator: string;
  value: number | Record<string, number | null> | null;
  raw_value?: any;
  available: boolean;
  warmup_remaining: number;
}

export interface CalculateIndicatorRequest {
  candles: CandleData[];
  indicator: string;
  params: Record<string, any>;
}

export interface CalculateIndicatorResponse {
  indicator: string;
  params: Record<string, any>;
  results: IndicatorResultOutput[];
}

export async function checkHealth(): Promise<boolean> {
  try {
    const res = await fetch(`${API_BASE_URL}/health`);
    if (!res.ok) return false;
    const data = await res.json();
    return data.status === "healthy";
  } catch {
    return false;
  }
}

export async function validateStrategy(payload: any): Promise<ValidationResponse> {
  const res = await fetch(`${API_BASE_URL}/strategies/validate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const errData = await res.json();
    return {
      valid: false,
      errors: errData.detail?.errors || [errData.detail || "Validation request failed"],
    };
  }
  return res.json();
}

export async function saveStrategy(payload: any, isUpdate = false): Promise<StrategyResponse> {
  const url = isUpdate
    ? `${API_BASE_URL}/strategies/${payload.id}`
    : `${API_BASE_URL}/strategies`;

  const res = await fetch(url, {
    method: isUpdate ? "PUT" : "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  if (!res.ok) {
    const errData = await res.json();
    const errors = errData.detail?.errors || [errData.detail?.message || errData.detail || "Save request failed"];
    throw new Error(errors.join(", "));
  }

  return res.json();
}

export async function getStrategies(): Promise<StrategyResponse[]> {
  const res = await fetch(`${API_BASE_URL}/strategies`);
  if (!res.ok) throw new Error("Failed to fetch strategies list");
  return res.json();
}

export async function getStrategyById(id: string): Promise<StrategyResponse> {
  const res = await fetch(`${API_BASE_URL}/strategies/${id}`);
  if (!res.ok) throw new Error(`Failed to fetch strategy with id ${id}`);
  return res.json();
}

export async function getSyntheticDatasets(): Promise<{ datasets: DatasetMetadata[] }> {
  const res = await fetch(`${API_BASE_URL}/indicators/datasets`);
  if (!res.ok) throw new Error("Failed to fetch synthetic datasets list");
  return res.json();
}

export async function getSyntheticDatasetById(datasetId: string): Promise<DatasetDetailResponse> {
  const res = await fetch(`${API_BASE_URL}/indicators/datasets/${datasetId}`);
  if (!res.ok) {
    const errData = await res.json();
    throw new Error(errData.detail || `Failed to fetch dataset '${datasetId}'`);
  }
  return res.json();
}

export async function getSupportedIndicators(): Promise<{ indicators: SupportedIndicatorMetadata[] }> {
  const res = await fetch(`${API_BASE_URL}/indicators/supported`);
  if (!res.ok) throw new Error("Failed to fetch supported indicators list");
  return res.json();
}

export async function calculateIndicator(req: CalculateIndicatorRequest): Promise<CalculateIndicatorResponse> {
  const res = await fetch(`${API_BASE_URL}/indicators/calculate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });

  if (!res.ok) {
    const errData = await res.json();
    throw new Error(errData.detail || "Failed to calculate indicator");
  }
  return res.json();
}

export type EvaluationStatus = "TRUE" | "FALSE" | "UNAVAILABLE" | "INVALID";

export interface ConditionResult {
  condition_id: string;
  status: EvaluationStatus;
  timestamp?: string;
  left_value?: any;
  operator: string;
  right_value?: any;
  reason?: string;
  indicator_values_used?: Record<string, any>;
  warmup_info?: Record<string, any>;
}

export interface GroupResult {
  group_id: string;
  logical_operator: string;
  status: EvaluationStatus;
  child_results: (GroupResult | ConditionResult)[];
  reason?: string;
}

export interface RuleEvaluationResult {
  strategy_id?: string;
  evaluated_at: string;
  reference_timestamp?: string;
  subject_timestamp?: string;
  reference_series_result?: GroupResult | ConditionResult;
  subject_series_result?: GroupResult | ConditionResult;
  overall_status: EvaluationStatus;
  passed_condition_ids: string[];
  failed_condition_ids: string[];
  unavailable_condition_ids: string[];
  invalid_condition_ids: string[];
}

export interface RuleEvaluationRequest {
  strategy_id?: string;
  strategy?: any;
  reference_dataset_id?: string;
  subject_dataset_id?: string;
  eval_timestamp?: string;
}

export async function evaluateRules(req: RuleEvaluationRequest): Promise<RuleEvaluationResult> {
  const res = await fetch(`${API_BASE_URL}/rules/evaluate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });

  if (!res.ok) {
    const errData = await res.json();
    const message = typeof errData.detail === "string" ? errData.detail : JSON.stringify(errData.detail);
    throw new Error(message || "Failed to evaluate rules");
  }
  return res.json();
}

export async function getSupportedOperators(): Promise<{ operators: any[] }> {
  const res = await fetch(`${API_BASE_URL}/rules/operators`);
  if (!res.ok) throw new Error("Failed to fetch supported operators list");
  return res.json();
}

export type DatasetCategory = "REFERENCE" | "SUBJECT";

export interface DatasetManifestEntry {
  dataset_id: string;
  display_name: string;
  description: string;
  instrument_id: string;
  timeframe: string;
  candle_count: number;
  completed_candle_count: number;
  category: DatasetCategory;
  is_synthetic: boolean;
}

export interface SeriesEvaluationResult {
  dataset_id: string;
  instrument_id: string;
  timeframe: string;
  evaluation_timestamp: string;
  candle_timestamp_used?: string | null;
  overall_status: EvaluationStatus;
  reference_result?: GroupResult | ConditionResult | null;
  subject_result?: GroupResult | ConditionResult | null;
  passed_condition_ids: string[];
  failed_condition_ids: string[];
  unavailable_condition_ids: string[];
  invalid_condition_ids: string[];
  inspection_summary: string;
}

export interface MultiSeriesEvaluationResult {
  strategy_id?: string | null;
  requested_evaluation_timestamp: string;
  reference_dataset_id: string;
  reference_timestamp_used?: string | null;
  results: SeriesEvaluationResult[];
  status_counts: Record<string, number>;
  total_series_evaluated: number;
  warnings: string[];
}

export interface MultiSeriesEvaluationRequest {
  strategy_id?: string;
  strategy?: any;
  reference_dataset_id: string;
  subject_dataset_ids: string[];
  eval_timestamp: string;
}

export async function getDatasetManifest(): Promise<DatasetManifestEntry[]> {
  const res = await fetch(`${API_BASE_URL}/multi-series/datasets`);
  if (!res.ok) throw new Error("Failed to fetch dataset manifest");
  return res.json();
}

export async function evaluateMultiSeries(req: MultiSeriesEvaluationRequest): Promise<MultiSeriesEvaluationResult> {
  const res = await fetch(`${API_BASE_URL}/multi-series/evaluate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });

  if (!res.ok) {
    const errData = await res.json();
    const message = typeof errData.detail === "string" ? errData.detail : JSON.stringify(errData.detail);
    throw new Error(message || "Failed to evaluate multi-series rules");
  }
  return res.json();
}
