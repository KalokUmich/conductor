import { type ReactNode } from "react";
import { render, type RenderOptions } from "@testing-library/react";

/**
 * Minimal wrapper that provides the VSCode API mock.
 * Components that need SessionContext or ChatContext will need
 * additional wrapping — add as needed per test file.
 */
export function renderWithProviders(
  ui: ReactNode,
  options?: Omit<RenderOptions, "wrapper">
) {
  return render(ui, { ...options });
}

/** Get the mock postMessage function from setup.ts */
export function getPostMessage(): ReturnType<typeof vi.fn> {
  const api = window.acquireVsCodeApi();
  return api.postMessage as ReturnType<typeof vi.fn>;
}
