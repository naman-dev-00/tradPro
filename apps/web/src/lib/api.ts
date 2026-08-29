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
