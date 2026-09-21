import { test, expect } from "@playwright/test";
import { loginAs } from "./helpers/auth";
import { checkA11y } from "./helpers/a11y";

test.describe("Accessibility (Axe-Core & Behavioral) E2E", () => {
  const routesToAudit = [
    { path: "/", title: "Dashboard / Home", role: "editor" },
    { path: "/login", title: "Login Page", role: null },
    { path: "/builder", title: "Strategy Builder", role: "editor" },
    { path: "/indicator-lab", title: "Indicator Lab", role: "editor" },
    { path: "/rule-lab", title: "Rule Lab", role: "editor" },
    { path: "/multi-series-lab", title: "Multi-Series Lab", role: "editor" },
    { path: "/historical-replay-lab", title: "Historical Replay Lab", role: "editor" },
    { path: "/inspection-history", title: "Inspection History", role: "editor" },
    { path: "/replay-comparison-lab", title: "Replay Comparison Lab", role: "editor" },
    { path: "/data-quality-lab", title: "Dataset Quality Lab", role: "editor" },
    { path: "/paper-trading-lab", title: "Paper Trading Runtime & OMS Lab", role: "editor" },
    { path: "/_not_found_test_route", title: "404 Not Found Page", role: null },
  ];

  for (const route of routesToAudit) {
    test(`Axe-core WCAG 2.1 AA scan: ${route.title} (${route.path})`, async ({ page }) => {
      if (route.role) {
        await loginAs(page, route.role as any);
      }
      await page.goto(route.path);
      await page.waitForLoadState("domcontentloaded");
      await page.waitForTimeout(500); // Allow react state rendering to settle

      // Run full automated WCAG 2.1 AA audit
      await checkA11y(page, route.title);
    });
  }

  test("Skip navigation link becomes visible on Tab and targets #main-content", async ({ page }) => {
    await page.goto("/login");
    await page.keyboard.press("Tab");

    const skipLink = page.locator('a[href="#main-content"]');
    await expect(skipLink).toBeVisible();

    await page.keyboard.press("Enter");
    // Verify focus shifts to #main-content
    const isMainFocused = await page.evaluate(() => document.activeElement?.id === "main-content");
    expect(isMainFocused).toBeTruthy();
  });

  test("Keyboard-only login flow operates without mouse", async ({ page }) => {
    await page.goto("/login");
    await page.getByRole("textbox", { name: "Username or Email" }).waitFor({ state: "visible" });

    // Tab into username
    await page.keyboard.press("Tab"); // Skip link
    await page.keyboard.press("Tab"); // Username or Email

    await page.keyboard.type("e2e_editor");
    await page.keyboard.press("Tab"); // Password
    await page.keyboard.type("SomePassword123!");
    await page.keyboard.press("Enter"); // Submit form

    // Form attempts submission and displays alert
    await expect(page.locator('#login-error-alert, [role="alert"]:not(#__next-route-announcer__)').first()).toBeVisible({ timeout: 5000 });
  });

  test("Status badges communicate meaning through text and icons, not color alone", async ({ page }) => {
    await loginAs(page, "editor");
    await page.goto("/data-quality-lab");

    // Verify presence of text status labels
    await expect(page.locator("text=/PASS|WARN|FAIL/").first()).toBeVisible({ timeout: 10000 });
  });
});
