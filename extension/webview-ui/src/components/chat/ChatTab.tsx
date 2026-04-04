import { useRef, useCallback, useEffect } from "react";
import { useChat } from "../../contexts/ChatContext";
import { MessageList } from "./MessageList";
import { ChatInput } from "./ChatInput";
import { ThinkingIndicator } from "./ThinkingIndicator";
import { AgentQuestionCard } from "./AgentQuestionCard";
import { PendingChangesCard } from "./PendingChangesCard";
import { ScanningOverlay } from "./ScanningOverlay";
import { ScrollToBottom } from "../shared/ScrollToBottom";
import { useWebSocket } from "../../hooks/useWebSocket";
import { useHistoryPagination } from "../../hooks/useHistoryPagination";

// ============================================================
// ChatTab — main chat interface
// ============================================================

export function ChatTab() {
  const { state } = useChat();
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const isNearBottomRef = useRef(true);

  useWebSocket();
  useHistoryPagination(scrollContainerRef);

  const handleScroll = useCallback(() => {
    const el = scrollContainerRef.current;
    if (!el) return;
    const threshold = 100;
    isNearBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < threshold;
  }, []);

  // Auto-scroll when new messages arrive
  useEffect(() => {
    if (isNearBottomRef.current) {
      const el = scrollContainerRef.current;
      if (el) {
        requestAnimationFrame(() => { el.scrollTop = el.scrollHeight; });
      }
    }
  }, [state.messages.length]);

  return (
    <div className="chat-tab">
      {/* Pending changes card (above messages) */}
      <PendingChangesCard />

      {/* Scanning overlay */}
      <ScanningOverlay />

      {/* Message area */}
      <div ref={scrollContainerRef} className="chat-messages-area" onScroll={handleScroll}>
        {/* Empty state */}
        {state.messages.length === 0 && !state.isAIThinking && (
          <div className="chat-empty">
            <div className="chat-empty-icon">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" width="32" height="32">
                <path d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
            </div>
            <p className="chat-empty-text">Start a conversation</p>
            <p className="chat-empty-hint">Type a message, use /ask for AI, or /jira for tickets</p>
          </div>
        )}

        {/* Message list */}
        {state.messages.length > 0 && (
          <MessageList messages={state.messages} scrollContainerRef={scrollContainerRef} />
        )}

        {/* AI thinking / agent question */}
        {state.isAIThinking && !state.agentQuestion && (
          <ThinkingIndicator brainTree={state.brainTree} currentAction={state.currentAction} />
        )}
        {state.agentQuestion && <AgentQuestionCard question={state.agentQuestion} />}

        <div style={{ height: 8, flexShrink: 0 }} />
      </div>

      {/* Scroll to bottom FAB */}
      <ScrollToBottom scrollContainerRef={scrollContainerRef} isNearBottomRef={isNearBottomRef} />

      {/* Input area */}
      <ChatInput />
    </div>
  );
}
