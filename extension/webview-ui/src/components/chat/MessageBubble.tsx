import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSession } from "../../contexts/SessionContext";
import { useVSCode } from "../../contexts/VSCodeContext";
import type { ChatMessage } from "../../types/messages";
import { escapeHtml, formatTime, getInitials, getAvatarColor } from "../../utils/format";
import { CodeBlock } from "../shared/CodeBlock";

// ============================================================
// MessageBubble — renders a single chat message
// ============================================================

interface MessageBubbleProps {
  message: ChatMessage;
  isGrouped: boolean;
}

export const MessageBubble = memo(function MessageBubble({
  message,
  isGrouped,
}: MessageBubbleProps) {
  const { state } = useSession();
  const isOwn = message.userId === state.session?.userId;

  switch (message.type) {
    case "system":
      return <SystemMessage text={message.content} />;
    case "code_snippet":
      return (
        <CodeSnippetMessage message={message} isOwn={isOwn} isGrouped={isGrouped} />
      );
    case "ai_answer":
    case "ai_explanation":
    case "ai_summary":
    case "ai_code_prompt":
      return <AIMessage message={message} isGrouped={isGrouped} />;
    case "file":
      return <FileMessage message={message} isOwn={isOwn} isGrouped={isGrouped} />;
    case "test_failures":
      return <TestFailuresMessage message={message} isOwn={isOwn} isGrouped={isGrouped} />;
    case "stack_trace":
      return <StackTraceMessage message={message} isOwn={isOwn} isGrouped={isGrouped} />;
    default:
      return <TextMessage message={message} isOwn={isOwn} isGrouped={isGrouped} />;
  }
});

// ── Text Message ──────────────────────────────────────────

interface TextMessageProps {
  message: ChatMessage;
  isOwn: boolean;
  isGrouped: boolean;
}

function TextMessage({ message, isOwn, isGrouped }: TextMessageProps) {
  return (
    <div className={`message-row ${isOwn ? "message-own" : "message-other"} ${isGrouped ? "message-grouped" : ""}`}>
      {!isOwn && (
        <Avatar
          name={message.displayName}
          colorIndex={message.userId?.charCodeAt(0) || 0}
          hidden={isGrouped}
        />
      )}
      <div className="message-content-wrapper">
        {!isOwn && !isGrouped && (
          <MessageHeader
            name={message.displayName}
            role={message.role}
            identitySource={message.identitySource}
          />
        )}
        <div className={`message-bubble ${isOwn ? "bubble-own" : "bubble-other"}`}>
          <p className="message-text">{message.content}</p>
        </div>
        <MessageMeta ts={message.ts} isOwn={isOwn} />
      </div>
    </div>
  );
}

// ── AI Message ────────────────────────────────────────────

function AIMessage({ message, isGrouped }: { message: ChatMessage; isGrouped: boolean }) {
  const content = message.answer || message.summary || message.codePrompt || message.content || "";

  return (
    <div className={`message-row message-other ${isGrouped ? "message-grouped" : ""}`}>
      <Avatar name="AI" colorIndex={-1} hidden={isGrouped} isAI />
      <div className="message-content-wrapper" style={{ maxWidth: "92%" }}>
        {!isGrouped && (
          <MessageHeader name="Brain" role="ai" />
        )}
        <div className="message-bubble bubble-ai">
          <AIContent content={content} />
        </div>
        <MessageMeta ts={message.ts} isOwn={false} />
      </div>
    </div>
  );
}

/** Render AI content with markdown-like formatting + mermaid */
function AIContent({ content }: { content: string }) {
  const containerRef = useRef<HTMLDivElement>(null);

  // Split by code blocks
  const parts = content.split(/(```[\s\S]*?```)/g);

  // After render, try to render mermaid diagrams
  useEffect(() => {
    const el = containerRef.current;
    if (!el || !window.mermaid) return;
    const mermaidEls = el.querySelectorAll<HTMLElement>(".mermaid-source");
    let counter = 0;
    mermaidEls.forEach(async (mel) => {
      if (mel.dataset.rendered === "true") return;
      const code = mel.textContent || "";
      const id = `mermaid-ai-${Date.now()}-${counter++}`;
      try {
        const { svg } = await window.mermaid!.render(id, code);
        mel.innerHTML = svg;
        mel.dataset.rendered = "true";
      } catch {
        // keep raw source
      }
    });
  }, [content]);

  return (
    <div className="ai-content" ref={containerRef}>
      {parts.map((part, i) => {
        if (part.startsWith("```")) {
          const match = part.match(/^```(\w*)\n?([\s\S]*?)```$/);
          if (match) {
            const lang = match[1].toLowerCase();
            const code = match[2].trim();
            // Mermaid diagram
            if (lang === "mermaid") {
              return (
                <div key={i} className="mermaid-source" data-rendered="false">
                  {code}
                </div>
              );
            }
            return <CodeBlock key={i} code={code} language={match[1] || ""} />;
          }
        }
        if (!part.trim()) return null;
        return (
          <div
            key={i}
            className="message-text ai-text"
            dangerouslySetInnerHTML={{ __html: renderMarkdown(part) }}
          />
        );
      })}
    </div>
  );
}



