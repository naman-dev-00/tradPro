const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

// --- Milestone 5B: Authentication & Authorization ---

export interface User {
  id: string;
  username: string;
  email: string;
  role: "VIEWER" | "EDITOR" | "ADMIN";
  is_active: boolean;
  created_at: string;
}

export interface LegacyTransferRequest {
  target_user_id: string;
  resource_type: "STRATEGIES" | "INSPECTION_RUNS";
  resource_ids: string[];
}

export interface LegacyTransferResponse {
  transferred_count: number;
  rejected_count: number;
  message: string;
}

export function getCsrfTokenFromCookie(): string | null {
  if (typeof document === "undefined") return null;
  const match = document.cookie.match(/(?:^|;\s*)tradepro_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : null;
}

export async function fetchCsrfToken(): Promise<string> {
  const res = await fetch(`${API_BASE_URL}/api/v1/auth/csrf-token`, {
    credentials: "include",
  });
  if (!res.ok) {
    throw new Error("Failed to fetch CSRF token");
  }
  const data = await res.json();
  return data.csrf_token;
}

export async function apiFetch(path: string, options: RequestInit = {}): Promise<Response> {
  const url = path.startsWith("http") ? path : `${API_BASE_URL}${path}`;
  const method = (options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers || {});

  if (!headers.has("Content-Type") && options.body && typeof options.body === "string") {
    headers.set("Content-Type", "application/json");
  }

  // Attach CSRF token for non-safe methods if not already set
  if (["POST", "PUT", "PATCH", "DELETE"].includes(method)) {
    if (!headers.has("X-CSRF-Token")) {
      let csrf = getCsrfTokenFromCookie();
      if (!csrf) {
        try {
          csrf = await fetchCsrfToken();
        } catch (err) {
          console.warn("Could not fetch CSRF token before mutation:", err);
        }
      }
      if (csrf) {
        headers.set("X-CSRF-Token", csrf);
      }
    }
  }

  return fetch(url, {
    ...options,
    headers,
    credentials: "include",
  });
}

export async function getCurrentUser(): Promise<User | null> {
  try {
    const res = await apiFetch("/api/v1/auth/me");
    if (res.status === 401 || res.status === 403) {
      return null;
    }
    if (!res.ok) {
      return null;
    }
    return await res.json();
  } catch {
    return null;
  }
}

export async function loginUser(username_or_email: string, password: string): Promise<User> {
  const csrf = await fetchCsrfToken();

  const res = await apiFetch("/api/v1/auth/login", {
    method: "POST",
    headers: {
      "X-CSRF-Token": csrf,
    },
    body: JSON.stringify({ username_or_email, password }),
  });

  if (!res.ok) {
    const errData = await res.json().catch(() => ({}));
    const message = typeof errData.detail === "string" ? errData.detail : "Invalid credentials";
    throw new Error(message);
  }

  return res.json();
}

export async function logoutUser(): Promise<void> {
  const res = await apiFetch("/api/v1/auth/logout", {
    method: "POST",
  });
  if (!res.ok && res.status !== 401) {
    const errData = await res.json().catch(() => ({}));
    throw new Error(errData.detail || "Logout failed");
  }
}

// --- Strategy Builder & Core Engine ---

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
    const res = await apiFetch("/health");
    if (!res.ok) return false;
    const data = await res.json();
    return data.status === "healthy";
  } catch {
    return false;
  }
}

