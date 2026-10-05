"use client";

import React, { useState, useEffect, useRef } from "react";
import { activateOrchestration, OrchestrationActivationPayload, fetchOrchestrationConfig,
  fetchOrchestrationReadiness, createOrchestrationConfig, fetchPaperRuntime, validatePaperRuntime,
  fetchSandboxMappings, getDatasetManifest, OrchestrationConfigResponse, StrategyRuntime,
  ProviderInstrumentMappingResponse, DatasetManifestEntry } from "../../lib/api";

interface Props {
  runtimeId: string;
  isOpen: boolean;
  onClose: () => void;
  onActivated: () => void;
}

export const OrchestrationActivationModal: React.FC<Props> = ({
  runtimeId,
  isOpen,
  onClose,
  onActivated,
}) => {
  const [policy, setPolicy] = useState<"INTERNAL_MOCK_ONLY" | "INTERNAL_PAPER">("INTERNAL_MOCK_ONLY");
  const [confirmNoExternal, setConfirmNoExternal] = useState(false);
  const [confirmFixtureReplay, setConfirmFixtureReplay] = useState(false);
  const [confirmOperatorAuth, setConfirmOperatorAuth] = useState(false);
  const [confirmExecutionPolicy, setConfirmExecutionPolicy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [config, setConfig] = useState<OrchestrationConfigResponse | null>(null);
  const [runtime, setRuntime] = useState<StrategyRuntime | null>(null);
  const [initializing, setInitializing] = useState(true);
  const [mappings, setMappings] = useState<ProviderInstrumentMappingResponse[]>([]);
  const [datasets, setDatasets] = useState<DatasetManifestEntry[]>([]);
  const [referenceId, setReferenceId] = useState("");
  const [mappingId, setMappingId] = useState("");
  const [replayOpen, setReplayOpen] = useState("");
  const [replayClose, setReplayClose] = useState("");
  const [readiness, setReadiness] = useState<string[]>([]);

  useEffect(() => {
    if (!isOpen) return;
    let cancelled = false;
    const initialize = async () => {
      setInitializing(true);
      setError(null);
      setConfig(null);
      setRuntime(null);
      setReadiness([]);
      setConfirmNoExternal(false);
      setConfirmFixtureReplay(false);
      setConfirmOperatorAuth(false);
      setConfirmExecutionPolicy(false);
      try {
        const [existing, rt, maps, manifest] = await Promise.all([
          fetchOrchestrationConfig(runtimeId), fetchPaperRuntime(runtimeId), fetchSandboxMappings(), getDatasetManifest(),
        ]);
        if (cancelled) return;
        setRuntime(rt);
        setConfig(existing);
        if (!rt.instrument_id) {
          setError("Runtime is missing an authoritative orderable instrument identity.");
          setMappings([]);
        } else {
          setMappings(maps.filter(m => m.verification_status === "VERIFIED" && m.tradepro_instrument_id === rt.instrument_id));
        }
        setDatasets(manifest.filter(d => d.timeframe === rt.timeframe));
        if (existing) {
          if (existing.execution_policy !== "INTERNAL_PAPER" && existing.execution_policy !== "INTERNAL_MOCK_ONLY") throw new Error("Unsupported existing execution policy");
          setPolicy(existing.execution_policy);
          setReplayOpen(existing.replay_open_at);
          setReplayClose(existing.replay_close_at);
          const ready = await fetchOrchestrationReadiness(runtimeId);
          if (!cancelled) {
            const blockers = [...ready.reasons];
            if (!rt.instrument_id) blockers.push("Runtime is missing an authoritative orderable instrument identity.");
            setReadiness(blockers);
          }
        } else {
          setPolicy("INTERNAL_MOCK_ONLY");
          setReferenceId(""); setMappingId(""); setReplayOpen(""); setReplayClose("");
          if (!rt.instrument_id) {
            setReadiness(["Runtime is missing an authoritative orderable instrument identity."]);
          }
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load configuration");
      } finally {
        if (!cancelled) setInitializing(false);
      }
    };
    void initialize();
    return () => { cancelled = true; };
  }, [runtimeId, isOpen]);

  const modalRef = useRef<HTMLDivElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const previousActiveElement = useRef<Element | null>(null);
  const onCloseRef = useRef(onClose);

  useEffect(() => {
    onCloseRef.current = onClose;
  });

  useEffect(() => {
    if (!isOpen) return;

    previousActiveElement.current = document.activeElement;
    closeButtonRef.current?.focus();

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onCloseRef.current();
        return;
      }
      if (e.key === "Tab" && modalRef.current) {
        const focusableElements = modalRef.current.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
        );
        if (focusableElements.length === 0) return;

        const firstElement = focusableElements[0];
        const lastElement = focusableElements[focusableElements.length - 1];

        if (e.shiftKey) {
          if (document.activeElement === firstElement) {
            e.preventDefault();
            lastElement.focus();
          }
        } else {
          if (document.activeElement === lastElement) {
            e.preventDefault();
            firstElement.focus();
          }
        }
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      if (previousActiveElement.current instanceof HTMLElement) {
        previousActiveElement.current.focus();
      }
    };
  }, [isOpen]);

  // Reset confirmation check if policy changes
  const handlePolicyChange = (newPolicy: "INTERNAL_MOCK_ONLY" | "INTERNAL_PAPER") => {
    setPolicy(newPolicy);
    setConfirmExecutionPolicy(false);
    setError(null);
  };

  const isFormValid =
    confirmNoExternal &&
    confirmFixtureReplay &&
    confirmOperatorAuth &&
    confirmExecutionPolicy &&
    !initializing &&
    runtime !== null &&
    Boolean(runtime.instrument_id) &&
    (config !== null || Boolean(referenceId && mappingId && replayOpen && replayClose));

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!isFormValid || loading || !runtime?.instrument_id) return;

    setLoading(true);
    setError(null);

    const payload: OrchestrationActivationPayload = {
      consent_version: policy === "INTERNAL_PAPER" ? "fixture_paper_consent_v1" : "fixture_consent_v1",
      acknowledged_execution_policy: policy,
      confirm_internal_mock_only: policy === "INTERNAL_MOCK_ONLY",
      confirm_internal_paper_execution: policy === "INTERNAL_PAPER" ? true : undefined,
    };

    try {
      if (!config) {
        if (!runtime || (runtime.timeframe !== "5m" && runtime.timeframe !== "15m")) throw new Error("Unsupported runtime timeframe");
        if (runtime.status === "DRAFT") {
          const validation = await validatePaperRuntime(runtimeId);
          if (!validation.valid) throw new Error(validation.errors?.join("; ") || "Runtime validation failed");
        }
        const open = new Date(replayOpen.endsWith("Z") ? replayOpen : `${replayOpen}Z`).toISOString();
        const close = new Date(replayClose.endsWith("Z") ? replayClose : `${replayClose}Z`).toISOString();
        if (open >= close) throw new Error("Replay end must follow replay start");
        const chosenDatasets = [
          { dataset_id: referenceId, series_role: "REFERENCE" as const },
          { dataset_id: runtime.dataset_id, series_role: "SUBJECT" as const },
        ];
        const created = await createOrchestrationConfig({
          runtime_id: runtimeId, timeframe: runtime.timeframe, strategy_version: 1,
          replay_open_at: open, replay_close_at: close, provider_mapping_id: mappingId,
          execution_policy: policy, datasets: chosenDatasets,
          consent: {
            consent_version: payload.consent_version, acknowledged_source_type: "FIXTURE_REPLAY",
            acknowledged_execution_policy: policy, acknowledged_timeframe: runtime.timeframe,
            acknowledged_replay_open_at: open, acknowledged_replay_close_at: close,
            acknowledged_dataset_ids: chosenDatasets.map(d => d.dataset_id),
            confirm_prohibition_of_live_trading: confirmNoExternal,
            confirm_internal_mock_only: policy === "INTERNAL_MOCK_ONLY" && confirmExecutionPolicy,
            confirm_internal_paper_execution: policy === "INTERNAL_PAPER" && confirmExecutionPolicy,
          },
        });
        setConfig(created);
      }
      const ready = await fetchOrchestrationReadiness(runtimeId);
      setReadiness(ready.reasons);
      if (!ready.ready) throw new Error(ready.reasons.join("; ") || "Runtime is not ready");
      await activateOrchestration(runtimeId, payload);
      onActivated();
      onClose();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Failed to activate orchestration";
      setError(msg);
    } finally {
      setLoading(false);
    }
  };

  if (!isOpen) return null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="activation-modal-title"
      aria-describedby="activation-modal-desc"
      className="fixed inset-0 z-50 bg-slate-950/80 backdrop-blur-sm flex items-center justify-center p-4"
    >
      <div
        ref={modalRef}
        className="bg-slate-900 border border-slate-800 rounded-xl max-w-lg w-full p-6 space-y-5 shadow-2xl max-h-[90vh] overflow-y-auto"
      >
        <div className="flex items-center justify-between border-b border-slate-800 pb-3">
          <div>
            <h3 id="activation-modal-title" className="text-base font-bold text-white">
              Activate Orchestration Runtime
            </h3>
            <p id="activation-modal-desc" className="text-xs text-slate-400 font-mono">
              Runtime ID: {runtimeId}
            </p>
          </div>
          <button
            ref={closeButtonRef}
            onClick={onClose}
            aria-label="Close activation dialog"
            className="text-slate-400 hover:text-white text-lg font-bold p-1 rounded focus:outline-none focus:ring-2 focus:ring-indigo-500"
          >
            ✕
          </button>
        </div>

        {error && (
          <div
            role="alert"
            className="p-3 bg-rose-950/60 border border-rose-700 rounded text-xs text-rose-300"
          >
            {error}
          </div>
        )}

        {initializing && <p role="status">Loading runtime configuration...</p>}
        {config && <p className="text-xs text-slate-300">Existing configuration is immutable: {config.execution_policy}, {config.replay_open_at} to {config.replay_close_at}.</p>}
        {readiness.length > 0 && <ul aria-label="Readiness blockers">{readiness.map(reason => <li key={reason}>{reason}</li>)}</ul>}
        <form onSubmit={handleSubmit} className="space-y-4">
          {!config && !initializing && <fieldset className="space-y-2 text-xs">
            <legend>Configure fixture replay</legend>
            <label className="block">Reference dataset
              <select aria-label="Reference dataset" value={referenceId} onChange={e => setReferenceId(e.target.value)}>
                <option value="">Select reference dataset</option>
                {datasets.filter(d => d.category === "REFERENCE").map(d => <option key={d.dataset_id} value={d.dataset_id}>{d.display_name}</option>)}
              </select>
            </label>
            <p>Execution dataset: {runtime?.dataset_id}</p>
            <label className="block">Verified instrument mapping
              <select aria-label="Verified instrument mapping" value={mappingId} onChange={e => setMappingId(e.target.value)}>
                <option value="">Select verified mapping</option>
                {mappings.map(m => <option key={m.id} value={m.id}>{m.symbol} (v{m.mapping_version})</option>)}
              </select>
            </label>
            {mappings.length === 0 && (
              <p className="text-amber-400">
                {!runtime?.instrument_id
                  ? "Runtime is missing an authoritative orderable instrument identity."
                  : "A verified mapping for this instrument is required. Manage mappings in the Sandbox tab."}
              </p>
            )}
            <label className="block">Replay start (UTC)<input aria-label="Replay start (UTC)" type="datetime-local" value={replayOpen} onChange={e => setReplayOpen(e.target.value)} /></label>
            <label className="block">Replay end (UTC)<input aria-label="Replay end (UTC)" type="datetime-local" value={replayClose} onChange={e => setReplayClose(e.target.value)} /></label>
          </fieldset>}
          <div>
            <label className="block text-xs font-semibold text-slate-300 mb-2">
              Select Execution Policy:
            </label>
            <div className="grid grid-cols-2 gap-3">
              <label
                className={`flex items-start p-3 border rounded-lg cursor-pointer transition-colors ${
                  policy === "INTERNAL_MOCK_ONLY"
                    ? "bg-slate-800/80 border-indigo-500 ring-1 ring-indigo-500"
                    : "bg-slate-950/50 border-slate-800 hover:bg-slate-800/40"
                }`}
              >
                <input
                  disabled={Boolean(config) || initializing}
                  type="radio"
                  name="execution_policy"
                  value="INTERNAL_MOCK_ONLY"
                  checked={policy === "INTERNAL_MOCK_ONLY"}
                  onChange={() => handlePolicyChange("INTERNAL_MOCK_ONLY")}
                  className="mt-0.5 mr-2 text-indigo-600 focus:ring-indigo-500"
                />
                <div>
                  <div className="text-xs font-bold text-white">INTERNAL_MOCK_ONLY</div>
                  <div className="text-[11px] text-slate-400 mt-0.5">
                    Evaluates rules and conditions without generating paper orders or ledger reservations.
                  </div>
                </div>
              </label>

              <label
                className={`flex items-start p-3 border rounded-lg cursor-pointer transition-colors ${
                  policy === "INTERNAL_PAPER"
                    ? "bg-indigo-950/40 border-indigo-500 ring-1 ring-indigo-500"
                    : "bg-slate-950/50 border-slate-800 hover:bg-slate-800/40"
                }`}
              >
                <input
                  disabled={Boolean(config) || initializing}
                  type="radio"
                  name="execution_policy"
                  value="INTERNAL_PAPER"
                  checked={policy === "INTERNAL_PAPER"}
                  onChange={() => handlePolicyChange("INTERNAL_PAPER")}
                  className="mt-0.5 mr-2 text-indigo-600 focus:ring-indigo-500"
                />
                <div>
                  <div className="text-xs font-bold text-indigo-300">INTERNAL_PAPER</div>
                  <div className="text-[11px] text-slate-400 mt-0.5">
                    Generates paper orders, reserves funds, simulates fills, and updates paper ledger.
                  </div>
                </div>
              </label>
            </div>
          </div>

          <div className="bg-slate-950/60 border border-slate-800 rounded-lg p-3.5 space-y-2.5">
            <span className="text-[11px] font-bold uppercase tracking-wider text-slate-400">
              Required Operator Consent ({policy === "INTERNAL_PAPER" ? "fixture_paper_consent_v1" : "fixture_consent_v1"}):
            </span>

            <label className="flex items-start gap-2 text-xs text-slate-300 cursor-pointer">
              <input
                id="consent-no-external"
                type="checkbox"
                checked={confirmNoExternal}
                onChange={(e) => setConfirmNoExternal(e.target.checked)}
                className="mt-0.5 rounded border-slate-700 text-indigo-600 focus:ring-indigo-500"
              />
              <span>I confirm external broker transmission is prohibited and zero network calls will be made.</span>
            </label>

            <label className="flex items-start gap-2 text-xs text-slate-300 cursor-pointer">
              <input
                id="consent-fixture-replay"
                type="checkbox"
                checked={confirmFixtureReplay}
                onChange={(e) => setConfirmFixtureReplay(e.target.checked)}
                className="mt-0.5 rounded border-slate-700 text-indigo-600 focus:ring-indigo-500"
              />
              <span>I confirm execution is driven solely by deterministic fixture candle boundaries.</span>
            </label>

            <label className="flex items-start gap-2 text-xs text-slate-300 cursor-pointer">
              <input
                id="consent-operator-auth"
                type="checkbox"
                checked={confirmOperatorAuth}
                onChange={(e) => setConfirmOperatorAuth(e.target.checked)}
                className="mt-0.5 rounded border-slate-700 text-indigo-600 focus:ring-indigo-500"
              />
              <span>I acknowledge operator authorization for this automated replay session.</span>
            </label>

            <label className="flex items-start gap-2 text-xs text-slate-300 cursor-pointer">
              <input
                id="consent-policy-acknowledgement"
                type="checkbox"
                checked={confirmExecutionPolicy}
                onChange={(e) => setConfirmExecutionPolicy(e.target.checked)}
                className="mt-0.5 rounded border-slate-700 text-indigo-600 focus:ring-indigo-500"
              />
              <span>
                {policy === "INTERNAL_PAPER"
                  ? "I explicitly consent to internal paper order generation and cash ledger reservations (fixture_paper_consent_v1)."
                  : "I confirm evaluation-only mock policy with no order creation (fixture_consent_v1)."}
              </span>
            </label>
          </div>

          <div className="flex justify-end gap-2 pt-2 border-t border-slate-800">
            <button
              type="button"
              onClick={onClose}
              className="px-3.5 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-medium rounded transition-colors"
            >
              Cancel
            </button>
            <button
              id="submit-activate-orchestration-btn"
              type="submit"
              disabled={!isFormValid || loading}
              className="px-4 py-1.5 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 disabled:cursor-not-allowed text-white text-xs font-semibold rounded shadow transition-colors"
            >
              {loading ? "Activating..." : `Activate (${policy})`}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};
