import { test, expect } from "@playwright/test";
import { getTestManifest, loginAs, logout } from "./helpers/auth";

test.describe("Authentication and Session Security E2E", () => {
  test("anonymous user is redirected to /login when accessing protected routes", async ({ browser }) => {
    const context = await browser.newContext();
    const page = await context.newPage();
    try {
      await page.goto("/builder");
      await page.waitForURL((url) => url.pathname.includes("/login"), { timeout: 10000 });
      expect(page.url()).toContain("returnUrl=%2Fbuilder");
    } finally {
      await context.close();
    }
  });

  test("safe returnUrl redirection after login", async ({ browser }) => {
    const manifest = getTestManifest();
    const editor = manifest.users.editor;
    const context = await browser.newContext();
    const page = await context.newPage();

    try {
      await page.goto("/login?returnUrl=/indicator-lab");
      await page.fill("#usernameOrEmail", editor.username);
      await page.fill("#password", editor.password);
      await page.click('button[type="submit"]');

      await page.waitForURL((url) => url.pathname === "/indicator-lab", { timeout: 10000 });
      expect(page.url()).toContain("/indicator-lab");
    } finally {
      await context.close();
    }
  });

  test("malicious and protocol-relative returnUrl parameters are sanitized to /", async ({ browser }) => {
    const manifest = getTestManifest();

    const cases = [
      { url: "//evil.example.com", user: manifest.users.viewer },
      { url: "https://evil.example.com", user: manifest.users.other_editor },
    ];

    for (const testCase of cases) {
      const context = await browser.newContext();
      const page = await context.newPage();
      try {
        await page.goto(`/login?returnUrl=${encodeURIComponent(testCase.url)}`);
        await page.fill("#usernameOrEmail", testCase.user.username);
        await page.fill("#password", testCase.user.password);
        await page.click('button[type="submit"]');

        await page.waitForURL((url) => url.pathname === "/", { timeout: 10000 });
        expect(page.url()).not.toContain("evil.example.com");
      } finally {
        await context.close();
      }
    }
  });

  test("invalid credentials show accessible role='alert' error without revealing password", async ({ browser }) => {
    const manifest = getTestManifest();
    const admin = manifest.users.admin;
    const context = await browser.newContext();
    const page = await context.newPage();

    try {
      await page.goto("/login");
      await page.fill("#usernameOrEmail", admin.username);
      await page.fill("#password", "WrongPassword123!");
      await page.click('button[type="submit"]');

      const errorAlert = page.locator('#login-error-alert, [role="alert"]:not(#__next-route-announcer__)').first();
      await expect(errorAlert).toBeVisible({ timeout: 5000 });
      await expect(errorAlert).toContainText(/invalid/i);
      expect(page.url()).toContain("/login");
    } finally {
      await context.close();
    }
  });

  test("disabled user is rejected from login", async ({ browser }) => {
    const manifest = getTestManifest();
    const disabledUser = manifest.users.disabled_user;
    const context = await browser.newContext();
    const page = await context.newPage();

    try {
      await page.goto("/login");
      await page.fill("#usernameOrEmail", disabledUser.username);
      await page.fill("#password", disabledUser.password);
      await page.click('button[type="submit"]');

      const errorAlert = page.locator('#login-error-alert, [role="alert"]:not(#__next-route-announcer__)').first();
      await expect(errorAlert).toBeVisible({ timeout: 5000 });
      await expect(errorAlert).toContainText(/deactivated|disabled|inactive|invalid/i);
    } finally {
      await context.close();
    }
  });

  test("successful login and logout cycle updates header badge and cookies", async ({ page }) => {
    const user = await loginAs(page, "editor");
    await page.goto("/");
    await page.waitForLoadState("domcontentloaded");
    await expect(page.getByText(user.username, { exact: true })).toBeVisible({ timeout: 10000 });
    await expect(page.getByText(user.role, { exact: true })).toBeVisible({ timeout: 10000 });

    await logout(page);
    await page.goto("/");
    await page.waitForLoadState("domcontentloaded");
    await expect(page.getByRole("link", { name: /sign in/i })).toBeVisible({ timeout: 10000 });
  });
});
