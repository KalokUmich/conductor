import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSession } from "../../contexts/SessionContext";
import { useVSCode } from "../../contexts/VSCodeContext";
import type { ChatMessage } from "../../types/messages";
import { escapeHtml, formatTime, getInitials, getAvatarColor } from "../../utils/format";
import { CodeBlock } from "../shared/CodeBlock";
import { DiagramLightbox } from "../shared/DiagramLightbox";

// ============================================================
// MessageBubble — renders a single chat message
// ============================================================

/** Resolve display info from SessionContext users map (ChatRecord v2 participants). */
function useParticipant(message: ChatMessage) {
  const { state } = useSession();
  const uid = message.sender || message.userId;
  const user = state.users.get(uid);
  return {
    displayName: user?.displayName || message.displayName || "Unknown",
    role: user?.role || message.role || "engineer",
    identitySource: user?.identitySource || message.identitySource,
    avatarColor: uid?.charCodeAt(0) || 0,
  };
}

interface MessageBubbleProps {
  message: ChatMessage;
  isGrouped: boolean;
}

export const MessageBubble = memo(function MessageBubble({
  message,
  isGrouped,
}: MessageBubbleProps) {
  const { state } = useSession();
  const isOwn = state.knownUserIds.has(message.userId);

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
  const p = useParticipant(message);
  return (
    <div className={`message-row ${isOwn ? "message-own" : "message-other"} ${isGrouped ? "message-grouped" : ""}`}>
      {!isOwn && (
        <Avatar
          name={p.displayName}
          colorIndex={p.avatarColor}
          hidden={isGrouped}
        />
      )}
      <div className="message-content-wrapper">
        {!isOwn && !isGrouped && (
          <MessageHeader
            name={p.displayName}
            role={p.role}
            identitySource={p.identitySource}
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
  const { send } = useVSCode();
  const { state: sessionState } = useSession();
  const [codePromptLoading, setCodePromptLoading] = useState(false);
  const [copied, setCopied] = useState(false);
  const [planApplied, setPlanApplied] = useState(false);
  const [planApplying, setPlanApplying] = useState(false);

  // Code prompt generation: show button for ai_summary messages with content
  const isSummary = message.type === "ai_summary" && !!message.summary;

  // Plan apply bar: show when AI responded to a planMode query
  const hasPlan = !!message.planQuery && !!content;

  const handleCopyMessage = useCallback(() => {
    navigator.clipboard.writeText(content).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }, [content]);

  const handleMarkCode = useCallback(() => {
    if (!sessionState.session?.roomId || !content) return;
    setPlanApplying(true);

    // Detect if this is an investigation (needs TODO markers) vs code change
    const isInvestigation = /investigate|investigation|findings|jira.*ticket/i.test(message.planQuery || "");
    const ticketKeyMatch = content.match(/\b([A-Z][A-Z0-9]+-\d+)\b/);
    const ticketTag = ticketKeyMatch ? `{jira:${ticketKeyMatch[1]}}` : "";

    const applyQuery = isInvestigation
      ? `[apply] Based on the investigation below, add TODO comments at each identified code location. ` +
        `For each file mentioned in the plan:\n` +
        `1. Use read_file to read the file\n` +
        `2. Use file_edit to add a TODO comment at or near the identified line\n` +
        `3. Format: "// TODO: ${ticketTag} [short description of what needs to change]"\n` +
        `4. Add a TODO_DESC line: "// TODO_DESC: [details from the plan]"\n` +
        `Do NOT re-investigate or re-analyze. Just add TODO markers at the locations identified below.\n` +
        `After all TODOs are added, list what was marked.\n\n---\n\n${content}`
      : `[apply] Execute the following plan. Use file_edit and file_write tools to make the changes. ` +
        `Apply all changes described below. For each file, use read_file first, then file_edit for precise changes. ` +
        `After all changes, summarize what was modified.\n\n---\n\n${content}`;

    send({
      command: "askAI",
      roomId: sessionState.session.roomId,
      query: applyQuery,
    });
    setPlanApplied(true);
  }, [send, sessionState.session?.roomId, content, message.planQuery]);

  const handleGenerateCodePrompt = useCallback(() => {
    if (!sessionState.session?.roomId || !message.summary) return;
    setCodePromptLoading(true);
    send({
      command: "generateCodePromptAndPost",
      decisionSummary: message.summary,
      roomId: sessionState.session.roomId,
    });
  }, [send, sessionState.session?.roomId, message.summary]);

  const [showSteps, setShowSteps] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const thinkingSteps = message.thinkingSteps || [];
  const toolSteps = thinkingSteps.filter(
    (s: { kind?: string }) => s.kind === "tool_call" || s.kind === "tool_result"
  );
  const toolCount = toolSteps.length;
  const fileCount = new Set(
    thinkingSteps
      .filter((s: { kind?: string; tool?: string }) => s.kind === "tool_result" && s.tool === "read_file")
      .map((s: { params?: { path?: string } }) => s.params?.path)
      .filter(Boolean)
  ).size;

  // Extract first meaningful line as collapse summary
  const summaryLine = useMemo(() => {
    const lines = content.split("\n").filter(l => l.trim() && !l.startsWith("#") && !l.startsWith("---") && !l.startsWith("```"));
    const first = lines[0] || "";
    return first.length > 80 ? first.slice(0, 80) + "..." : first;
  }, [content]);

  // Auto-collapse long messages (>15 lines)
  const isLong = content.split("\n").length > 15;

  return (
    <div className={`message-row message-other ${isGrouped ? "message-grouped" : ""}`}>
      <Avatar name="AI" colorIndex={-1} hidden={isGrouped} isAI />
      <div className="message-content-wrapper" style={{ maxWidth: "92%" }}>
        {!isGrouped && (
          <MessageHeader name="Brain" role="ai" />
        )}
        <div className="message-bubble bubble-ai">
          {/* Collapse bar for long AI messages */}
          {isLong && (
            <button className="ai-collapse-bar" onClick={() => setCollapsed(!collapsed)}>
              <span className={`ai-collapse-chevron ${collapsed ? "" : "expanded"}`}>▸</span>
              <span className="ai-collapse-summary">
                {collapsed ? summaryLine : (message.type === "ai_summary" ? "Summary" : "AI Response")}
              </span>
            </button>
          )}
          {!collapsed && <AIContent content={content} />}
          {/* Investigation steps disclosure (post-completion) */}
          {toolCount > 0 && (
            <div className="investigation-disclosure">
              <button
                className="investigation-toggle"
                onClick={() => setShowSteps(!showSteps)}
                aria-expanded={showSteps}
              >
                <span className="investigation-chevron">{showSteps ? "▾" : "▸"}</span>
                {fileCount > 0
                  ? `Investigated ${fileCount} file${fileCount > 1 ? "s" : ""} with ${toolCount} tool${toolCount > 1 ? "s" : ""}`
                  : `Used ${toolCount} tool${toolCount > 1 ? "s" : ""}`}
              </button>
              {showSteps && (
                <div className="investigation-steps">
                  {thinkingSteps
                    .filter((s: { kind?: string }) => s.kind === "tool_call")
                    .map((step: { tool?: string; summary?: string; success?: boolean }, i: number) => (
                      <div key={i} className={`investigation-step ${step.success === false ? "step-fail" : "step-ok"}`}>
                        <span className="step-icon">{step.success === false ? "✗" : "✓"}</span>
                        <span className="step-text">{step.summary || step.tool || "tool"}</span>
                      </div>
                    ))}
                </div>
              )}
            </div>
          )}
          {/* AI message actions: copy + code prompt */}
          <div className="ai-message-actions">
            <button
              className="ai-copy-btn"
              onClick={handleCopyMessage}
              title="Copy response"
            >
              {copied ? "Copied!" : "Copy"}
            </button>
          </div>
          {/* Code prompt generation button for summaries */}
          {isSummary && (
            <div className="code-prompt-action">
              <button
                className="action-btn action-brand code-prompt-btn"
                onClick={handleGenerateCodePrompt}
                disabled={codePromptLoading}
              >
                <svg viewBox="0 0 20 20" fill="currentColor" width="12" height="12">
                  <path fillRule="evenodd" d="M12.316 3.051a1 1 0 01.633 1.265l-4 12a1 1 0 11-1.898-.632l4-12a1 1 0 011.265-.633zM5.707 6.293a1 1 0 010 1.414L3.414 10l2.293 2.293a1 1 0 11-1.414 1.414l-3-3a1 1 0 010-1.414l3-3a1 1 0 011.414 0zm8.586 0a1 1 0 011.414 0l3 3a1 1 0 010 1.414l-3 3a1 1 0 11-1.414-1.414L16.586 10l-2.293-2.293a1 1 0 010-1.414z" clipRule="evenodd" />
                </svg>
                {codePromptLoading ? "Generating..." : "Generate Code Prompt"}
              </button>
            </div>
          )}
          {/* Plan Apply Bar — appears after AI responds to planMode investigation */}
          {hasPlan && !planApplied && (
            <div className="plan-apply-bar">
              <button
                className="plan-apply-btn"
                onClick={handleMarkCode}
                disabled={planApplying}
              >
                <svg viewBox="0 0 20 20" fill="currentColor" width="14" height="14">
                  <path fillRule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clipRule="evenodd"/>
                </svg>
                {planApplying ? "Marking..." : "Mark Code"}
              </button>
              <button
                className="plan-dismiss-btn"
                onClick={() => setPlanApplied(true)}
              >
                Dismiss
              </button>
            </div>
          )}
          {hasPlan && planApplied && !planApplying && (
            <div className="plan-applied-badge">Applied</div>
          )}
        </div>
        <MessageMeta ts={message.ts} isOwn={false} />
      </div>
    </div>
  );
}

/** Render AI content with markdown-like formatting + mermaid + file nav */
function AIContent({ content }: { content: string }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [lightboxSvg, setLightboxSvg] = useState<string | null>(null);
  const { send } = useVSCode();

  // Split by code blocks
  const parts = content.split(/(```[\s\S]*?```)/g);

  // After render: mermaid diagrams + file-ref click handlers
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    // Mermaid rendering
    if (window.mermaid) {
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
          mel.classList.add("mermaid-clickable");
          mel.title = "Click to zoom";
          mel.addEventListener("click", () => setLightboxSvg(mel.innerHTML));
        } catch {
          // keep raw source
        }
      });
    }

    // File reference click handlers
    const fileRefs = el.querySelectorAll<HTMLButtonElement>(".file-ref");
    const handleFileRefClick = (e: Event) => {
      const btn = e.currentTarget as HTMLButtonElement;
      const path = btn.dataset.path;
      const line = parseInt(btn.dataset.line || "1", 10);
      if (path) {
        send({ command: "navigateToCode", relativePath: path, startLine: line, endLine: line });
      }
    };
    fileRefs.forEach(ref => ref.addEventListener("click", handleFileRefClick));
    return () => {
      fileRefs.forEach(ref => ref.removeEventListener("click", handleFileRefClick));
    };
  }, [content, send]);

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
      {lightboxSvg && (
        <DiagramLightbox svgHtml={lightboxSvg} onClose={() => setLightboxSvg(null)} />
      )}
    </div>
  );
}



