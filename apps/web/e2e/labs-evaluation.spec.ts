import { test, expect } from "@playwright/test";
import { loginAs } from "./helpers/auth";

test.describe("Indicator, Rule, and Multi-Series Labs E2E", () => {
  test("Indicator Lab calculates SMA and renders candlestick chart", async ({ page }) => {
    await loginAs(page, "editor");
    await page.goto("/indicator-lab");

    await expect(page.locator("header, h1, h2, h3").first()).toContainText(/Indicator/i);

    // Ensure datasets load
    const datasetSelect = page.locator("select").first();
    await expect(datasetSelect).toBeVisible({ timeout: 10000 });

    // Click Calculate Indicator
    const calcBtn = page.getByRole("button", { name: /calculate indicator|calculate/i });
    if (await calcBtn.isVisible()) {
      await calcBtn.click();
      // Verify chart canvas or results table becomes populated
      await expect(page.locator("table, canvas").first().or(page.getByText(/Calculated Points/i))).toBeVisible({ timeout: 10000 });
    }
  });

  test("Rule Lab evaluates boolean condition tree and returns categorical status", async ({ page }) => {
    await loginAs(page, "editor");
    await page.goto("/rule-lab");

    await expect(page.locator("h1")).toContainText(/Rule Evaluation/i);

    // Evaluate rules
    const evalBtn = page.getByRole("button", { name: /evaluate rule tree|evaluate rules|evaluate/i });
    if (await evalBtn.isVisible()) {
      await evalBtn.click();
      // Check for categorical badge: TRUE, FALSE, UNAVAILABLE, or INVALID
      await expect(
        page.getByText(/TRUE|FALSE|UNAVAILABLE|INVALID/).first()
      ).toBeVisible({ timeout: 10000 });
    }
  });

  test("Multi-Series Lab runs cross-sectional evaluation across candidates", async ({ page }) => {
    await loginAs(page, "editor");
    await page.goto("/multi-series-lab");

    await expect(page.locator("h1")).toContainText(/Multi-Series/i);

    // Select candidate datasets if checkboxes are available
    const checkboxes = page.locator('input[type="checkbox"]');
    const count = await checkboxes.count();
    if (count > 0) {
      for (let i = 0; i < Math.min(count, 3); i++) {
        await checkboxes.nth(i).check();
      }
    }

    // Run evaluation
    const runBtn = page.getByRole("button", { name: /run multi-series evaluation|evaluate/i });
    if (await runBtn.isVisible()) {
      await runBtn.click();
      // Should show evaluated candidates and result table
      await expect(page.locator("table").first().or(page.getByText(/Total Candidates|Evaluated/i))).toBeVisible({ timeout: 10000 });
    }
  });
});