export async function validateStrategy(payload: any): Promise<ValidationResponse> {
  const res = await apiFetch("/strategies/validate", {
    method: "POST",
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
    ? `/strategies/${payload.id}`
    : `/strategies`;

  const res = await apiFetch(url, {
    method: isUpdate ? "PUT" : "POST",
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
  const res = await apiFetch("/strategies");
  if (!res.ok) throw new Error("Failed to fetch strategies list");
  return res.json();
}

export async function getStrategyById(id: string): Promise<StrategyResponse> {
  const res = await apiFetch(`/strategies/${id}`);
  if (!res.ok) throw new Error(`Failed to fetch strategy with id ${id}`);
  return res.json();
}

export async function getSyntheticDatasets(): Promise<{ datasets: DatasetMetadata[] }> {
  const res = await apiFetch("/indicators/datasets");
  if (!res.ok) throw new Error("Failed to fetch synthetic datasets list");
  return res.json();
}

export async function getSyntheticDatasetById(datasetId: string): Promise<DatasetDetailResponse> {
  const res = await apiFetch(`/indicators/datasets/${datasetId}`);
  if (!res.ok) {
    const errData = await res.json();
    throw new Error(errData.detail || `Failed to fetch dataset '${datasetId}'`);
  }
  return res.json();
}

export async function getSupportedIndicators(): Promise<{ indicators: SupportedIndicatorMetadata[] }> {
  const res = await apiFetch("/indicators/supported");
  if (!res.ok) throw new Error("Failed to fetch supported indicators list");
  return res.json();
}

export async function calculateIndicator(req: CalculateIndicatorRequest): Promise<CalculateIndicatorResponse> {
  const res = await apiFetch("/indicators/calculate", {
    method: "POST",
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
  const res = await apiFetch("/rules/evaluate", {
    method: "POST",
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
  const res = await apiFetch("/rules/operators");
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

export type CandidateEvaluationResult = SeriesEvaluationResult;

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
  const res = await apiFetch("/multi-series/datasets");
  if (!res.ok) throw new Error("Failed to fetch dataset manifest");
  return res.json();
}

export async function evaluateMultiSeries(req: MultiSeriesEvaluationRequest): Promise<MultiSeriesEvaluationResult> {
  const res = await apiFetch("/multi-series/evaluate", {
    method: "POST",
    body: JSON.stringify(req),
  });

  if (!res.ok) {
    const errData = await res.json();
    const message = typeof errData.detail === "string" ? errData.detail : JSON.stringify(errData.detail);
    throw new Error(message || "Failed to evaluate multi-series rules");
  }
  return res.json();
}

export interface ReplayPoint {
  evaluation_timestamp: string;
  reference_timestamp_used?: string | null;
  results: SeriesEvaluationResult[];
  status_counts: Record<string, number>;
  warnings: string[];
}

export interface SubjectStatusTimeline {
  dataset_id: string;
  points: { timestamp: string; status: string; inspection_summary?: string }[];
  transition_counts: Record<string, number>;
  consecutive_status_runs: Record<string, number>;
  first_available_timestamp?: string | null;
  unavailable_point_count: number;
  invalid_point_count: number;
}

export interface HistoricalReplayResult {
  run_id?: string | null;
  strategy_id?: string | null;
  start_timestamp: string;
  end_timestamp: string;
  sampling_step: number;
  sampled_timestamp_count: number;
  total_evaluations: number;
  reference_dataset_id: string;
  reference_metadata: Record<string, any>;
  subject_dataset_ids: string[];
  subject_metadata: Record<string, any>[];
  replay_points: ReplayPoint[];
  subject_timelines: SubjectStatusTimeline[];
  aggregate_status_counts: Record<string, number>;
  reproducibility: {
    is_exact_match: boolean;
    mismatches?: Record<string, any>;
    warning?: string | null;
    engine_version?: string;
    manifest_version?: string;
    request_fingerprint?: string;
    completed_fingerprint?: string;
    synthetic_data_confirmed?: boolean;
  };
  failure_summary?: string;
  is_reused?: boolean;
}

export interface HistoricalReplayRequest {
  strategy_id?: string;
  strategy_payload?: any;
  reference_dataset_id: string;
  subject_dataset_ids: string[];
  start_timestamp: string;
  end_timestamp: string;
  sampling_step?: number;
}

export async function createHistoricalReplay(req: HistoricalReplayRequest): Promise<{ run_id: string; status: string; is_reused: boolean; run: any }> {
  const res = await apiFetch("/api/v1/replays", {
    method: "POST",
    body: JSON.stringify(req),
  });

  if (!res.ok) {
    const errData = await res.json();
    const message = typeof errData.detail === "string" ? errData.detail : JSON.stringify(errData.detail);
    throw new Error(message || "Historical replay creation failed");
  }
  return res.json();
}

export const executeHistoricalReplay = createHistoricalReplay;

export interface InspectionRunSummaryResponse {
  id: string;
  strategy_id?: string | null;
  strategy_name?: string | null;
  run_type: string;
  reference_dataset_id?: string | null;
  subject_dataset_ids: string[];
  requested_start_timestamp?: string | null;
  requested_end_timestamp?: string | null;
  timeframe: string;
  engine_version: string;
  manifest_version: string;
  created_at: string;
  completed_at?: string | null;
  status: string;
  failure_summary?: string | null;
  result_summary?: string | null;
  synthetic_data_confirmed: boolean;
  is_exact_match: boolean;
}

export type InspectionRunListItem = InspectionRunSummaryResponse;

export interface PaginatedInspectionRunList {
  items: InspectionRunSummaryResponse[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

export type InspectionHistoryResponse = PaginatedInspectionRunList;

export async function listInspectionRuns(params: {
  page?: number;
  page_size?: number;
  strategy_id?: string;
  status?: string;
  run_type?: string;
  start_date?: string;
  end_date?: string;
} = {}): Promise<PaginatedInspectionRunList> {
  const query = new URLSearchParams();
  if (params.page) query.set("page", params.page.toString());
  if (params.page_size) query.set("page_size", params.page_size.toString());
  if (params.strategy_id) query.set("strategy_id", params.strategy_id);
  if (params.status) query.set("status", params.status);
  if (params.run_type) query.set("run_type", params.run_type);
  if (params.start_date) query.set("start_date", params.start_date);
  if (params.end_date) query.set("end_date", params.end_date);

  const res = await apiFetch(`/api/v1/replays?${query.toString()}`);
  if (!res.ok) throw new Error("Failed to fetch inspection runs list");
  const data = await res.json();
  const page_size = data.page_size || params.page_size || 20;
  return {
    items: (data.items || []).map((item: any) => ({
      ...item,
      subject_dataset_ids: item.subject_dataset_ids || [],
      is_exact_match: item.is_exact_match ?? true,
      synthetic_data_confirmed: item.synthetic_data_confirmed ?? true,
    })),
    total: data.total || 0,
    page: data.page || params.page || 1,
    page_size: page_size,
    total_pages: data.total_pages || Math.ceil((data.total || 0) / page_size) || 1,
  };
}

export async function fetchInspectionHistory(page = 1, pageSize = 20, status?: string): Promise<InspectionHistoryResponse> {
  return listInspectionRuns({ page, page_size: pageSize, status });
}

export async function getInspectionRunDetail(runId: string): Promise<any> {
  const res = await apiFetch(`/api/v1/replays/${runId}`);
  if (!res.ok) {
    const errData = await res.json();
    throw new Error(errData.detail || `Failed to fetch run '${runId}'`);
  }
  return res.json();
}

export const fetchInspectionRunDetail = getInspectionRunDetail;

export async function getInspectionRunReproducibility(runId: string): Promise<any> {
  const res = await apiFetch(`/api/v1/replays/${runId}/reproducibility`);
  if (!res.ok) throw new Error("Failed to fetch reproducibility status");
  return res.json();
}

export function getExportJsonUrl(runId: string): string {
  return `${API_BASE_URL}/api/v1/replays/${runId}/export.json`;
}

export function getExportCsvUrl(runId: string): string {
  return `${API_BASE_URL}/api/v1/replays/${runId}/export.csv`;
}

export function getExportUrl(runId: string, format: "json" | "csv" = "json"): string {
  return `${API_BASE_URL}/api/v1/replays/${runId}/export?format=${format}`;
}

export interface DatasetChecksumResult {
  dataset_id: string;
  stored_checksum?: string | null;
  current_checksum?: string | null;
  matches: boolean;
}

export interface ReplayVerificationResult {
  run_id: string;
  verification_status: "VERIFIED" | "MISMATCH" | "UNVERIFIABLE" | "INVALID";
  stored_request_fingerprint?: string | null;
  recomputed_request_fingerprint?: string | null;
  fingerprint_matches: boolean;
  stored_manifest_version?: string | null;
  current_manifest_version?: string | null;
  manifest_version_matches: boolean;
  stored_engine_version?: string | null;
  current_engine_version?: string | null;
  engine_version_matches: boolean;
  stored_replay_schema_version?: string | null;
  current_replay_schema_version?: string | null;
  replay_schema_version_matches: boolean;
  dataset_checksum_results: DatasetChecksumResult[];
  strategy_snapshot_present: boolean;
  result_payload_present: boolean;
  reasons: string[];
}

export type VerificationResult = ReplayVerificationResult;

export interface ReplayComparisonRequest {
  baseline_run_id: string;
  comparison_run_id: string;
  include_unchanged?: boolean;
}

export interface ReplayStatusDifference {
  timestamp: string;
  dataset_id: string;
  baseline_present: boolean;
  comparison_present: boolean;
  baseline_status?: string | null;
  comparison_status?: string | null;
  changed: boolean;
  baseline_condition_ids: Record<string, string[]>;
  comparison_condition_ids: Record<string, string[]>;
  newly_true_condition_ids: string[];
  no_longer_true_condition_ids: string[];
  newly_false_condition_ids: string[];
  no_longer_false_condition_ids: string[];
  newly_unavailable_condition_ids: string[];
  newly_invalid_condition_ids: string[];
  explanation: string;
}

export interface ReplayComparisonResult {
  baseline_metadata: Record<string, any>;
  comparison_metadata: Record<string, any>;
  aligned_point_count: number;
  baseline_only_point_count: number;
  comparison_only_point_count: number;
  unchanged_point_count: number;
  changed_point_count: number;
  status_transition_counts: Record<string, number>;
  differences: ReplayStatusDifference[];
  warnings: string[];
}

export type ReplayComparisonResponse = ReplayComparisonResult;

export async function verifyReplayRun(runId: string): Promise<ReplayVerificationResult> {
  const res = await apiFetch(`/api/v1/replays/${runId}/verify`);
  if (!res.ok) {
    const errData = await res.json();
    throw new Error(errData.detail || `Failed to verify run '${runId}'`);
  }
  return res.json();
}

export async function compareReplays(
  reqOrBaseline: ReplayComparisonRequest | string,
  comparisonRunId?: string,
  includeUnchanged = false
): Promise<ReplayComparisonResult> {
  const body = typeof reqOrBaseline === "string"
    ? { baseline_run_id: reqOrBaseline, comparison_run_id: comparisonRunId, include_unchanged: includeUnchanged }
    : reqOrBaseline;

  const res = await apiFetch("/api/v1/replays/compare", {
    method: "POST",
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const errData = await res.json();
    const message = typeof errData.detail === "string" ? errData.detail : JSON.stringify(errData.detail);
    throw new Error(message || "Failed to compare replay runs");
  }
  return res.json();
}

// Milestone 5A: Dataset Quality & Diagnostics Interfaces

export type DatasetQualityStatus = "PASS" | "WARN" | "FAIL";
export type DatasetIssueSeverity = "INFO" | "WARNING" | "ERROR";

export type DatasetIssueCode =
  | "FILE_UNAVAILABLE"
  | "CSV_HEADER_INVALID"
  | "ROW_MALFORMED"
  | "TIMESTAMP_INVALID"
  | "TIMESTAMP_NOT_UTC"
  | "TIMESTAMP_OUT_OF_ORDER"
  | "DUPLICATE_TIMESTAMP"
  | "TIMEFRAME_UNSUPPORTED"
  | "TIMEFRAME_INTERVAL_MISMATCH"
  | "MISSING_INTERVAL"
  | "INSTRUMENT_ID_MISMATCH"
  | "TIMEFRAME_VALUE_MISMATCH"
  | "NON_FINITE_VALUE"
  | "NEGATIVE_PRICE"
  | "NEGATIVE_VOLUME"
  | "OHLC_HIGH_BOUND_INVALID"
  | "OHLC_LOW_BOUND_INVALID"
  | "INCOMPLETE_CANDLE_PRESENT"
  | "MANIFEST_COUNT_MISMATCH"
  | "COMPLETED_COUNT_MISMATCH"
  | "CHECKSUM_MISMATCH"
  | "MANIFEST_METADATA_MISMATCH"
  | "INSUFFICIENT_DATA_FOR_WARMUP"
  | "DATASET_ROW_LIMIT_EXCEEDED";

export interface DatasetQualityIssue {
  code: DatasetIssueCode;
  severity: DatasetIssueSeverity;
  message: string;
  row_number?: number | null;
  timestamp?: string | null;
  field?: string | null;
  expected?: string | null;
  actual?: string | null;
}

export interface DatasetQualitySummary {
  total_rows: number;
  valid_rows: number;
  malformed_rows: number;
  completed_rows: number;
  incomplete_rows: number;
  duplicate_timestamp_count: number;
  missing_interval_count: number;
  first_timestamp?: string | null;
  last_timestamp?: string | null;
  expected_interval_seconds?: number | null;
  calculated_checksum?: string | null;
  manifest_checksum?: string | null;
  checksum_matches?: boolean | null;
}

export interface DatasetProvenance {
  dataset_id: string;
  display_name: string;
  category: "REFERENCE" | "SUBJECT";
  instrument_id: string;
  timeframe: string;
  is_synthetic: true;
  manifest_version: string;
  fixture_checksum?: string | null;
  source_type: "PACKAGED_SYNTHETIC_FIXTURE";
  immutable: true;
}

export interface DatasetQualityReport {
  dataset_id: string;
  status: DatasetQualityStatus;
  provenance: DatasetProvenance;
  summary: DatasetQualitySummary;
  issues: DatasetQualityIssue[];
  total_issue_count: number;
  reported_issue_count: number;
  issues_truncated: boolean;
  audit_rules_version: string;
  warnings: string[];
}

export interface DatasetQualityListItem {
  dataset_id: string;
  display_name: string;
  category: "REFERENCE" | "SUBJECT";
  instrument_id: string;
  timeframe: string;
  status: DatasetQualityStatus;
  summary: DatasetQualitySummary;
  provenance: DatasetProvenance;
}

export interface DatasetAuditBatchResponse {
  reports: DatasetQualityReport[];
  status_counts: Record<"PASS" | "WARN" | "FAIL", number>;
  total_datasets: number;
  audit_rules_version: string;
  warnings: string[];
}

export async function fetchDatasetQualitySummaries(): Promise<DatasetQualityListItem[]> {
  const res = await apiFetch("/api/v1/data-quality/datasets");
  if (!res.ok) {
    const errData = await res.json();
    throw new Error(errData.detail || "Failed to fetch dataset quality summaries");
  }
  return res.json();
}

export async function fetchDatasetQualityReport(datasetId: string): Promise<DatasetQualityReport> {
  const res = await apiFetch(`/api/v1/data-quality/datasets/${datasetId}`);
  if (!res.ok) {
    const errData = await res.json();
    throw new Error(errData.detail || `Failed to fetch quality report for dataset '${datasetId}'`);
  }
  return res.json();
}

export async function auditDatasetsBatch(datasetIds: string[]): Promise<DatasetAuditBatchResponse> {
  const res = await apiFetch("/api/v1/data-quality/audit", {
    method: "POST",
    body: JSON.stringify({ dataset_ids: datasetIds }),
  });
  if (!res.ok) {
    const errData = await res.json();
    const message = typeof errData.detail === "string" ? errData.detail : JSON.stringify(errData.detail);
    throw new Error(message || "Failed to run batch dataset audit");
  }
  return res.json();
}

export function getDataQualityExportUrl(datasetId: string): string {
  return `${API_BASE_URL}/api/v1/data-quality/datasets/${datasetId}/export`;
}

// --- Milestone 6A: Paper Trading Runtime, OMS & Risk Engine ---

export interface PaperAccount {
  id: string;
  owner_id: string;
  name: string;
  currency: string;
  total_cash: string;
  reserved_cash: string;
  available_cash: string;
  is_active: boolean;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface AccountLedgerEntry {
  id: string;
  account_id: string;
  sequence_number: number;
  entry_type: string;
  amount: string;
  balance_after: string;
  order_id?: string | null;
  fill_id?: string | null;
  reason_code: string;
  created_at: string;
}

export interface ActionRuleMapping {
  mapping_id: string;
  rule_target: "GLOBAL" | "CANDIDATE";
  trigger_status: "ON_TRUE" | "ON_FALSE";
  instrument_id: string;
  side: "BUY" | "SELL";
  order_type: "MARKET" | "LIMIT";
  quantity: string | number;
  limit_price?: string | number | null;
  time_in_force: "DAY" | "GTC";
  cooldown_bars?: number;
  intent_type: "ENTRY" | "EXIT" | "REDUCE" | "REVERSE";
}

export interface StrategyActionPolicy {
  id: string;
  owner_id: string;
  strategy_id: string;
  name: string;
  version: number;
  payload: {
    entry_mapping: ActionRuleMapping;
    exit_mapping?: ActionRuleMapping;
    position_exists_behavior: "IGNORE" | "REJECT" | "SCALE" | "REVERSE";
    max_entries_per_day: number;
  };
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface RiskPolicy {
  id: string;
  owner_id: string;
  name: string;
  version: number;
  payload: Record<string, any>;
  is_default: boolean;
  created_at: string;
  updated_at: string;
}

export interface StrategyRuntime {
  id: string;
  owner_id: string;
  strategy_id: string;
  action_policy_id: string;
  risk_policy_id: string;
  account_id: string;
  status: "DRAFT" | "READY" | "RUNNING" | "PAUSED" | "HALTED" | "STOPPED" | "ERROR";
  trading_mode: string;
  dataset_id: string;
  timeframe: string;
  last_processed_candle_timestamp?: string | null;
  consecutive_errors: number;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface OrderEvent {
  id: string;
  sequence_number: number;
  previous_status: string;
  new_status: string;
  actor: string;
  reason_code: string;
  metadata_json?: Record<string, any> | null;
  created_at: string;
}

export interface Order {
  id: string;
  owner_id: string;
  runtime_id: string;
  intent_id: string;
  account_id: string;
  order_sequence_number: number;
  instrument_id: string;
  side: "BUY" | "SELL";
  order_type: "MARKET" | "LIMIT";
  quantity: string;
  limit_price?: string | null;
  filled_quantity: string;
  status: string;
  version: number;
  events?: OrderEvent[];
  created_at: string;
  updated_at: string;
}

export interface Fill {
  id: string;
  order_id: string;
  account_id: string;
  instrument_id: string;
  side: "BUY" | "SELL";
  quantity: string;
  price: string;
  fee: string;
  candle_timestamp: string;
  created_at: string;
}

export interface PaperPosition {
  id: string;
  account_id: string;
  instrument_id: string;
  net_quantity: string;
  average_entry_price: string;
  cost_basis: string;
  gross_realized_pnl: string;
  total_fees: string;
  net_realized_pnl: string;
  last_mark_price: string;
  unrealized_pnl: string;
  updated_at: string;
}

export interface KillSwitchStatus {
  global_active: boolean;
  global_engaged_at?: string | null;
  global_reason?: string | null;
  user_active: boolean;
  user_engaged_at?: string | null;
  user_reason?: string | null;
}

export async function fetchPaperAccounts(): Promise<PaperAccount[]> {
  const res = await apiFetch("/api/v1/paper/accounts");
  if (!res.ok) throw new Error("Failed to fetch paper accounts");
  return res.json();
}

export async function createPaperAccount(name: string, initial_balance: string | number, currency: string = "INR"): Promise<PaperAccount> {
  const res = await apiFetch("/api/v1/paper/accounts", {
    method: "POST",
    body: JSON.stringify({ name, initial_balance, currency }),
  });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail || "Failed to create paper account");
  }
  return res.json();
}

export async function fetchAccountLedger(accountId: string): Promise<AccountLedgerEntry[]> {
  const res = await apiFetch(`/api/v1/paper/accounts/${accountId}/ledger`);
  if (!res.ok) throw new Error("Failed to fetch account ledger");
  return res.json();
}

export async function fetchActionPolicies(): Promise<StrategyActionPolicy[]> {
  const res = await apiFetch("/api/v1/paper/action-policies");
  if (!res.ok) throw new Error("Failed to fetch action policies");
  return res.json();
}

export async function createActionPolicy(policyData: any): Promise<StrategyActionPolicy> {
  const res = await apiFetch("/api/v1/paper/action-policies", {
    method: "POST",
    body: JSON.stringify(policyData),
  });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail || "Failed to create action policy");
  }
  return res.json();
}

export async function fetchPaperRuntimes(): Promise<StrategyRuntime[]> {
  const res = await apiFetch("/api/v1/paper/runtimes");
  if (!res.ok) throw new Error("Failed to fetch paper runtimes");
  return res.json();
}

export async function createPaperRuntime(data: {
  strategy_id: string;
  account_id: string;
  dataset_id: string;
  timeframe?: string;
  trading_mode?: string;
  action_policy_id?: string;
  risk_policy_id?: string;
}): Promise<StrategyRuntime> {
  const res = await apiFetch("/api/v1/paper/runtimes", {
    method: "POST",
    body: JSON.stringify(data),
  });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail || "Failed to create runtime");
  }
  return res.json();
}

export async function validatePaperRuntime(runtimeId: string): Promise<{ valid: boolean; status?: string; errors?: string[] }> {
  const res = await apiFetch(`/api/v1/paper/runtimes/${runtimeId}/validate`, { method: "POST" });
  if (!res.ok) throw new Error("Failed to validate runtime");
  return res.json();
}

export async function startPaperRuntime(runtimeId: string): Promise<StrategyRuntime> {
  const res = await apiFetch(`/api/v1/paper/runtimes/${runtimeId}/start`, { method: "POST" });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail || "Failed to start runtime");
  }
  return res.json();
}

export async function pausePaperRuntime(runtimeId: string): Promise<StrategyRuntime> {
  const res = await apiFetch(`/api/v1/paper/runtimes/${runtimeId}/pause`, { method: "POST" });
  if (!res.ok) throw new Error("Failed to pause runtime");
  return res.json();
}

export async function resumePaperRuntime(runtimeId: string): Promise<StrategyRuntime> {
  const res = await apiFetch(`/api/v1/paper/runtimes/${runtimeId}/resume`, { method: "POST" });
  if (!res.ok) throw new Error("Failed to resume runtime");
  return res.json();
}

export async function stopPaperRuntime(runtimeId: string): Promise<StrategyRuntime> {
  const res = await apiFetch(`/api/v1/paper/runtimes/${runtimeId}/stop`, { method: "POST" });
  if (!res.ok) throw new Error("Failed to stop runtime");
  return res.json();
}

export async function stepPaperRuntime(runtimeId: string, stepCount: number = 1): Promise<{
  runtime_id: string;
  status: string;
  last_candle_timestamp: string;
  steps_executed: number;
  fills_executed: number;
  intents_created: number;
}> {
  const res = await apiFetch(`/api/v1/paper/runtimes/${runtimeId}/step`, {
    method: "POST",
    body: JSON.stringify({ step_count: stepCount }),
  });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail || "Failed to step runtime");
  }
  return res.json();
}

export async function fetchRuntimeEvents(runtimeId: string): Promise<OrderEvent[]> {
  const res = await apiFetch(`/api/v1/paper/runtimes/${runtimeId}/events`);
  if (!res.ok) throw new Error("Failed to fetch runtime events");
  return res.json();
}

export async function fetchPaperOrders(runtimeId?: string): Promise<Order[]> {
  const path = runtimeId ? `/api/v1/paper/orders?runtime_id=${runtimeId}` : "/api/v1/paper/orders";
  const res = await apiFetch(path);
  if (!res.ok) throw new Error("Failed to fetch orders");
  return res.json();
}

export async function cancelPaperOrder(orderId: string): Promise<Order> {
  const res = await apiFetch(`/api/v1/paper/orders/${orderId}/cancel`, { method: "POST" });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail || "Failed to cancel order");
  }
  return res.json();
}

export async function fetchPaperPositions(accountId?: string): Promise<PaperPosition[]> {
  const path = accountId ? `/api/v1/paper/positions?account_id=${accountId}` : "/api/v1/paper/positions";
  const res = await apiFetch(path);
  if (!res.ok) throw new Error("Failed to fetch positions");
  return res.json();
}

export async function fetchPaperFills(accountId?: string): Promise<Fill[]> {
  const path = accountId ? `/api/v1/paper/fills?account_id=${accountId}` : "/api/v1/paper/fills";
  const res = await apiFetch(path);
  if (!res.ok) throw new Error("Failed to fetch fills");
  return res.json();
}

export async function fetchKillSwitchStatus(): Promise<KillSwitchStatus> {
  const res = await apiFetch("/api/v1/paper/kill-switch");
  if (!res.ok) throw new Error("Failed to fetch kill switch status");
  return res.json();
}

export async function engageKillSwitch(scope: "GLOBAL" | "USER", reason: string): Promise<any> {
  const res = await apiFetch("/api/v1/paper/kill-switch", {
    method: "POST",
    body: JSON.stringify({ scope, reason }),
  });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail || "Failed to engage kill switch");
  }
  return res.json();
}

