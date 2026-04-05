import "@testing-library/jest-dom/vitest";

// Mock VS Code WebView API
const vscodeApi = {
  postMessage: vi.fn(),
  getState: vi.fn(() => ({})),
  setState: vi.fn(),
};

// @ts-expect-error — mock for WebView environment
window.acquireVsCodeApi = vi.fn(() => vscodeApi);

// Mock Highlight.js
// @ts-expect-error — mock for WebView environment
window.hljs = {
  highlightElement: vi.fn(),
  highlight: vi.fn(() => ({ value: "" })),
};

// Mock clipboard
Object.assign(navigator, {
  clipboard: {
    writeText: vi.fn(() => Promise.resolve()),
    readText: vi.fn(() => Promise.resolve("")),
  },
});

// Expose mock for test assertions
export { vscodeApi };
