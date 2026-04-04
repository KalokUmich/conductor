import { memo, useCallback, useDeferredValue } from "react";
import { useChat } from "../../contexts/ChatContext";
import type { BrainTree, AgentState } from "../../types/messages";
import { escapeHtml } from "../../utils/format";

// ============================================================
// ThinkingIndicator — concurrent rendering with useDeferredValue
//
// This is the main perf bottleneck in the old chat.html.
// Old: innerHTML replaced on every SSE event (dozens/sec), causing reflow.
// New: React diffs only changed nodes. useDeferredValue ensures the
// indicator update never blocks the input thread.
// ============================================================

interface ThinkingIndicatorProps {
  brainTree: BrainTree;
  currentAction: string;
}

export const ThinkingIndicator = memo(function ThinkingIndicator({
  brainTree,
  currentAction,
}: ThinkingIndicatorProps) {
  const { stopAI } = useChat();

  // Defer the tree rendering — input stays responsive during rapid SSE
  const deferredTree = useDeferredValue(brainTree);
  const deferredAction = useDeferredValue(currentAction);

  const agentEntries = Object.entries(deferredTree.agents);

  return (
    <div className="thinking-indicator animate-slide-up">
      {/* AI Avatar */}
      <div className="thinking-avatar">
        <svg className="animate-spin" width="16" height="16" viewBox="0 0 24 24" fill="none">
          <circle opacity="0.25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
          <path
            opacity="0.75"
            fill="currentColor"
            d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
          />
        </svg>
      </div>

      {/* Content */}
      <div className="thinking-content">
        <div className="thinking-header">
          <span className="thinking-label">Brain</span>
          {agentEntries.length > 0 && (
            <span className="thinking-agent-count">
              {agentEntries.length} agent{agentEntries.length > 1 ? "s" : ""}
            </span>
          )}
          <button
            className="thinking-stop"
            onClick={stopAI}
            title="Stop investigation"
            aria-label="Stop AI"
          >
            <svg width="8" height="8" viewBox="0 0 8 8" fill="currentColor">
              <rect width="8" height="8" rx="1" />
            </svg>
          </button>
        </div>

        {/* Current action */}
        <div className="thinking-action">{deferredAction}</div>

        {/* Agent tree — only render if agents exist */}
        {agentEntries.length > 0 && (
          <div className="thinking-tree">
            {agentEntries.map(([name, agent]) => (
              <AgentRow key={name} name={name} agent={agent} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
});

// ── Agent Row ─────────────────────────────────────────────

const AgentRow = memo(function AgentRow({
  name,
  agent,
}: {
  name: string;
  agent: AgentState;
}) {
  const statusIcon =
    agent.status === "done" ? "✓" :
    agent.status === "fail" ? "✗" : "⟳";

  const statusClass =
    agent.status === "done" ? "agent-done" :
    agent.status === "fail" ? "agent-fail" : "agent-running";

  // Show only last 3 tool steps
  const recentSteps = agent.steps.slice(-3);

  return (
    <div className="agent-row">
      <span className={`agent-status ${statusClass}`}>{statusIcon}</span>
      <span className="agent-name">{name}</span>
      {recentSteps.length > 0 && (
        <span className="agent-tools">
          {recentSteps.map((step, i) => (
            <span
              key={i}
              className={`tool-step ${
                step.status === "ok" ? "tool-ok" :
                step.status === "fail" ? "tool-fail" : "tool-running"
              }`}
            >
              {step.status === "ok" ? "✓" : step.status === "fail" ? "✗" : "…"}{" "}
              {step.tool}
            </span>
          ))}
        </span>
      )}
    </div>
  );
});
