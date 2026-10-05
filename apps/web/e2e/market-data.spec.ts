import { test, expect } from "@playwright/test";
import { loginAs } from "./helpers/auth";

test.describe("Phase 5: Market Data & Provider Inspection Lab E2E", () => {
  test("navigates to Market Data Lab and displays honest readiness status", async ({ page }) => {
    await loginAs(page, "editor");
    await page.goto("/");

    // Click Market Data link in top navigation
    const marketDataLink = page.getByRole("link", { name: "Market Data" });
    await expect(marketDataLink).toBeVisible();
    await marketDataLink.click();

    // Verify URL and Lab title
    await expect(page).toHaveURL(/.*market-data-lab/);
    await expect(page.locator("h1")).toContainText(/Market Data & Provider Inspection Lab/i);

    // Verify operational boundary disclosure notice
    await expect(page.getByText(/Phase 5 Read-Only Operational Boundary/i)).toBeVisible();

    // Verify zero token input or secret fields exist anywhere in the DOM
    const tokenInputs = page.locator("input[type='password'], input[name*='token' i], input[name*='secret' i]");
    expect(await tokenInputs.count()).toBe(0);

    // Verify approved endpoint host is shown honestly
    await expect(page.getByText("api.upstox.com")).toBeVisible();
  });

  test("acquires completed candles with mocked provider transport and verifies provenance", async ({ page }) => {
    // Mock the market-data candles endpoint to simulate successful provider acquisition
    await page.route("**/api/v1/market-data/candles*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          instrument_key: "NSE_INDEX|Nifty 50",
          tradepro_instrument_id: "NIFTY50_INDEX",
          timeframe: "5m",
          mode: "intraday",
          candles: [
            {
              timestamp: "2026-10-05T09:15:00+00:00",
              open: "25200.0000",
              high: "25250.0000",
              low: "25180.0000",
              close: "25240.0000",
              open_units: 252000000,
              high_units: 252500000,
              low_units: 251800000,
              close_units: 252400000,
              volume: 65000,
              is_closed: true,
            },
            {
              timestamp: "2026-10-05T09:20:00+00:00",
              open: "25240.0000",
              high: "25280.0000",
              low: "25230.0000",
              close: "25275.0000",
              open_units: 252400000,
              high_units: 252800000,
              low_units: 252300000,
              close_units: 252750000,
              volume: 72000,
              is_closed: true,
            },
          ],
          provenance: {
            provider: "UPSTOX",
            source_type: "PROVIDER_UPSTOX_V3",
            retrieved_at: "2026-10-05T10:00:00Z",
            requested_instrument_key: "NSE_INDEX|Nifty 50",
            timeframe: "5m",
            mode: "intraday",
            date_range: null,
            candle_count: 2,
            content_fingerprint: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            completeness: "COMPLETE",
            is_complete_series: true,
            warnings: [],
          },
        }),
      });
    });

    // Also ensure readiness allows clicking fetch button in mocked test
    await page.route("**/api/v1/market-data/readiness*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          network_enabled: true,
          credential_configured: true,
          operator_configured: true,
          is_authorized_operator: true,
          status: "CONFIGURED_AND_ENABLED",
          base_url: "https://api.upstox.com",
          approved_hosts: ["api.upstox.com"],
          supported_timeframes: ["5m", "15m"],
        }),
      });
    });

    await loginAs(page, "editor");
    await page.goto("/market-data-lab");

    // Click Acquire Completed Candles button
    const fetchBtn = page.locator("#fetch-candles-btn");
    await expect(fetchBtn).toBeVisible();
    await fetchBtn.click();

    // Verify Dataset Provenance card
    await expect(page.getByText(/Dataset Provenance \(SHA-256 Digest\)/i)).toBeVisible();
    await expect(page.getByText("PROVIDER_UPSTOX_V3")).toBeVisible();
    await expect(page.getByText("Complete Series")).toBeVisible();
    await expect(page.getByText("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")).toBeVisible();

    // Verify Completed Candles Table
    await expect(page.getByText("25,200.00")).toBeVisible();
    await expect(page.getByText("25,275.00")).toBeVisible();
    await expect(page.getByText("+0.16%")).toBeVisible();
    await expect(page.getByText("Completed").first()).toBeVisible();
  });

  test("verifies responsive mobile layout with no horizontal overflow", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 667 });
    await loginAs(page, "editor");
    await page.goto("/market-data-lab");

    await expect(page.locator("h1")).toBeVisible();

    const bodyWidth = await page.evaluate(() => document.body.scrollWidth);
    const windowWidth = await page.evaluate(() => window.innerWidth);
    expect(bodyWidth).toBeLessThanOrEqual(windowWidth + 5);
  });
});