export async function resetKillSwitch(scope: "GLOBAL" | "USER", reason: string): Promise<any> {
  const res = await apiFetch("/api/v1/paper/kill-switch/reset", {
    method: "POST",
    body: JSON.stringify({ scope, reason }),
  });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail || "Failed to reset kill switch");
  }
  return res.json();
}

// --- Milestone 6B Part 1: Upstox Sandbox & Outbox API ---

export interface SandboxReadinessResponse {
  runtime_id: string;
  environment_allowed: boolean;
  network_enabled: boolean;
  credential_present: boolean;
  owner_matches: boolean;
  provider_matches: boolean;
  mapping_verified: boolean;
  mapping_unexpired: boolean;
  global_kill_switch_clear: boolean;
  user_kill_switch_clear: boolean;
  worker_available: boolean;
  ready_for_submission: boolean;
  cancel_available: boolean;
  reasons: string[];
}

export interface ProviderConnectionResponse {
  provider: string;
  environment: string;
  credential_configured: boolean;
  credential_version: string;
  readiness_status: string;
  last_successful_transmission_at: string | null;
}


export interface ProviderInstrumentMappingResponse {
  id: string;
  owner_id: string;
  tradepro_instrument_id: string;
  provider_instrument_token: string;
  exchange: string;
  segment: string;
  symbol: string;
  expiry_date: string | null;
  strike_price: string | null;
  option_type: string | null;
  lot_size: number;
  tick_size: string;
  freeze_quantity: number;
  verification_status: string;
  verified_by: string | null;
  verified_at: string | null;
  verification_audit_json: Record<string, any> | null;
  mapping_version: number;
  created_at: string;
  updated_at: string;
}

