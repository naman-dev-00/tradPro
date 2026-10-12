import { test, expect } from "@playwright/test";
import { loginAs } from "./helpers/auth";

test.describe("Broker Readiness & Live Guardrails E2E", () => {
  test("renders read-only constraints banner and live dispatch disabled badge", async ({ page }) => {
    await loginAs(page, "editor");
    await page.goto("/broker-readiness");

    // Verify page header
    await expect(page.locator("h1")).toContainText(/Broker Readiness/i);

    // Verify Read-Only Safeguard Banner
    await expect(page.getByText(/Phase 7A: Read-Only Broker Connection/i)).toBeVisible();
    await expect(page.getByText(/live_dispatch_enabled: false/i)).toBeVisible();
    await expect(page.getByText(/Live order placement and cancellation remain strictly blocked/i)).toBeVisible();

    // Verify Live Dispatch Status badge is DISABLED
    const liveDispatchBadge = page.locator("#live-dispatch-badge");
    await expect(liveDispatchBadge).toBeVisible();
    await expect(liveDispatchBadge).toContainText(/Live Orders Disabled/i);

    // Verify no secret input fields in DOM
    const secretInputs = page.locator("input[type='password'], input[name*='secret' i]");
    const count = await secretInputs.count();
    expect(count).toBe(0);
  });

  test("browser acceptance flow: strips code and state from URL on callback and posts exactly once", async ({ page }) => {
    await loginAs(page, "editor");

    let callbackPostCount = 0;

    // Intercept callback POST to simulate broker response and assert exactly once
    await page.route("**/api/v1/broker/auth/callback", async (route) => {
      callbackPostCount++;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          provider: "UPSTOX",
          environment: "LIVE",
          status: "CONNECTED",
          broker_user_id_masked: "***789",
          account_name: "E2E Operator",
          token_valid: true,
          token_expires_at: "2026-10-09T03:30:00Z",
          last_successful_refresh_at: null,
          version: 1,
          credential_generation: 1,
        }),
      });
    });

    await page.goto("/broker-readiness?code=test_code_123&state=test_state_456");

    // Wait for URL to be cleaned (code and state stripped)
    await page.waitForFunction(() => {
      const search = window.location.search;
      return !search.includes("code=") && !search.includes("state=");
    }, { timeout: 10000 });

    const currentUrl = page.url();
    expect(currentUrl).not.toContain("code=");
    expect(currentUrl).not.toContain("state=");

    // Verify callback POST occurred exactly once
    expect(callbackPostCount).toBe(1);

    // Verify success outcome displayed with masked account
    await expect(page.getByText(/Successfully connected to UPSTOX/i)).toBeVisible();
    await expect(page.getByText(/\*\*\*789/i)).toBeVisible();

    // Verify live dispatch remains disabled
    const liveDispatchBadge = page.locator("#live-dispatch-badge");
    await expect(liveDispatchBadge).toBeVisible();
    await expect(liveDispatchBadge).toContainText(/Live Orders Disabled/i);

    // Verify no credentials exposed
    const secretInputs = page.locator("input[type='password'], input[name*='secret' i]");
    expect(await secretInputs.count()).toBe(0);
  });

  test("cleans callback URL parameters and displays failure outcome on error", async ({ page }) => {
    await loginAs(page, "editor");

    let failCallbackCount = 0;

    // Intercept callback POST to simulate failure and assert exactly once
    await page.route("**/api/v1/broker/auth/callback", async (route) => {
      failCallbackCount++;
      await route.fulfill({
        status: 400,
        contentType: "application/json",
        body: JSON.stringify({ detail: "OAuth state token has expired." }),
      });
    });

    await page.goto("/broker-readiness?code=expired_code&state=expired_state");

    // Wait for URL to be cleaned even on error
    await page.waitForFunction(() => {
      const search = window.location.search;
      return !search.includes("code=") && !search.includes("state=");
    }, { timeout: 10000 });

    const currentUrl = page.url();
    expect(currentUrl).not.toContain("code=");
    expect(currentUrl).not.toContain("state=");

    // Verify callback POST occurred exactly once
    expect(failCallbackCount).toBe(1);

    // Verify failure outcome displayed correctly
    await expect(page.getByText(/OAuth state token has expired/i)).toBeVisible();

    // Verify credentials not exposed
    const secretInputs = page.locator("input[type='password'], input[name*='secret' i]");
    expect(await secretInputs.count()).toBe(0);
  });
});
