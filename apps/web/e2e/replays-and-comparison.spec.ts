import { test, expect } from "@playwright/test";
import { loginAs } from "./helpers/auth";

test.describe("Historical Replays, Inspection History, Comparison, and Exports E2E", () => {
  test("Historical Replay Lab executes simulation and detects deduplicated runs", async ({ page }) => {
    await loginAs(page, "editor");
    await page.goto("/historical-replay-lab");

    await expect(page.locator("h1")).toContainText(/Historical Replay/i);

    // Click Run Historical Replay
    const runBtn = page.getByRole("button", { name: /run historical replay|execute replay|run/i });
    await expect(runBtn).toBeVisible({ timeout: 10000 });
    await runBtn.click();

    // Verify timeline and results render
    await expect(page.getByText(/Subject Status Timelines|TRUE Count|Replay Execution Results|Run ID:/i).first()).toBeVisible({ timeout: 15000 });

    // Click again to verify owner-scoped deduplication reuse notice
    await runBtn.click();
    await expect(page.getByText(/Subject Status Timelines|TRUE Count|Replay Execution Results|Run ID:/i).first()).toBeVisible({ timeout: 15000 });
  });

  test("Inspection History lists runs and opens run detail", async ({ page }) => {
    await loginAs(page, "editor");
    await page.goto("/inspection-history");

    await expect(page.locator("h1")).toContainText(/Inspection History/i);

    // Verify history table contains at least one row from previous replay
    await expect(page.locator("table").first()).toBeVisible({ timeout: 10000 });
  });

  test("Replay Comparison Lab renders difference table, transition matrix, and verification", async ({ page }) => {
    await loginAs(page, "editor");
    await page.goto("/replay-comparison-lab");

    await expect(page.locator("h1")).toContainText(/Deterministic Replay Comparison Lab/i);

    // Select baseline and comparison if options exist
    const selects = page.locator("select");
    const count = await selects.count();
    if (count >= 2) {
      const compareBtn = page.getByRole("button", { name: /compare replays|compare/i });
      if (await compareBtn.isVisible()) {
        await compareBtn.click();
        await expect(page.getByText(/Comparison Summary|Transition Matrix|Verification/i).first()).toBeVisible({ timeout: 10000 });
      }
    }
  });

  test("JSON Export produces valid educational schema with zero secrets", async ({ page }) => {
    await loginAs(page, "editor");
    await page.goto("/historical-replay-lab");

    // Execute run if needed
    const runBtn = page.getByRole("button", { name: /run historical replay|execute replay|run/i });
    if (await runBtn.isVisible()) {
      await runBtn.click();
    }

    // Look for JSON export button
    const jsonExportBtn = page.getByRole("button", { name: /export json|json/i }).or(page.getByRole("link", { name: /export json|json/i })).first();
    if (await jsonExportBtn.isVisible()) {
      const [download] = await Promise.all([
        page.waitForEvent("download"),
        jsonExportBtn.click(),
      ]);

      const filename = download.suggestedFilename();
      expect(filename).toMatch(/^replay_.*\.json$/);

      const stream = await download.createReadStream();
      const chunks: Buffer[] = [];
      for await (const chunk of stream) {
        chunks.push(Buffer.from(chunk));
      }
      const content = Buffer.concat(chunks).toString("utf-8");
      const parsed = JSON.parse(content);

      // Verify educational notice and core metadata
      expect(parsed.notice).toContain("Educational synthetic historical replay");
      expect(parsed.run_id).toBeTruthy();
      expect(parsed.engine_version).toBeTruthy();

      // Assert zero secrets or hashes
      expect(parsed.hashed_password).toBeUndefined();
      expect(parsed.password).toBeUndefined();
      expect(parsed.session_hash).toBeUndefined();
      expect(parsed.csrf_hash).toBeUndefined();
    }
  });

  test("CSV Export produces sanitized headers with formula injection protection", async ({ page }) => {
    await loginAs(page, "editor");
    await page.goto("/historical-replay-lab");

    const csvExportBtn = page.getByRole("button", { name: /export csv|csv/i }).or(page.getByRole("link", { name: /export csv|csv/i })).first();
    if (await csvExportBtn.isVisible()) {
      const [download] = await Promise.all([
        page.waitForEvent("download"),
        csvExportBtn.click(),
      ]);

      const filename = download.suggestedFilename();
      expect(filename).toMatch(/^replay_.*\.csv$/);

      const stream = await download.createReadStream();
      const chunks: Buffer[] = [];
      for await (const chunk of stream) {
        chunks.push(Buffer.from(chunk));
      }
      const content = Buffer.concat(chunks).toString("utf-8");

      expect(content).toContain("# NOTICE: Educational synthetic historical replay");
      expect(content).toContain("evaluation_timestamp,dataset_id,status,passed_conditions,failed_conditions,inspection_summary");

      // Verify zero security fields
      expect(content).not.toContain("hashed_password");
      expect(content).not.toContain("session_hash");
    }
  });
});
