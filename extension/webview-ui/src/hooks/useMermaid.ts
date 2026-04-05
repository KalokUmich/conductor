import { useCallback, useEffect } from "react";

// ============================================================
// useMermaid — render mermaid diagrams in messages
// Mermaid is loaded via CDN in the HTML head.
// ============================================================

declare global {
  interface Window {
    mermaid?: {
      initialize: (config: Record<string, unknown>) => void;
      render: (id: string, code: string) => Promise<{ svg: string }>;
    };
  }
}

let mermaidInitialized = false;

function initMermaid() {
  if (mermaidInitialized || !window.mermaid) return;
  window.mermaid.initialize({
    startOnLoad: false,
    theme: "dark",
    themeVariables: {
      darkMode: true,
      background: "#1a1a1a",
      primaryColor: "#7c3aed",
      primaryTextColor: "#f0ece4",
      lineColor: "#57534e",
    },
  });
  mermaidInitialized = true;
}

let mermaidCounter = 0;

export function useMermaid() {
  useEffect(() => {
    initMermaid();
  }, []);

  /** Render all .mermaid-source elements within a container */
  const renderMermaidInContainer = useCallback(
    async (container: HTMLElement | null) => {
      if (!container || !window.mermaid) return;

      const elements = container.querySelectorAll<HTMLElement>(".mermaid-source");
      for (const el of elements) {
        if (el.dataset.rendered === "true") continue;

        const code = el.textContent || "";
        if (!code.trim()) continue;

        const id = `mermaid-${++mermaidCounter}`;
        try {
          const { svg } = await window.mermaid.render(id, code);
          el.innerHTML = svg;
          el.dataset.rendered = "true";
          el.classList.remove("mermaid-source");
          el.classList.add("mermaid-rendered");
        } catch {
          // Fallback: show raw source in a code block
          el.innerHTML = `<pre class="mermaid-fallback"><code>${code
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")}</code></pre>`;
          el.dataset.rendered = "true";
        }
      }
    },
    []
  );

  return { renderMermaidInContainer };
}
