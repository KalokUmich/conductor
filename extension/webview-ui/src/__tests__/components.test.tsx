import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

// ============================================================
// Component behavior tests (lightweight, no full provider trees)
// ============================================================

// Since our components depend heavily on contexts (SessionContext, ChatContext, VSCodeContext),
// full integration tests would need complex provider setups. Instead, we test:
// 1. Pure rendering logic (via extracted functions — already tested in other files)
// 2. Key interaction patterns using minimal component slices

describe("DiagramLightbox", () => {
  // Lazy import to avoid context issues at module level
  let DiagramLightbox: typeof import("../components/shared/DiagramLightbox").DiagramLightbox;

  beforeEach(async () => {
    const mod = await import("../components/shared/DiagramLightbox");
    DiagramLightbox = mod.DiagramLightbox;
  });

  it("renders SVG content", () => {
    const onClose = vi.fn();
    render(<DiagramLightbox svgHtml='<svg data-testid="test-svg"><circle r="10"/></svg>' onClose={onClose} />);
    expect(document.querySelector(".diagram-lightbox")).toBeTruthy();
    expect(document.querySelector(".diagram-lightbox-content")?.innerHTML).toContain("<circle");
  });

  it("closes on backdrop click", () => {
    const onClose = vi.fn();
    render(<DiagramLightbox svgHtml="<svg/>" onClose={onClose} />);
    fireEvent.click(document.querySelector(".diagram-lightbox")!);
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("closes on Escape key", () => {
    const onClose = vi.fn();
    render(<DiagramLightbox svgHtml="<svg/>" onClose={onClose} />);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("close button works", () => {
    const onClose = vi.fn();
    render(<DiagramLightbox svgHtml="<svg/>" onClose={onClose} />);
    fireEvent.click(document.querySelector(".diagram-lightbox-close")!);
    expect(onClose).toHaveBeenCalledOnce();
  });
});

describe("Modal", () => {
  let Modal: typeof import("../components/shared/Modal").Modal;

  beforeEach(async () => {
    const mod = await import("../components/shared/Modal");
    Modal = mod.Modal;
  });

  it("renders when open", () => {
    render(
      <Modal open={true} onClose={() => {}} title="Test Modal">
        <p>Content</p>
      </Modal>
    );
    expect(screen.getByText("Test Modal")).toBeTruthy();
    expect(screen.getByText("Content")).toBeTruthy();
  });

  it("does not render when closed", () => {
    render(
      <Modal open={false} onClose={() => {}} title="Test Modal">
        <p>Content</p>
      </Modal>
    );
    expect(screen.queryByText("Test Modal")).toBeNull();
  });

  it("closes on Escape", () => {
    const onClose = vi.fn();
    render(
      <Modal open={true} onClose={onClose} title="Test">
        <p>Body</p>
      </Modal>
    );
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("closes on overlay click", () => {
    const onClose = vi.fn();
    render(
      <Modal open={true} onClose={onClose} title="Test">
        <p>Body</p>
      </Modal>
    );
    fireEvent.click(document.querySelector(".modal-overlay")!);
    expect(onClose).toHaveBeenCalledOnce();
  });
});

describe("RebuildIndexModal", () => {
  let RebuildIndexModal: typeof import("../components/modals/RebuildIndexModal").RebuildIndexModal;

  beforeEach(async () => {
    const mod = await import("../components/modals/RebuildIndexModal");
    RebuildIndexModal = mod.RebuildIndexModal;
  });

  it("shows warning text and buttons", () => {
    render(<RebuildIndexModal open={true} onClose={() => {}} onConfirm={() => {}} />);
    expect(screen.getByText(/delete all cached embeddings/i)).toBeTruthy();
    expect(screen.getByText("Cancel")).toBeTruthy();
    expect(screen.getByText("Rebuild")).toBeTruthy();
  });

  it("calls onConfirm when Rebuild clicked", () => {
    const onConfirm = vi.fn();
    render(<RebuildIndexModal open={true} onClose={() => {}} onConfirm={onConfirm} />);
    fireEvent.click(screen.getByText("Rebuild"));
    expect(onConfirm).toHaveBeenCalledOnce();
  });
});

describe("CodeBlock", () => {
  let CodeBlock: typeof import("../components/shared/CodeBlock").CodeBlock;

  beforeEach(async () => {
    const mod = await import("../components/shared/CodeBlock");
    CodeBlock = mod.CodeBlock;
  });

  it("renders code content", () => {
    render(<CodeBlock code='console.log("hello")' language="javascript" />);
    expect(screen.getByText('console.log("hello")')).toBeTruthy();
  });

  it("has a copy button", () => {
    render(<CodeBlock code="x = 1" language="python" />);
    const copyBtn = document.querySelector(".copy-btn");
    expect(copyBtn).toBeTruthy();
  });

  it("copies code to clipboard on button click", async () => {
    render(<CodeBlock code="test code" language="" />);
    const copyBtn = document.querySelector(".copy-btn")!;
    fireEvent.click(copyBtn);
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith("test code");
  });
});

describe("Toast", () => {
  it("renders toast when showToast is called with ToastContainer mounted", async () => {
    const { showToast, ToastContainer } = await import("../components/shared/Toast");
    const { container } = render(<ToastContainer />);
    showToast("Test message", "success");
    // Wait for React re-render
    await new Promise((r) => setTimeout(r, 50));
    const toasts = container.querySelectorAll(".toast");
    expect(toasts.length).toBeGreaterThan(0);
    expect(toasts[0].textContent).toBe("Test message");
  });
});
