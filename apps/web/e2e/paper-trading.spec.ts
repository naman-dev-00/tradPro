import { test, expect } from "@playwright/test";
import { loginAs } from "./helpers/auth";

test.describe("Paper Trading Runtime, OMS & Risk Controls E2E", () => {
  test("verifies paper simulation disclaimer and educational safeguards", async ({ page }) => {
    await loginAs(page, "editor");
    await page.goto("/paper-trading-lab");

    // 1. Prominent simulation banner must be present with required exact text
    const banner = page.locator("#paper-simulation-banner");
    await expect(banner).toBeVisible({ timeout: 10000 });
    await expect(banner).toContainText("PAPER SIMULATION");
    await expect(banner).toContainText(/zero external broker connectivity/i);

    // 2. Heading and description
    await expect(page.locator("h1")).toContainText(/Paper Trading Runtime & OMS/i);

    // 3. Financial cards overview
    await expect(page.locator("#account-available-cash")).toBeVisible();
    await expect(page.locator("#account-reserved-cash")).toBeVisible();
    await expect(page.locator("#account-total-cash")).toBeVisible();
    await expect(page.locator("#account-unrealized-pnl")).toBeVisible();
    await expect(page.locator("#account-realized-pnl")).toBeVisible();
    await expect(page.locator("#portfolio-net-value")).toBeVisible();
  });

  test("creates a new paper account and verifies portfolio balance", async ({ page }) => {
    await loginAs(page, "editor");
    await page.goto("/paper-trading-lab");

    // Click + Account button
    const createAcctBtn = page.locator("#create-account-btn");
    await expect(createAcctBtn).toBeVisible();
    await createAcctBtn.click();

    // Fill new account form in modal
    const uniqueName = `E2E Test Account ${Date.now().toString().slice(-4)}`;
    await page.locator("#new-account-name-input").fill(uniqueName);
    await page.locator("#new-account-balance-input").fill("250000.00");
    const submitBtn = page.locator("#submit-create-account-btn");
    await expect(submitBtn).toBeEnabled({ timeout: 10000 });
    await submitBtn.click();

    // Verify account selector displays new account
    const selector = page.locator("#account-selector");
    await expect(selector).toContainText(uniqueName, { timeout: 10000 });
  });

  test("verifies runtime controls, order book panel, and emergency kill switch workflow", async ({ page }) => {
    await loginAs(page, "editor");
    await page.goto("/paper-trading-lab");

    // 1. Verify Orders & Fills Tabs
    await expect(page.getByRole("button", { name: /orders/i })).toBeVisible();
    await expect(page.getByRole("button", { name: /fills/i })).toBeVisible();

    // Switch to Fills tab
    await page.getByRole("button", { name: /fills/i }).click();
    await expect(page.getByText("No fills recorded yet")).toBeVisible();

    // Switch back to Orders tab
    await page.getByRole("button", { name: /orders/i }).click();
    await expect(page.getByText("No orders placed yet")).toBeVisible();

    // 2. Test Emergency Kill Switch Engagement & Reset
    const killSwitchBtn = page.locator("#kill-switch-btn");
    await expect(killSwitchBtn).toBeVisible();
    await killSwitchBtn.click();

    // In modal, select USER scope, provide reason, and engage
    await expect(page.locator("#kill-switch-modal")).toBeVisible();
    await page.locator("#scope-user-btn").click();
    await page.locator("#kill-reason-input").fill("E2E Automated Safety Verification");
    await page.locator("#kill-switch-confirm-check").check();
    await page.locator("#confirm-kill-switch-btn").click();

    // Verify alert banner appears
    await expect(page.locator("#kill-switch-active-alert")).toBeVisible({ timeout: 10000 });
    await expect(page.locator("#kill-switch-active-alert")).toContainText("EMERGENCY KILL SWITCH ENGAGED");

    // Reset kill switch
    await page.locator("#kill-switch-btn").click();
    await expect(page.locator("#kill-switch-modal")).toBeVisible();
    await page.locator("#kill-reason-input").fill("E2E Test Safety Reset");
    await page.locator("#kill-switch-confirm-check").check();
    await page.locator("#confirm-kill-switch-btn").click();

    // Verify alert banner disappears
    await expect(page.locator("#kill-switch-active-alert")).not.toBeVisible({ timeout: 10000 });
  });

  test("verifies orchestration activation modal, policy selection, submission, and timeline drawer user flows", async ({ page }) => {
    await loginAs(page, "editor");
    await page.goto("/paper-trading-lab");

    // 1. Verify timeline drawer button is visible and test Escape key dismissal
    const timelineBtn = page.locator("#open-timeline-drawer-btn");
    await expect(timelineBtn).toBeVisible({ timeout: 10000 });
    await timelineBtn.click();

    const initialDrawer = page.getByRole("dialog", { name: /orchestration timeline drawer/i });
    await expect(initialDrawer).toBeVisible({ timeout: 5000 });
    await expect(initialDrawer).toContainText(/automated orchestration timeline/i);

    // Test Escape key dismissal on drawer
    await page.keyboard.press("Escape");
    await expect(initialDrawer).not.toBeVisible({ timeout: 5000 });

    // 2. Test Activate Orchestration Modal when runtime is in READY state
    const activateBtn = page.locator("#open-orchestration-modal-btn");
    await expect(activateBtn).toBeVisible({ timeout: 10000 });
    await activateBtn.click();

    const modal = page.getByRole("dialog", { name: /activate orchestration runtime/i });
    await expect(modal).toBeVisible({ timeout: 5000 });

    // Verify default execution policy is INTERNAL_MOCK_ONLY
    await expect(page.locator('input[value="INTERNAL_MOCK_ONLY"]')).toBeChecked();

    // Verify keyboard Escape dismissal on modal
    await page.keyboard.press("Escape");
    await expect(modal).not.toBeVisible({ timeout: 5000 });

    // Reopen modal to proceed through explicit INTERNAL_PAPER submission
    await activateBtn.click();
    await expect(modal).toBeVisible({ timeout: 5000 });

    // Select reference dataset
    await page.getByLabel("Reference dataset").selectOption("synthetic_underlying_nifty_15m");

    // Select verified instrument mapping
    await page.getByLabel("Verified instrument mapping").selectOption({ index: 1 });

    // Fill replay start and end
    await page.getByLabel("Replay start (UTC)").fill("2026-08-28T09:15");
    await page.getByLabel("Replay end (UTC)").fill("2026-08-28T15:30");

    // Select INTERNAL_PAPER
    await page.locator('input[value="INTERNAL_PAPER"]').click();
    await expect(page.locator('input[value="INTERNAL_PAPER"]')).toBeChecked();

    // Verify submit is disabled until all 4 consent checks are completed
    const submitBtn = page.locator("#submit-activate-orchestration-btn");
    await expect(submitBtn).toBeDisabled();

    // Check all required consents
    await page.locator("#consent-no-external").check();
    await expect(page.locator("#consent-no-external")).toBeChecked();
    await page.locator("#consent-fixture-replay").check();
    await expect(page.locator("#consent-fixture-replay")).toBeChecked();
    await page.locator("#consent-operator-auth").check();
    await expect(page.locator("#consent-operator-auth")).toBeChecked();
    await page.locator("#consent-policy-acknowledgement").check();
    await expect(page.locator("#consent-policy-acknowledgement")).toBeChecked();
    await expect(submitBtn).toBeEnabled({ timeout: 5000 });

    // 3. Complete submission against the API
    await submitBtn.click();

    // Verify modal closes upon successful activation
    await expect(modal).not.toBeVisible({ timeout: 10000 });

    // Verify feedback toast confirms activation
    await expect(page.locator("text=Orchestration activated successfully")).toBeVisible({ timeout: 10000 });

    // Verify runtime status updates to RUNNING
    await expect(page.locator("#runtime-status-badge")).toContainText("RUNNING", { timeout: 10000 });

    // 4. Observe reachable timeline evidence after activation
    await timelineBtn.click();
    const activeDrawer = page.getByRole("dialog", { name: /orchestration timeline drawer/i });
    await expect(activeDrawer).toBeVisible({ timeout: 5000 });
    await expect(activeDrawer).toContainText(/automated orchestration timeline/i);

    // Dismiss drawer
    await page.keyboard.press("Escape");
    await expect(activeDrawer).not.toBeVisible({ timeout: 5000 });
  });
});
