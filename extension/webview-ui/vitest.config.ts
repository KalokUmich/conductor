import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./webview-ui/src/__tests__/setup.ts"],
    include: ["webview-ui/src/**/*.test.{ts,tsx}"],
    css: false,
  },
});
