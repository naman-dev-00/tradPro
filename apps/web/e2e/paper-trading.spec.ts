import { test, expect } from "@playwright/test";
import { loginAs } from "./helpers/auth";

test.describe("Paper Trading Runtime, OMS & Risk Controls E2E", () => {
  test("verifies paper simulation disclaimer and educational safeguards", async ({ page }) => {
    await loginAs(page, "editor");
    await page.goto("/paper-trading-lab");

    // 1. Prominent simulation banner must be present with required exact text
    const banner = page.locator("#paper-simulation-banner");
    await expect(banner).toBeVisible({ timeout: 10000 });
    await expect(banner).toContainText("PAPER SIMULATION — NO LIVE ORDERS");
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
    await page.locator("#submit-create-account-btn").click();

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
});
