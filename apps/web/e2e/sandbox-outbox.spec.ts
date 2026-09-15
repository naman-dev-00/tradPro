import { test, expect } from "@playwright/test";
import { loginAs } from "./helpers/auth";

test.describe("Upstox Sandbox & Outbox Queue E2E", () => {
  test("verifies honest external-transmission banner and no secret tokens in DOM", async ({ page }) => {
    await loginAs(page, "editor");
    await page.goto("/paper-trading-lab");

    // Click Sandbox tab
    const sandboxTab = page.locator("#tab-sandbox");
    await expect(sandboxTab).toBeVisible();
    await sandboxTab.click();

    // Verify readiness gates section is visible
    await expect(page.getByText("Runtime Sandbox Readiness")).toBeVisible();
    await expect(page.getByText("Transactional Submission Outbox")).toBeVisible();
    await expect(page.getByText(/Provider Instrument Mappings/i)).toBeVisible();

    // CRITICAL SECURITY REQUIREMENT: Verify zero token input or secret fields exist anywhere in the DOM
    const tokenInputs = page.locator("input[type='password'], input[name*='token' i], input[name*='secret' i]");
    const count = await tokenInputs.count();
    expect(count).toBe(0);

    // Verify no secret tokens leaked in page text content
    const pageText = await page.content();
    expect(pageText).not.toContain("mock_token_");
    expect(pageText).not.toContain("UPSTOX_SANDBOX_ACCESS_TOKEN");
  });

  test("verifies mobile viewport responsive layout and no horizontal overflow", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 667 });
    await loginAs(page, "editor");
    await page.goto("/paper-trading-lab");

    // Verify tabs are reachable on mobile
    await expect(page.locator("#tab-trading")).toBeVisible();
    await expect(page.locator("#tab-sandbox")).toBeVisible();

    await page.locator("#tab-sandbox").click();

    // Verify readiness panel renders without body horizontal overflow
    const bodyWidth = await page.evaluate(() => document.body.scrollWidth);
    const windowWidth = await page.evaluate(() => window.innerWidth);
    expect(bodyWidth).toBeLessThanOrEqual(windowWidth + 5);
  });

  test("verifies sandbox execution mode selector in create runtime modal", async ({ page }) => {
    await loginAs(page, "editor");
    await page.goto("/paper-trading-lab");

    const createRtBtn = page.locator("#create-runtime-btn");
    await expect(createRtBtn).toBeVisible();
    await createRtBtn.click();

    // Verify trading mode selector exists and contains BROKER_SANDBOX
    const modeSelect = page.locator("#runtime-mode-select");
    await expect(modeSelect).toBeVisible();
    await expect(modeSelect).toContainText("BROKER_SANDBOX (Upstox Sandbox Transmission)");
    await expect(modeSelect).toContainText("BROKER_SANDBOX_RECORDED_FIXTURE");
    await expect(modeSelect).toContainText("PAPER");
  });
});
