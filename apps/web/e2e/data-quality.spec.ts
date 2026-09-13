import { test, expect } from "@playwright/test";
import { loginAs } from "./helpers/auth";

test.describe("Dataset Quality & Provenance Diagnostics E2E", () => {
  test("dynamically discovers packaged dataset fixtures and matches runtime API manifest", async ({ page, request }) => {
    // 1. Query runtime dataset API to discover actual manifest count and IDs
    const apiUrl = process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000";
    const response = await request.get(`${apiUrl}/api/v1/data-quality/datasets`);
    expect(response.ok()).toBeTruthy();
    const datasets = await response.json();

    expect(Array.isArray(datasets)).toBeTruthy();
    expect(datasets.length).toBeGreaterThan(0);

    // Assert unique dataset IDs
    const datasetIds = datasets.map((d: any) => d.dataset_id);
    const uniqueIds = new Set(datasetIds);
    expect(uniqueIds.size).toBe(datasetIds.length);

    // 2. Navigate to Dataset Quality Lab UI and verify all discovered datasets render
    await loginAs(page, "editor");
    await page.goto("/data-quality-lab");

    await expect(page.locator("h1")).toContainText(/Synthetic Dataset Quality & Provenance Lab/i);

    // Verify each discovered fixture is present in the UI
    for (const dataset of datasets) {
      await expect(page.getByText(dataset.dataset_id).first()).toBeVisible({ timeout: 5000 });
    }

    // 3. Trigger batch audit and verify aggregate summary completion
    const auditBtn = page.getByRole("button", { name: /run batch quality audit|batch audit|audit all/i });
    if (await auditBtn.isVisible()) {
      await auditBtn.click();
      await expect(page.locator("text=Batch Audit Summary, text=Datasets Audited, text=Overall Quality Status").first()).toBeVisible({ timeout: 15000 });
    }
  });
});
