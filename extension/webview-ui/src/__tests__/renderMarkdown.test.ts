import { describe, it, expect } from "vitest";
import { renderMarkdown } from "../components/chat/MessageBubble";

// ============================================================
// renderMarkdown tests
// ============================================================

describe("renderMarkdown", () => {
  it("escapes HTML entities", () => {
    expect(renderMarkdown("<script>alert(1)</script>")).toContain("&lt;script&gt;");
    expect(renderMarkdown("<script>alert(1)</script>")).not.toContain("<script>");
  });

  it("renders bold text", () => {
    expect(renderMarkdown("This is **bold** text")).toContain("<strong>bold</strong>");
  });

  it("renders italic text", () => {
    expect(renderMarkdown("This is *italic* text")).toContain("<em>italic</em>");
  });

  it("renders inline code", () => {
    const result = renderMarkdown("Use `foo()` here");
    expect(result).toContain('<code class="inline-code">foo()</code>');
  });

  it("renders newlines as <br />", () => {
    expect(renderMarkdown("line1\nline2")).toContain("line1<br />line2");
  });

  it("handles combined formatting", () => {
    const result = renderMarkdown("**bold** and *italic* and `code`");
    expect(result).toContain("<strong>bold</strong>");
    expect(result).toContain("<em>italic</em>");
    expect(result).toContain('<code class="inline-code">code</code>');
  });

  it("returns empty string for empty input", () => {
    expect(renderMarkdown("")).toBe("");
  });

  it("escapes HTML inside bold/italic", () => {
    const result = renderMarkdown("**<b>xss</b>**");
    expect(result).not.toContain("<b>");
    expect(result).toContain("<strong>");
  });
});