/** Markdown → HTML renderer (headers, bold, italic, inline code, lists, blockquotes, tables, links, hr) */
export function renderMarkdown(text: string): string {
  let html = escapeHtml(text);

  // Strip blank lines to single \n before block-level matching.
  // This prevents \n* greediness issues between adjacent headers.
  html = html.replace(/\n{2,}/g, "\n");

  // Horizontal rule
  html = html.replace(/^ {0,3}---$/gm, '<hr class="md-hr" />');

  // Headers — process ### before ## before # (greedy order)
  // Strip **bold** wrapping inside headers (headers are already bold by CSS)
  const stripBold = (s: string) => s.replace(/^\*\*(.*)\*\*$/, "$1").replace(/\*\*(.*?)\*\*/g, "$1");
  html = html.replace(/^ {0,3}###\s+(.+)$/gm, (_, t) => `<h3 class="md-h3">${stripBold(t)}</h3>`);
  html = html.replace(/^ {0,3}##\s+(.+)$/gm, (_, t) => `<h2 class="md-h2">${stripBold(t)}</h2>`);
  html = html.replace(/^ {0,3}#\s+(.+)$/gm, (_, t) => `<h1 class="md-h1">${stripBold(t)}</h1>`);

  // Blockquotes: > text
  html = html.replace(/^&gt; (.+)$/gm, '<blockquote class="md-blockquote">$1</blockquote>');

  // Tables: | col | col | with header separator |---|---|
  html = html.replace(/(?:^\|.+\|$\n?)+/gm, (match) => {
    const rows = match.trim().split("\n").filter(r => r.trim());
    if (rows.length < 2) return match;
    // Check for separator row: every cell contains only dashes, colons, spaces
    const isSepRow = (r: string) => {
      const cells = r.split("|").filter((_, i, a) => i > 0 && i < a.length - 1);
      return cells.length > 0 && cells.every(c => /^[\s:-]+$/.test(c));
    };
    const sepIdx = rows.findIndex(isSepRow);
    if (sepIdx < 0) return match;
    const headerRows = rows.slice(0, sepIdx);
    const bodyRows = rows.slice(sepIdx + 1);
    const parseCells = (row: string) =>
      row.split("|").filter((_, i, a) => i > 0 && i < a.length - 1).map(c => c.trim());

    let table = '<table>';
    if (headerRows.length > 0) {
      table += '<thead>';
      for (const hr of headerRows) {
        table += '<tr>' + parseCells(hr).map(c => `<th>${c}</th>`).join("") + '</tr>';
      }
      table += '</thead>';
    }
    if (bodyRows.length > 0) {
      table += '<tbody>';
      for (const br of bodyRows) {
        table += '<tr>' + parseCells(br).map(c => `<td>${c}</td>`).join("") + '</tr>';
      }
      table += '</tbody>';
    }
    table += '</table>';
    return table;
  });

  // Unordered lists: - item (consecutive lines)
  html = html.replace(/(?:^- (.+)$\n?)+/gm, (match) => {
    const items = match.trim().split("\n").map(line => {
      const content = line.replace(/^- /, "");
      return `<li>${content}</li>`;
    }).join("");
    return `<ul class="md-ul">${items}</ul>`;
  });

  // Ordered lists: 1. item (consecutive lines)
  html = html.replace(/(?:^\d+\. (.+)$\n?)+/gm, (match) => {
    const items = match.trim().split("\n").map(line => {
      const content = line.replace(/^\d+\. /, "");
      return `<li>${content}</li>`;
    }).join("");
    return `<ol class="md-ol">${items}</ol>`;
  });

  // Bold, italic, inline code
  html = html.replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\*(.*?)\*/g, "<em>$1</em>");
  html = html.replace(/`([^`]+)`/g, '<code class="inline-code">$1</code>');

  // File path auto-linking: src/path/file.ts:42 → clickable reference
  // Also matches PascalCase class names without extension: ExternalCommonServiceImpl:340
  html = html.replace(
    /(?<!["\w/])([a-zA-Z0-9_][a-zA-Z0-9_/.-]+\.[a-zA-Z]{1,5}):(\d+)(?:-\d+)?/g,
    '<button class="file-ref" data-path="$1" data-line="$2">$&</button>'
  );
  html = html.replace(
    /(?<!["\w/.])([A-Z][a-zA-Z0-9_]{3,}(?:\.[A-Z][a-zA-Z0-9_]*)*):(\d+)(?:-\d+)?/g,
    (match, path, line) => {
      if (html.includes(`data-path="${path}"`)) return match; // already linked
      return `<button class="file-ref" data-path="${path}" data-line="${line}">${match}</button>`;
    }
  );

  // Newlines → <br />
  html = html.replace(/\n/g, "<br />");

  // Clean up: remove <br /> immediately before/after block elements
  html = html.replace(/(<br \/>)+(<(?:h[1-3]|hr|ul|ol|table|blockquote))/g, "$2");
  html = html.replace(/(<\/(?:h[1-3]|ul|ol|table|blockquote)>)(<br \/>)+/g, "$1");
  html = html.replace(/(<hr class="md-hr" \/>)(<br \/>)+/g, "$1");
  html = html.replace(/(<br \/>)+(<hr class="md-hr" \/>)/g, "$2");

  return html;
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
  const p = useParticipant(message);
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
          name={p.displayName}
          colorIndex={p.avatarColor}
          hidden={isGrouped}
        />
      )}
      <div className="message-content-wrapper" style={{ maxWidth: "88%" }}>
        {!isOwn && !isGrouped && (
          <MessageHeader name={p.displayName} role={p.role} />
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
  const p = useParticipant(message);
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
          name={p.displayName}
          colorIndex={p.avatarColor}
          hidden={isGrouped}
        />
      )}
      <div className="message-content-wrapper">
        {!isOwn && !isGrouped && (
          <MessageHeader name={p.displayName} role={p.role} />
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
  const p = useParticipant(message);
  const { send } = useVSCode();
  const tf = message.testFailures;
  if (!tf) return null;

  return (
    <div className={`message-row ${isOwn ? "message-own" : "message-other"} ${isGrouped ? "message-grouped" : ""}`}>
      {!isOwn && (
        <Avatar
          name={p.displayName}
          colorIndex={p.avatarColor}
          hidden={isGrouped}
        />
      )}
      <div className="message-content-wrapper" style={{ maxWidth: "90%" }}>
        {!isOwn && !isGrouped && (
          <MessageHeader name={p.displayName} role={p.role} />
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
  const p = useParticipant(message);
  const st = message.stackTrace;
  if (!st) return null;

  return (
    <div className={`message-row ${isOwn ? "message-own" : "message-other"} ${isGrouped ? "message-grouped" : ""}`}>
      {!isOwn && (
        <Avatar
          name={p.displayName}
          colorIndex={p.avatarColor}
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
          {/* Robot face — cute AI avatar */}
          <rect x="4" y="8" width="16" height="12" rx="3" stroke="currentColor" strokeWidth="1.5" />
          <circle cx="9" cy="14" r="1.5" fill="currentColor" />
          <circle cx="15" cy="14" r="1.5" fill="currentColor" />
          <path d="M10 17.5h4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
          <path d="M12 4v4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
          <circle cx="12" cy="3" r="1.5" stroke="currentColor" strokeWidth="1.5" />
          <path d="M2 13h2M20 13h2" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
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
