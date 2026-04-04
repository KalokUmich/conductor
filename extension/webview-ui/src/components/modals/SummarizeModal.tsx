import { useCallback, useState } from "react";
import { useVSCode, useCommand } from "../../contexts/VSCodeContext";
import { useChat } from "../../contexts/ChatContext";
import { useSession } from "../../contexts/SessionContext";
import { Modal } from "../shared/Modal";

// ============================================================
// Summarize Modals — options, message selection, results
// ============================================================

interface Props {
  open: boolean;
  onClose: () => void;
}

type Phase = "options" | "select" | "loading" | "results";

interface SummaryResult {
  topic?: string;
  problem?: string;
  solution?: string;
  risk?: string;
  components?: string[];
  steps?: string[];
}

export function SummarizeModal({ open, onClose }: Props) {
  const { send } = useVSCode();
  const { state: chatState } = useChat();
  const { state: sessionState } = useSession();
  const [phase, setPhase] = useState<Phase>("options");
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [result, setResult] = useState<SummaryResult | null>(null);
  const [codePrompt, setCodePrompt] = useState("");
  const [codePromptLoading, setCodePromptLoading] = useState(false);

  useCommand("summarizeResult", (msg) => {
    if (msg.command !== "summarizeResult") return;
    if (msg.data.error) {
      setPhase("options");
      return;
    }
    try {
      const parsed = typeof msg.data.decision_summary === "string"
        ? JSON.parse(msg.data.decision_summary)
        : msg.data.decision_summary;
      setResult(parsed);
      setPhase("results");
    } catch {
      setResult({ topic: "Summary", problem: msg.data.decision_summary as string });
      setPhase("results");
    }
  });

  useCommand("codePromptResult", (msg) => {
    if (msg.command !== "codePromptResult") return;
    setCodePromptLoading(false);
    if (msg.data.code_prompt) setCodePrompt(msg.data.code_prompt);
  });

  const handleDistillAll = useCallback(() => {
    const messages = chatState.messages
      .filter((m) => m.content && m.type !== "system")
      .map((m) => ({ role: m.role === "host" ? "host" : "engineer", text: m.content, timestamp: m.ts }));
    setPhase("loading");
    send({ command: "summarize", query: "Summarize this discussion", context: messages });
  }, [send, chatState.messages]);

  const handleDistillSelected = useCallback(() => {
    const messages = chatState.messages
      .filter((m) => selectedIds.has(m.id))
      .map((m) => ({ role: m.role === "host" ? "host" : "engineer", text: m.content, timestamp: m.ts }));
    setPhase("loading");
    send({ command: "summarize", query: "Summarize these selected messages", context: messages });
  }, [send, chatState.messages, selectedIds]);

  const handleGenerateCodePrompt = useCallback(() => {
    if (!result) return;
    setCodePromptLoading(true);
    send({ command: "generateCodePrompt", decisionSummary: JSON.stringify(result) });
  }, [send, result]);

  const handleCopyPrompt = useCallback(() => {
    navigator.clipboard?.writeText(codePrompt);
  }, [codePrompt]);

  const handlePostPrompt = useCallback(() => {
    if (!sessionState.session?.roomId) return;
    send({ command: "generateCodePromptAndPost", decisionSummary: JSON.stringify(result), roomId: sessionState.session.roomId });
    onClose();
  }, [send, result, sessionState.session?.roomId, onClose]);

  const toggleMessage = useCallback((id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }, []);

  const handleClose = useCallback(() => {
    setPhase("options");
    setResult(null);
    setCodePrompt("");
    setSelectedIds(new Set());
    onClose();
  }, [onClose]);

  const textMessages = chatState.messages.filter((m) => m.content && m.type !== "system");

  return (
    <Modal open={open} onClose={handleClose} title={phase === "results" ? "Decision Summary" : "Distill Conversation"} width="420px">
      {/* Phase: Options */}
      {phase === "options" && (
        <div className="summarize-options stagger-children">
          <button className="summarize-option-card" onClick={handleDistillAll}>
            <span className="soc-icon">✨</span>
            <div>
              <span className="soc-title">Distill All Messages</span>
              <span className="soc-desc">{textMessages.length} messages</span>
            </div>
          </button>
          <button className="summarize-option-card" onClick={() => setPhase("select")}>
            <span className="soc-icon">☑</span>
            <div>
              <span className="soc-title">Select Messages to Distill</span>
              <span className="soc-desc">Choose specific messages</span>
            </div>
          </button>
        </div>
      )}

      {/* Phase: Select */}
      {phase === "select" && (
        <div className="message-select">
          <div className="msg-select-controls">
            <button className="btn-sm btn-secondary" onClick={() => setSelectedIds(new Set(textMessages.map((m) => m.id)))}>Select All</button>
            <button className="btn-sm btn-secondary" onClick={() => setSelectedIds(new Set())}>Deselect</button>
            <span className="msg-select-count">{selectedIds.size} selected</span>
          </div>
          <div className="msg-select-list">
            {textMessages.map((m) => (
              <label key={m.id} className="msg-select-item">
                <input type="checkbox" checked={selectedIds.has(m.id)} onChange={() => toggleMessage(m.id)} />
                <span className="msg-select-author">{m.displayName}</span>
                <span className="msg-select-text">{m.content.slice(0, 80)}{m.content.length > 80 ? "..." : ""}</span>
              </label>
            ))}
          </div>
          <button className="btn-primary btn-wide" onClick={handleDistillSelected} disabled={selectedIds.size === 0}>
            Distill {selectedIds.size} Messages
          </button>
        </div>
      )}

      {/* Phase: Loading */}
      {phase === "loading" && (
        <div className="summarize-loading">
          <div className="typing-dots"><span /><span /><span /></div>
          <span>Analyzing conversation...</span>
        </div>
      )}

      {/* Phase: Results */}
      {phase === "results" && result && (
        <div className="summary-results stagger-children">
          {result.topic && <div className="sr-section"><h4 className="sr-heading">Topic</h4><p>{result.topic}</p></div>}
          {result.problem && <div className="sr-section"><h4 className="sr-heading">Problem</h4><p>{result.problem}</p></div>}
          {result.solution && <div className="sr-section"><h4 className="sr-heading">Solution</h4><p>{result.solution}</p></div>}
          {result.risk && <div className="sr-section"><h4 className="sr-heading">Risk</h4><p>{result.risk}</p></div>}
          {result.components && result.components.length > 0 && (
            <div className="sr-section">
              <h4 className="sr-heading">Components</h4>
              <ul className="sr-list">{result.components.map((c, i) => <li key={i}>{c}</li>)}</ul>
            </div>
          )}
          {result.steps && result.steps.length > 0 && (
            <div className="sr-section">
              <h4 className="sr-heading">Implementation Steps</h4>
              <ol className="sr-list sr-ordered">{result.steps.map((s, i) => <li key={i}>{s}</li>)}</ol>
            </div>
          )}

          {/* Code prompt generation */}
          <div className="sr-divider" />
          {!codePrompt ? (
            <button className="btn-primary btn-wide" onClick={handleGenerateCodePrompt} disabled={codePromptLoading}>
              {codePromptLoading ? "Generating..." : "Generate Code Prompt"}
            </button>
          ) : (
            <div className="code-prompt-result">
              <textarea className="code-prompt-textarea" value={codePrompt} readOnly rows={6} />
              <div className="code-prompt-actions">
                <button className="btn-secondary btn-sm" onClick={handleCopyPrompt}>Copy</button>
                <button className="btn-primary btn-sm" onClick={handlePostPrompt}>Post to Chat</button>
              </div>
            </div>
          )}
        </div>
      )}
    </Modal>
  );
}
