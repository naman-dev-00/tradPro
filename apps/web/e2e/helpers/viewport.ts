import { Page, expect } from "@playwright/test";

export const VIEWPORTS = {
  ultraNarrow: { width: 310, height: 640, name: "310x640 (Ultra-Narrow Mobile)" },
  compactMobile: { width: 320, height: 720, name: "320x720 (Compact Mobile)" },
  mobile: { width: 375, height: 812, name: "375x812 (Standard iPhone)" },
  tablet: { width: 768, height: 1024, name: "768x1024 (Tablet Portrait)" },
  desktop: { width: 1280, height: 800, name: "1280x800 (Desktop Baseline)" },
};

/**
 * Asserts that the document does not have horizontal overflow.
 * scrollWidth must be <= clientWidth (allowing 1px subpixel rounding tolerance).
 */
export async function assertNoDocumentOverflow(page: Page, contextName: string) {
  const { scrollWidth, clientWidth } = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));

  expect(
    scrollWidth,
    `Horizontal document overflow detected in '${contextName}'. scrollWidth (${scrollWidth}px) exceeds clientWidth (${clientWidth}px).`
  ).toBeLessThanOrEqual(clientWidth + 1);
}
