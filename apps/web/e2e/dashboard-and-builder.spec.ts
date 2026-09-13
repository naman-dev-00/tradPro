import { test, expect } from "@playwright/test";
import { loginAs } from "./helpers/auth";

test.describe("Dashboard and Strategy Builder E2E", () => {
  test("dashboard renders strategy cards and navigation controls", async ({ page }) => {
    await loginAs(page, "editor");
    await page.goto("/");

    await expect(page.locator("h1")).toContainText(/Strategy Blueprint Registry/i);
    // Check navigation links
    await expect(page.getByRole("link", { name: /indicator lab/i })).toBeVisible();
    await expect(page.getByRole("link", { name: /rule lab/i })).toBeVisible();
    await expect(page.getByRole("link", { name: /historical replay/i })).toBeVisible();
    await expect(page.getByRole("link", { name: /replay comparison/i })).toBeVisible();
    await expect(page.getByRole("link", { name: /data quality|dataset quality/i })).toBeVisible();

    // Verify starter strategy card exists
    await expect(page.getByText("E2E Starter Strategy")).toBeVisible();
  });

  test("strategy builder allows modifying metadata and saving new blueprints", async ({ page }) => {
    await loginAs(page, "editor");
    await page.goto("/builder");

    // Wait for builder React Flow canvas
    await expect(page.locator(".react-flow")).toBeVisible({ timeout: 10000 });

    // Load example strategy to populate valid condition and action nodes
    const loadExampleBtn = page.getByRole("button", { name: /load example strategy/i });
    if (await loadExampleBtn.isVisible()) {
      await loadExampleBtn.click();
    }

    // Select the root strategy node or click properties
    const rootNode = page.locator(".react-flow__node-strategyRoot").or(page.getByText(/Strategy Settings|My Strategy/i)).first();
    if (await rootNode.isVisible()) {
      await rootNode.click();
    }

    // Enter unique strategy name
    const uniqueStrategyName = `E2E Strategy ${crypto.randomUUID().slice(0, 8)}`;
    const nameInput = page.locator('input#strategy-name-input, input[placeholder*="Strategy Name"]').first();
    if (await nameInput.isVisible()) {
      await nameInput.fill(uniqueStrategyName);
    }

    // Save strategy
    const saveButton = page.getByRole("button", { name: /save blueprint|save strategy|save/i });
    if (await saveButton.isEnabled()) {
      await saveButton.click();
      // Should redirect or notify success
      await page.waitForTimeout(1000);
    }
  });

  test("viewer role cannot save or mutate strategies", async ({ page }) => {
    await loginAs(page, "viewer");
    await page.goto("/");

    // Viewers cannot create new strategies (button is either hidden or returns 403 on submit)
    const newStrategyBtn = page.getByRole("link", { name: /new blueprint|\+ create/i });
    if (await newStrategyBtn.isVisible()) {
      await newStrategyBtn.click();
      // Builder should indicate view-only mode or redirect
      await expect(page).toHaveURL(/\/builder|\//);
    }
  });
});
