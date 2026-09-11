import fs from "fs";
import path from "path";
import { Page, expect } from "@playwright/test";

export interface TestUser {
  id: string;
  username: string;
  email: string;
  password: string;
  role: "ADMIN" | "EDITOR" | "VIEWER";
  is_active: boolean;
}

export interface E2EManifest {
  users: {
    admin: TestUser;
    editor: TestUser;
    viewer: TestUser;
    other_editor: TestUser;
    disabled_user: TestUser;
  };
  strategies: {
    starter?: {
      id: string;
      owner_id: string;
      name: string;
    };
  };
}

let cachedManifest: E2EManifest | null = null;

export function getTestManifest(): E2EManifest {
  if (cachedManifest) return cachedManifest;

  const manifestPath = process.env.E2E_MANIFEST_PATH;
  if (manifestPath && fs.existsSync(manifestPath)) {
    const raw = fs.readFileSync(manifestPath, "utf-8");
    cachedManifest = JSON.parse(raw);
    return cachedManifest!;
  }

  // Look in temp directory if manifest path isn't directly set
  const tempDir = process.env.E2E_TEMP_DIR;
  if (tempDir) {
    const candidate = path.join(tempDir, "e2e_manifest.json");
    if (fs.existsSync(candidate)) {
      cachedManifest = JSON.parse(fs.readFileSync(candidate, "utf-8"));
      return cachedManifest!;
    }
  }

  throw new Error("E2E credential manifest not found. Ensure E2E database has been seeded with E2E_MANIFEST_PATH set.");
}

const storageStateDir = path.join(process.cwd(), "test-results", ".auth");

export async function loginAs(page: Page, roleKey: "admin" | "editor" | "viewer" | "other_editor"): Promise<TestUser> {
  const manifest = getTestManifest();
  const testUser = manifest.users[roleKey];
  if (!testUser) {
    throw new Error(`Test user roleKey '${roleKey}' not found in manifest.`);
  }

  const authFile = path.join(storageStateDir, `${roleKey}.json`);
  if (fs.existsSync(authFile)) {
    const context = page.context();
    const state = JSON.parse(fs.readFileSync(authFile, "utf-8"));
    if (state.cookies && state.cookies.length > 0) {
      await context.addCookies(state.cookies);
      return testUser;
    }
  }

  await page.goto("/login");
  await page.waitForSelector("#usernameOrEmail", { state: "visible" });

  await page.fill("#usernameOrEmail", testUser.username);
  await page.fill("#password", testUser.password);
  await page.click('button[type="submit"]');

  // Verify redirection to home or requested returnUrl
  await page.waitForURL((url) => !url.pathname.includes("/login"), { timeout: 10000 });
  await expect(page.locator("text=" + testUser.username)).toBeVisible({ timeout: 10000 });

  // Persist storage state to avoid rate limit exhaustion on serial test runs
  try {
    fs.mkdirSync(storageStateDir, { recursive: true });
    await page.context().storageState({ path: authFile });
  } catch {
    // Ignore storage state saving errors
  }

  return testUser;
}

export async function logout(page: Page): Promise<void> {
  const signOutBtn = page.getByRole("button", { name: /sign out/i });
  if (await signOutBtn.isVisible()) {
    await signOutBtn.click();
    await expect(page.getByRole("link", { name: /sign in/i })).toBeVisible({ timeout: 10000 });
  }
  if (fs.existsSync(storageStateDir)) {
    try {
      fs.rmSync(storageStateDir, { recursive: true, force: true });
    } catch {
      // Ignore cleanup error
    }
  }
}
