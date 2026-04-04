import { useCallback, useState } from "react";
import { useChat } from "../../contexts/ChatContext";
import type { AgentQuestion } from "../../types/messages";

// ============================================================
// AgentQuestionCard — agent asks user for input
// ============================================================

interface Props {
  question: AgentQuestion;
}

export function AgentQuestionCard({ question }: Props) {
  const { answerAgent } = useChat();
  const [showFreeInput, setShowFreeInput] = useState(!question.options?.length);
  const [freeText, setFreeText] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [submittedAnswer, setSubmittedAnswer] = useState("");

  const submit = useCallback(
    (answer: string) => {
      setSubmitted(true);
      setSubmittedAnswer(answer);
      answerAgent(question.sessionId, answer);
    },
    [answerAgent, question.sessionId]
  );

  // After submission — show compact Q&A summary
  if (submitted) {
    return (
      <div className="agent-question-card submitted animate-fade-in">
        <div className="aq-avatar">?</div>
        <div className="aq-summary">
          <div className="aq-summary-q">Q: {question.question}</div>
          <div className="aq-summary-a">A: {submittedAnswer || "(skipped)"}</div>
        </div>
      </div>
    );
  }

  return (
    <div className="agent-question-card animate-slide-up">
      <div className="aq-avatar">?</div>
      <div className="aq-content">
        <div className="aq-label">AI needs your input</div>
        <div className="aq-question">{question.question}</div>

        {question.context && (
          <div className="aq-context">{question.context}</div>
        )}

        {/* Option buttons */}
        {question.options && question.options.length > 0 && (
          <div className="aq-options">
            {question.options.map((opt, i) => {
              const isRecommended = opt.toLowerCase().includes("(recommended)");
              return (
                <button
                  key={i}
                  className={`aq-option ${isRecommended ? "aq-recommended" : ""}`}
                  onClick={() => submit(opt)}
                >
                  <span className="aq-option-num">{i + 1}</span>
                  <span className="aq-option-text">{opt}</span>
                </button>
              );
            })}
            {!showFreeInput && (
              <button
                className="aq-option aq-other"
                onClick={() => setShowFreeInput(true)}
              >
                <span className="aq-option-num">✎</span>
                <span className="aq-option-text">Other (type your own answer)</span>
              </button>
            )}
          </div>
        )}

        {/* Free text input */}
        {showFreeInput && (
          <div className="aq-input-row">
            <input
              type="text"
              className="aq-input"
              value={freeText}
              onChange={(e) => setFreeText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") submit(freeText);
              }}
              placeholder="Type your answer..."
              autoFocus
            />
            <button className="aq-send" onClick={() => submit(freeText)}>
              Send
            </button>
            {!question.options?.length && (
              <button className="aq-skip" onClick={() => submit("")}>
                Skip
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
