import { memo, useCallback, useEffect, useRef, useState } from "react";

// ============================================================
// CodeBlock — syntax highlighting, copy button, language badge
// Feels native to VS Code with the dark theme.
// ============================================================

interface CodeBlockProps {
  code: string;
  language?: string;
}

export const CodeBlock = memo(function CodeBlock({ code, language }: CodeBlockProps) {
  const codeRef = useRef<HTMLElement>(null);
  const [copied, setCopied] = useState(false);

  // Highlight.js is loaded globally from bundled file
  useEffect(() => {
    const el = codeRef.current;
    if (!el) return;
    const hljs = (window as unknown as Record<string, unknown>).hljs as {
      highlightElement: (el: HTMLElement) => void;
    } | undefined;
    if (hljs?.highlightElement) {
      hljs.highlightElement(el);
    }
  }, [code, language]);

  const handleCopy = useCallback(() => {
    navigator.clipboard.writeText(code).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }, [code]);

  return (
    <div className="code-block">
      {language && (
        <div className="code-block-header">
          <span>{language}</span>
        </div>
      )}
      <button
        className="copy-btn"
        onClick={handleCopy}
        aria-label="Copy code"
      >
        {copied ? "Copied!" : "Copy"}
      </button>
      <pre>
        <code
          ref={codeRef}
          className={language ? `language-${language}` : ""}
        >
          {code}
        </code>
      </pre>
    </div>
  );
});