/** Simple markdown → HTML renderer (bold, italic, inline code, links) */
function renderMarkdown(text: string): string {
  return escapeHtml(text)
    .replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>")
    .replace(/\*(.*?)\*/g, "<em>$1</em>")
    .replace(/`([^`]+)`/g, '<code class="inline-code">$1</code>')
    .replace(/\n/g, "<br />");
}

// ── Code Snippet Message ──────────────────────────────────
// Single block: clickable header bar (navigate + Explain with AI)
// Code preview: collapsed to 5 lines by default, expandable

const SNIPPET_PREVIEW_LINES = 5;

function CodeSnippetMessage({
  message,
  isOwn,
  isGrouped,
}: TextMessageProps) {
  const snippet = message.codeSnippet || message.metadata as ChatMessage["codeSnippet"];
  const { send } = useVSCode();
  const { state: sessionState } = useSession();
  const [expanded, setExpanded] = useState(false);
  const [showExplainInput, setShowExplainInput] = useState(false);
  const [explainQuestion, setExplainQuestion] = useState("");

  const handleNavigate = useCallback(() => {
    if (!snippet?.relativePath) return;
    send({
      command: "navigateToCode",
      relativePath: snippet.relativePath,
      startLine: snippet.startLine,
      endLine: snippet.endLine,
    });
  }, [send, snippet]);

  const handleExplain = useCallback(() => {
    if (!snippet) return;
    const query = explainQuestion.trim() || "Explain this code";
    send({
      command: "askAI",
      roomId: sessionState.session?.roomId || "",
      query,
      codeContext: {
        code: snippet.code,
        relativePath: snippet.relativePath || snippet.filename || "",
        startLine: snippet.startLine,
        endLine: snippet.endLine,
        language: snippet.language || "",
      },
    } as never);
    setShowExplainInput(false);
    setExplainQuestion("");
  }, [send, snippet, explainQuestion]);

  if (!snippet) return null;

  const codeLines = snippet.code.split("\n");
  const needsCollapse = codeLines.length > SNIPPET_PREVIEW_LINES;
  const displayCode = expanded || !needsCollapse
    ? snippet.code
    : codeLines.slice(0, SNIPPET_PREVIEW_LINES).join("\n");
  const hiddenCount = codeLines.length - SNIPPET_PREVIEW_LINES;
  const lineInfo = `L${snippet.startLine}-${snippet.endLine}`;

  return (
    <div className={`message-row ${isOwn ? "message-own" : "message-other"} ${isGrouped ? "message-grouped" : ""}`}>
      {!isOwn && (
        <Avatar
          name={message.displayName}
          colorIndex={message.userId?.charCodeAt(0) || 0}
          hidden={isGrouped}
        />
      )}
      <div className="message-content-wrapper" style={{ maxWidth: "88%" }}>
        {!isOwn && !isGrouped && (
          <MessageHeader name={message.displayName} role={message.role} />
        )}

        {/* Comment text if any */}
        {message.content && (
          <div className="message-bubble bubble-other" style={{ marginBottom: "2px", borderBottomLeftRadius: "var(--radius-sm)" }}>
            <p className="message-text">{message.content}</p>
          </div>
        )}

        {/* Single code block */}
        <div className="message-bubble bubble-code">
          {/* Header bar: filename + navigate + explain */}
          <div className="snippet-bar">
            <div className="snippet-bar-left" onClick={handleNavigate} title="Go to code location">
              <svg viewBox="0 0 20 20" fill="currentColor" width="14" height="14" style={{ color: "var(--c-info)", flexShrink: 0 }}>
                <path fillRule="evenodd" d="M12.316 3.051a1 1 0 01.633 1.265l-4 12a1 1 0 11-1.898-.632l4-12a1 1 0 011.265-.633zM5.707 6.293a1 1 0 010 1.414L3.414 10l2.293 2.293a1 1 0 11-1.414 1.414l-3-3a1 1 0 010-1.414l3-3a1 1 0 011.414 0zm8.586 0a1 1 0 011.414 0l3 3a1 1 0 010 1.414l-3 3a1 1 0 11-1.414-1.414L16.586 10l-2.293-2.293a1 1 0 010-1.414z" clipRule="evenodd" />
              </svg>
              <span className="snippet-filename">{snippet.relativePath || snippet.filename || "code"}</span>
              <span className="snippet-lines">{lineInfo}</span>
            </div>
            <div className="snippet-bar-right">
              <button
                className="snippet-explain-btn"
                onClick={() => setShowExplainInput(!showExplainInput)}
                title="Explain with AI"
              >
                <svg viewBox="0 0 20 20" fill="currentColor" width="14" height="14">
                  <path d="M11 3a1 1 0 10-2 0v1a1 1 0 102 0V3zM15.657 5.757a1 1 0 00-1.414-1.414l-.707.707a1 1 0 001.414 1.414l.707-.707zM18 10a1 1 0 01-1 1h-1a1 1 0 110-2h1a1 1 0 011 1zM5.05 6.464A1 1 0 106.464 5.05l-.707-.707a1 1 0 00-1.414 1.414l.707.707zM5 10a1 1 0 01-1 1H3a1 1 0 110-2h1a1 1 0 011 1zM8 16v-1h4v1a2 2 0 11-4 0zM12 14c.015-.68.166-1.32.42-1.897A5 5 0 006 10a5 5 0 006 10l-.579 2.894A1 1 0 0012 14z"/>
                </svg>
                <span>Explain</span>
              </button>
            </div>
          </div>

          {/* Explain input (inline, shown on click) */}
          {showExplainInput && (
            <div className="snippet-explain-area animate-slide-down">
              <input
                type="text"
                className="snippet-explain-input"
                value={explainQuestion}
                onChange={(e) => setExplainQuestion(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleExplain()}
                placeholder="Ask about this code... (Enter to send)"
                autoFocus
              />
              <div className="snippet-explain-actions">
                <button className="btn-primary btn-sm" onClick={handleExplain}>Ask AI</button>
                <button className="btn-secondary btn-sm" onClick={() => setShowExplainInput(false)}>Cancel</button>
              </div>
            </div>
          )}

          {/* Code content — collapsed by default */}
          <CodeBlock code={displayCode} language={snippet.language} />

          {/* Expand button */}
          {needsCollapse && !expanded && (
            <button className="snippet-expand-btn" onClick={() => setExpanded(true)}>
              Show {hiddenCount} more line{hiddenCount !== 1 ? "s" : ""} ↓
            </button>
          )}
          {needsCollapse && expanded && (
            <button className="snippet-expand-btn" onClick={() => setExpanded(false)}>
              Collapse ↑
            </button>
          )}
        </div>
        <MessageMeta ts={message.ts} isOwn={isOwn} />
      </div>
    </div>
  );
}



// ── File Message ──────────────────────────────────────────

function FileMessage({ message, isOwn, isGrouped }: TextMessageProps) {
  const { send } = useVSCode();

  const handleDownload = useCallback(() => {
    if (!message.fileId || !message.downloadUrl) return;
    send({
      command: "downloadFile",
      fileId: message.fileId,
      fileName: message.originalFilename || "file",
      downloadUrl: message.downloadUrl,
    });
  }, [send, message]);

  return (
    <div className={`message-row ${isOwn ? "message-own" : "message-other"} ${isGrouped ? "message-grouped" : ""}`}>
      {!isOwn && (
        <Avatar
          name={message.displayName}
          colorIndex={message.userId?.charCodeAt(0) || 0}
          hidden={isGrouped}
        />
      )}
      <div className="message-content-wrapper">
        {!isOwn && !isGrouped && (
          <MessageHeader name={message.displayName} role={message.role} />
        )}
        <div className="message-bubble bubble-file" onClick={handleDownload}>
          <div className="file-icon">
            {getFileIcon(message.mimeType || "")}
          </div>
          <div className="file-info">
            <span className="file-name">{message.originalFilename}</span>
            {message.sizeBytes && (
              <span className="file-size">
                {(message.sizeBytes / 1024).toFixed(1)} KB
              </span>
            )}
          </div>
          <span className="file-download">↓</span>
        </div>
        {message.caption && (
          <div className={`message-bubble ${isOwn ? "bubble-own" : "bubble-other"}`} style={{ marginTop: "2px" }}>
            <p className="message-text">{message.caption}</p>
          </div>
        )}
        <MessageMeta ts={message.ts} isOwn={isOwn} />
      </div>
    </div>
  );
}

function getFileIcon(mimeType: string): string {
  if (mimeType.startsWith("image/")) return "🖼";
  if (mimeType.includes("pdf")) return "📄";
  if (mimeType.includes("zip") || mimeType.includes("tar")) return "📦";
  return "📎";
}

// ── Test Failures Message ─────────────────────────────────

function TestFailuresMessage({ message, isOwn, isGrouped }: TextMessageProps) {
  const { send } = useVSCode();
  const tf = message.testFailures;
  if (!tf) return null;

  return (
    <div className={`message-row ${isOwn ? "message-own" : "message-other"} ${isGrouped ? "message-grouped" : ""}`}>
      {!isOwn && (
        <Avatar
          name={message.displayName}
          colorIndex={message.userId?.charCodeAt(0) || 0}
          hidden={isGrouped}
        />
      )}
      <div className="message-content-wrapper" style={{ maxWidth: "90%" }}>
        {!isOwn && !isGrouped && (
          <MessageHeader name={message.displayName} role={message.role} />
        )}
        <div className="message-bubble bubble-test-fail">
          <div className="test-fail-header">
            <span className="test-icon">🧪</span>
            <span className="test-count">
              {tf.totalFailed} test{tf.totalFailed !== 1 ? "s" : ""} failed
            </span>
            {tf.framework && (
              <span className="test-framework">{tf.framework}</span>
            )}
          </div>
          <div className="test-fail-list">
            {tf.tests.map((t, i) => (
              <div key={i} className="test-fail-item">
                <span className="test-fail-icon">✗</span>
                <div className="test-fail-detail">
                  <span className="test-name">{t.name}</span>
                  {t.errorMessage && (
                    <span className="test-error">{t.errorMessage}</span>
                  )}
                  {t.filePath && (
                    <button
                      className="test-nav"
                      onClick={() =>
                        send({
                          command: "navigateToCode",
                          relativePath: t.filePath!,
                          startLine: t.lineNumber || 1,
                        })
                      }
                    >
                      {t.filePath}:{t.lineNumber || "?"}
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
        <MessageMeta ts={message.ts} isOwn={isOwn} />
      </div>
    </div>
  );
}

// ── Stack Trace Message ───────────────────────────────────

function StackTraceMessage({ message, isOwn, isGrouped }: TextMessageProps) {
  const st = message.stackTrace;
  if (!st) return null;

  return (
    <div className={`message-row ${isOwn ? "message-own" : "message-other"} ${isGrouped ? "message-grouped" : ""}`}>
      {!isOwn && (
        <Avatar
          name={message.displayName}
          colorIndex={message.userId?.charCodeAt(0) || 0}
          hidden={isGrouped}
        />
      )}
      <div className="message-content-wrapper" style={{ maxWidth: "90%" }}>
        <div className="message-bubble bubble-stack-trace">
          <div className="stack-header">
            <span className="stack-icon">🔥</span>
            <span>Stack Trace</span>
          </div>
          <pre className="stack-content">{st.rawTrace}</pre>
        </div>
        <MessageMeta ts={message.ts} isOwn={isOwn} />
      </div>
    </div>
  );
}

// ── System Message ────────────────────────────────────────

function SystemMessage({ text }: { text: string }) {
  return (
    <div className="system-message animate-fade-in">
      <span className="system-text">{text}</span>
    </div>
  );
}

// ── Shared Sub-components ─────────────────────────────────

function Avatar({
  name,
  colorIndex,
  hidden,
  isAI,
}: {
  name: string;
  colorIndex: number;
  hidden?: boolean;
  isAI?: boolean;
}) {
  if (hidden) {
    return <div className="avatar-spacer" />;
  }

  if (isAI) {
    return (
      <div className="avatar avatar-ai">
        <svg viewBox="0 0 24 24" fill="none" width="16" height="16">
          <path
            d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </div>
    );
  }

  return (
    <div
      className="avatar"
      style={{ background: getAvatarColor(colorIndex) }}
    >
      {getInitials(name)}
    </div>
  );
}

function MessageHeader({
  name,
  role,
  identitySource,
}: {
  name: string;
  role: string;
  identitySource?: string;
}) {
  const roleLabel = role === "host" ? "Lead" : role === "ai" ? "AI" : "";
  const badge = identitySource === "sso" ? "verified" : "";

  return (
    <div className="message-header">
      <span className="message-author">{name}</span>
      {roleLabel && <span className={`role-badge role-${role}`}>{roleLabel}</span>}
      {badge && <span className="verified-badge">✓</span>}
    </div>
  );
}

function MessageMeta({ ts, isOwn }: { ts: number; isOwn: boolean }) {
  return (
    <div className={`message-meta ${isOwn ? "meta-own" : ""}`}>
      <span>{formatTime(ts)}</span>
      {isOwn && <span className="message-status">✓</span>}
    </div>
  );
}

// ── Date Separator ────────────────────────────────────────

import { formatDate } from "../../utils/format";

export function DateSeparator({ ts }: { ts: number }) {
  return (
    <div className="date-separator">
      <div className="date-line" />
      <span className="date-label">{formatDate(ts)}</span>
      <div className="date-line" />
    </div>
  );
}
