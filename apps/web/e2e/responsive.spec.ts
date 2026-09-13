import { test, expect } from "@playwright/test";
import { loginAs } from "./helpers/auth";
import { VIEWPORTS, assertNoDocumentOverflow } from "./helpers/viewport";

test.describe("Multi-Viewport Responsive Verification E2E", () => {
  const primaryRoutes = [
    { path: "/", name: "Dashboard / Home", auth: true },
    { path: "/login", name: "Login Page", auth: false },
    { path: "/builder", name: "Strategy Builder", auth: true },
    { path: "/indicator-lab", name: "Indicator Lab", auth: true },
    { path: "/rule-lab", name: "Rule Lab", auth: true },
    { path: "/multi-series-lab", name: "Multi-Series Lab", auth: true },
    { path: "/historical-replay-lab", name: "Historical Replay Lab", auth: true },
    { path: "/inspection-history", name: "Inspection History", auth: true },
    { path: "/replay-comparison-lab", name: "Replay Comparison Lab", auth: true },
    { path: "/data-quality-lab", name: "Dataset Quality Lab", auth: true },
    { path: "/paper-trading-lab", name: "Paper Trading Runtime & OMS Lab", auth: true },
  ];

  for (const viewport of Object.values(VIEWPORTS)) {
    test.describe(`Viewport: ${viewport.name}`, () => {
      test.use({ viewport: { width: viewport.width, height: viewport.height } });

      for (const route of primaryRoutes) {
        test(`Assert zero document horizontal overflow on ${route.name} (${route.path})`, async ({ page }) => {
          if (route.auth) {
            await loginAs(page, "editor");
          }
          await page.goto(route.path);
          await page.waitForLoadState("domcontentloaded");
          await page.waitForTimeout(300);

          // Assert document scrollWidth <= clientWidth
          await assertNoDocumentOverflow(page, `${route.name} @ ${viewport.name}`);

          // Assert authentication badge / header or main content remains present
          await expect(page.locator("header, [aria-label*='User profile'], a[href*='login'], form, #main-content").first()).toBeVisible();
        });
      }
    });
  }
});