export interface SubmissionOutboxResponse {
  id: string;
  owner_id: string;
  order_id: string;
  action_type: string;
  priority: number;
  status: string;
  idempotency_key: string;
  attempts: number;
  max_attempts: number;
  next_attempt_at: string;
  last_error_code: string | null;
  last_error_message: string | null;
  created_at: string;
  updated_at: string;
}

export interface ReconciliationRecordResponse {
  id: string;
  owner_id: string;
  order_id: string;
  outbox_id: string;
  resolution_type: "PLACE_CONFIRMED" | "PLACE_REJECTED" | "CANCEL_CONFIRMED" | "CANCEL_NOT_CONFIRMED";
  resolved_by: string;
  provider_order_reference: string | null;
  notes: string;
  resolved_at: string;
  created_at: string;
}

export async function fetchSandboxReadiness(runtimeId: string): Promise<SandboxReadinessResponse> {
  const res = await apiFetch(`/api/v1/sandbox/readiness?runtime_id=${runtimeId}`);
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail || "Failed to fetch sandbox readiness");
  }
  return res.json();
}

export async function fetchSandboxConnection(): Promise<ProviderConnectionResponse | null> {
  const res = await apiFetch("/api/v1/sandbox/connection");
  if (res.status === 404) return null;
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail || "Failed to fetch provider connection");
  }
  return res.json();
}

