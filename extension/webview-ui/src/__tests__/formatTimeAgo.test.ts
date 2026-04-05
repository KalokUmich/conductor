import { describe, it, expect } from "vitest";
import { formatTimeAgo } from "../components/panels/StatePanels";

// ============================================================
// formatTimeAgo tests
// ============================================================

describe("formatTimeAgo", () => {
  it('returns "just now" for recent dates', () => {
    expect(formatTimeAgo(new Date(Date.now() - 5_000))).toBe("just now");
  });

  it("returns minutes ago", () => {
    expect(formatTimeAgo(new Date(Date.now() - 3 * 60_000))).toBe("3m ago");
  });

  it("returns hours ago", () => {
    expect(formatTimeAgo(new Date(Date.now() - 2 * 3600_000))).toBe("2h ago");
  });

  it('returns "yesterday" for 1 day ago', () => {
    expect(formatTimeAgo(new Date(Date.now() - 25 * 3600_000))).toBe("yesterday");
  });

  it("returns days ago for 2-6 days", () => {
    expect(formatTimeAgo(new Date(Date.now() - 3 * 86400_000))).toBe("3d ago");
  });

  it("returns formatted date for 7+ days", () => {
    const oldDate = new Date(Date.now() - 10 * 86400_000);
    const result = formatTimeAgo(oldDate);
    // Should be a date string, not "Xd ago"
    expect(result).not.toContain("d ago");
    expect(result).toMatch(/\d/); // contains digits (date)
  });
});
