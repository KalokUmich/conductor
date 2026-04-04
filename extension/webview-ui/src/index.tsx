import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { VSCodeProvider } from "./contexts/VSCodeContext";
import { SessionProvider } from "./contexts/SessionContext";
import { App } from "./components/App";
import { ToastContainer } from "./components/shared/Toast";

// Styles
import "./styles/design-tokens.css";
import "./styles/components.css";

// ============================================================
// React WebView Entry Point
// ============================================================

const container = document.getElementById("root");
if (!container) {
  throw new Error("Root element not found");
}

const root = createRoot(container);

root.render(
  <StrictMode>
    <VSCodeProvider>
      <SessionProvider>
        <App />
        <ToastContainer />
      </SessionProvider>
    </VSCodeProvider>
  </StrictMode>
);
