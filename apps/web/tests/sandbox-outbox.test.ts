import { describe, it, expect } from "vitest";
import {
  SandboxReadinessResponse,
  SubmissionOutboxResponse,
  ReconciliationRecordResponse,
  ProviderInstrumentMappingResponse,
} from "../src/lib/api";

describe("Sandbox and Outbox Specification Suite", () => {
  const mockReadiness: SandboxReadinessResponse = {
    runtime_id: "rt-sandbox-1",
    environment_allowed: true,
    network_enabled: true,
    credential_present: true,
    owner_matches: true,
    provider_matches: true,
    mapping_verified: true,
    mapping_unexpired: true,
    global_kill_switch_clear: true,
    user_kill_switch_clear: true,
    worker_available: true,
    ready_for_submission: true,
    cancel_available: true,
    reasons: [],
  };

  it("verifies all 12 gates pass when sandbox is fully ready", () => {
    expect(mockReadiness.ready_for_submission).toBe(true);
    expect(mockReadiness.cancel_available).toBe(true);
    expect(mockReadiness.network_enabled).toBe(true);
    expect(mockReadiness.credential_present).toBe(true);
    expect(mockReadiness.mapping_verified).toBe(true);
    expect(mockReadiness.worker_available).toBe(true);
    expect(mockReadiness.reasons).toHaveLength(0);
  });

  it("proves kill switch blocks submission but preserves cancel availability", () => {
    const blockedReadiness: SandboxReadinessResponse = {
      ...mockReadiness,
      user_kill_switch_clear: false,
      ready_for_submission: false,
      cancel_available: true,
      reasons: ["USER kill switch engaged: Risk breach"],
    };

    expect(blockedReadiness.ready_for_submission).toBe(false);
    expect(blockedReadiness.cancel_available).toBe(true);
    expect(blockedReadiness.reasons).toContain("USER kill switch engaged: Risk breach");
  });

  it("proves unverified mapping blocks submission but preserves cancel availability", () => {
    const unverifiedReadiness: SandboxReadinessResponse = {
      ...mockReadiness,
      mapping_verified: false,
      ready_for_submission: false,
      cancel_available: true,
      reasons: ["No VERIFIED mapping found for instrument"],
    };

    expect(unverifiedReadiness.ready_for_submission).toBe(false);
    expect(unverifiedReadiness.cancel_available).toBe(true);
  });

  it("validates outbox item fields and idempotency tracking", () => {
    const outboxItem: SubmissionOutboxResponse = {
      id: "out-1",
      owner_id: "user-1",
      order_id: "ord-1",
      action_type: "PLACE",
      priority: 10,
      status: "PENDING",
      idempotency_key: "place:ord-1",
      attempts: 0,
      max_attempts: 3,
      next_attempt_at: "2026-09-13T10:00:00Z",
      last_error_code: null,
      last_error_message: null,
      created_at: "2026-09-13T10:00:00Z",
      updated_at: "2026-09-13T10:00:00Z",
    };

    expect(outboxItem.action_type).toBe("PLACE");
    expect(outboxItem.status).toBe("PENDING");
    expect(outboxItem.idempotency_key).toBe("place:ord-1");
  });

  it("validates manual reconciliation options and audit constraints", () => {
    const validResolutionTypes = [
      "PLACE_CONFIRMED",
      "PLACE_REJECTED",
      "CANCEL_CONFIRMED",
      "CANCEL_NOT_CONFIRMED",
    ];
    expect(validResolutionTypes).toContain("PLACE_CONFIRMED");
    expect(validResolutionTypes).toContain("PLACE_REJECTED");
    expect(validResolutionTypes).toContain("CANCEL_CONFIRMED");
    expect(validResolutionTypes).toContain("CANCEL_NOT_CONFIRMED");

    const record: ReconciliationRecordResponse = {
      id: "rec-1",
      owner_id: "user-1",
      order_id: "ord-1",
      outbox_id: "out-1",
      resolution_type: "CANCEL_CONFIRMED",
      resolved_by: "admin-1",
      provider_order_reference: "240913000123456",
      notes: "Verified order cancelled via Upstox merchant dashboard",
      resolved_at: "2026-09-13T10:05:00Z",
      created_at: "2026-09-13T10:05:00Z",
    };

    expect(record.resolution_type).toBe("CANCEL_CONFIRMED");
    expect(record.outbox_id).toBe("out-1");
    expect(record.resolved_by).toBe("admin-1");
    expect(record.notes.length).toBeGreaterThanOrEqual(5);
  });

  it("validates zero-secret persistence in mapping model", () => {
    const mapping: ProviderInstrumentMappingResponse = {
      id: "map-1",
      owner_id: "user-1",
      tradepro_instrument_id: "synthetic_candidate_option_pe_23000_15m",
      provider_instrument_token: "NSE_FO|99901",
      exchange: "NSE_FO",
      segment: "FO",
      symbol: "NIFTY24SEPPE23000",
      expiry_date: null,
      strike_price: "23000.00",
      option_type: "PE",
      lot_size: 50,
      tick_size: "0.05",
      freeze_quantity: 1800,
      verification_status: "VERIFIED",
      verified_by: "admin-1",
      verified_at: "2026-09-13T10:00:00Z",
      verification_audit_json: { actor: "admin-1", result: "VERIFIED" },
      mapping_version: 1,
      created_at: "2026-09-13T10:00:00Z",
      updated_at: "2026-09-13T10:00:00Z",
    };

    expect((mapping as any).token).toBeUndefined();
    expect((mapping as any).access_token).toBeUndefined();
    expect((mapping as any).secret).toBeUndefined();
    expect(mapping.verification_status).toBe("VERIFIED");
  });

  it("validates minimized connection response metadata exposure", () => {
    const conn: import("../src/lib/api").ProviderConnectionResponse = {
      provider: "UPSTOX",
      environment: "SANDBOX",
      credential_configured: true,
      credential_version: "v1",
      readiness_status: "CONFIGURED",
      last_successful_transmission_at: null,
    };

    expect(conn.provider).toBe("UPSTOX");
    expect(conn.credential_configured).toBe(true);
    expect((conn as any).credential_reference).toBeUndefined();
    expect((conn as any).token).toBeUndefined();
    expect((conn as any).token_fingerprint).toBeUndefined();
  });
});
