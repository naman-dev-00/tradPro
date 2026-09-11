import { Page, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

/**
 * Executes an automated WCAG 2.1 Level AA accessibility scan using axe-core.
 * Asserts 0 violations.
 */
export async function checkA11y(
  page: Page,
  contextName: string,
  options?: {
    disableRules?: string[];
    excludeSelectors?: string[];
  }
) {
  let builder = new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]);

  if (options?.disableRules && options.disableRules.length > 0) {
    builder = builder.disableRules(options.disableRules);
  }

  if (options?.excludeSelectors) {
    for (const sel of options.excludeSelectors) {
      builder = builder.exclude(sel);
    }
  }

  const results = await builder.analyze();

  if (results.violations.length > 0) {
    const violationSummary = results.violations
      .map(
        (v) =>
          `[${v.id}] ${v.help} (${v.impact} impact):\n  Nodes:\n` +
          v.nodes.map((n) => `    - ${n.html}\n      ${n.failureSummary}`).join("\n")
      )
      .join("\n\n");

    expect(
      results.violations,
      `Accessibility violations found in '${contextName}':\n${violationSummary}`
    ).toEqual([]);
  }

  expect(results.violations.length).toBe(0);
}