export async function fetchSandboxMappings(): Promise<ProviderInstrumentMappingResponse[]> {
  const res = await apiFetch("/api/v1/sandbox/instruments");
  if (!res.ok) throw new Error("Failed to fetch instrument mappings");
  return res.json();
}

export async function createSandboxMapping(payload: {
  tradepro_instrument_id: string;
  provider_instrument_token: string;
  exchange: string;
  segment: string;
  symbol: string;
  lot_size?: number;
  tick_size?: number;
  freeze_quantity?: number;
}): Promise<ProviderInstrumentMappingResponse> {
  const res = await apiFetch("/api/v1/sandbox/instruments", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail || "Failed to create instrument mapping");
  }
  return res.json();
}

export async function verifySandboxMapping(
  mappingId: string,
  status: "VERIFIED" | "REJECTED" | "DISABLED",
  reason: string
): Promise<ProviderInstrumentMappingResponse> {
  const res = await apiFetch(`/api/v1/sandbox/instruments/${mappingId}/verify`, {
    method: "POST",
    body: JSON.stringify({ status, reason }),
  });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail || "Failed to verify instrument mapping");
  }
  return res.json();
}

export async function disableSandboxMapping(mappingId: string): Promise<ProviderInstrumentMappingResponse> {
  const res = await apiFetch(`/api/v1/sandbox/instruments/${mappingId}/disable`, {
    method: "POST",
  });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail || "Failed to disable instrument mapping");
  }
  return res.json();
}

