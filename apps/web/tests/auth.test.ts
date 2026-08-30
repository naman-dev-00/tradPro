import { describe, it, expect } from "vitest";

function sanitizeReturnUrl(url: string | null): string {
  if (!url) return "/";
  if (url.startsWith("/") && !url.startsWith("//") && !url.startsWith("/\\") && !url.includes("://")) {
    return url;
  }
  return "/";
}

describe("Authentication Client & Return URL Sanitization", () => {
  it("allows safe relative return paths", () => {
    expect(sanitizeReturnUrl("/builder")).toBe("/builder");
    expect(sanitizeReturnUrl("/historical-replay-lab?page=2")).toBe("/historical-replay-lab?page=2");
    expect(sanitizeReturnUrl("/data-quality-lab")).toBe("/data-quality-lab");
    expect(sanitizeReturnUrl("/replay-comparison-lab?baseline=1&comparison=2")).toBe("/replay-comparison-lab?baseline=1&comparison=2");
  });

  it("sanitizes open-redirect and protocol-relative attempts to root path", () => {
    expect(sanitizeReturnUrl("https://evil.com/phishing")).toBe("/");
    expect(sanitizeReturnUrl("http://attacker.com")).toBe("/");
    expect(sanitizeReturnUrl("//evil.com/fake-login")).toBe("/");
    expect(sanitizeReturnUrl("/\\evil.com")).toBe("/");
    expect(sanitizeReturnUrl("javascript:alert(1)")).toBe("/");
    expect(sanitizeReturnUrl(null)).toBe("/");
    expect(sanitizeReturnUrl("")).toBe("/");
  });
});