export async function fetchSandboxOutbox(
  statusFilter?: string,
  actionTypeFilter?: string
): Promise<SubmissionOutboxResponse[]> {
  const params = new URLSearchParams();
  if (statusFilter) params.append("status", statusFilter);
  if (actionTypeFilter) params.append("action_type", actionTypeFilter);
  const query = params.toString() ? `?${params.toString()}` : "";
  const res = await apiFetch(`/api/v1/sandbox/outbox${query}`);
  if (!res.ok) throw new Error("Failed to fetch sandbox outbox");
  return res.json();
}

export async function fetchReconciliationRecords(): Promise<ReconciliationRecordResponse[]> {
  const res = await apiFetch("/api/v1/sandbox/reconciliations");
  if (!res.ok) throw new Error("Failed to fetch reconciliation records");
  return res.json();
}

export async function resolveReconciliation(
  orderId: string,
  resolutionType: "PLACE_CONFIRMED" | "PLACE_REJECTED" | "CANCEL_CONFIRMED" | "CANCEL_NOT_CONFIRMED",
  notes: string,
  providerOrderReference?: string,
  outboxId?: string
): Promise<ReconciliationRecordResponse> {
  const res = await apiFetch(`/api/v1/sandbox/reconciliations/${orderId}/resolve`, {
    method: "POST",
    body: JSON.stringify({
      resolution_type: resolutionType,
      notes,
      provider_order_reference: providerOrderReference,
      outbox_id: outboxId,
    }),
  });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail || "Failed to resolve reconciliation record");
  }
  return res.json();
}
