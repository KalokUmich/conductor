import { memo, useCallback, type RefObject } from "react";
import type { ChatMessage } from "../../types/messages";
import { useSession } from "../../contexts/SessionContext";
import { useReadReceipts } from "../../hooks/useReadReceipts";
import { MessageBubble } from "./MessageBubble";
import { DateSeparator } from "./DateSeparator";

// ============================================================
// MessageList — renders messages with date separators and grouping
//
// NOTE: Using standard DOM rendering for now.
// Virtual scrolling (@tanstack/react-virtual) will be added once
// the basic rendering is verified working. The perf gain from
// useTransition + useDeferredValue already handles the main
// bottleneck (AI thinking indicator).
// ============================================================

interface MessageListProps {
  messages: ChatMessage[];
  scrollContainerRef: RefObject<HTMLDivElement | null>;
}

function needsDateSeparator(
  msg: ChatMessage,
  prevMsg: ChatMessage | null
): boolean {
  if (!prevMsg) return true;
  const d1 = new Date(msg.ts * 1000);
  const d2 = new Date(prevMsg.ts * 1000);
  return (
    d1.getDate() !== d2.getDate() ||
    d1.getMonth() !== d2.getMonth() ||
    d1.getFullYear() !== d2.getFullYear()
  );
}

function shouldGroup(msg: ChatMessage, prevMsg: ChatMessage | null): boolean {
  if (!prevMsg) return false;
  if (msg.userId !== prevMsg.userId) return false;
  if (msg.type === "system" || prevMsg.type === "system") return false;
  const timeDiff = (msg.ts - prevMsg.ts) * 1000;
  return timeDiff < 120_000;
}

export const MessageList = memo(function MessageList({
  messages,
  scrollContainerRef,
}: MessageListProps) {
  const { state } = useSession();
  const { observe } = useReadReceipts(scrollContainerRef);
  const knownUserIds = state.knownUserIds;

  // Ref callback: observe non-own messages for read receipts
  const messageRefCallback = useCallback(
    (el: HTMLDivElement | null, isOwn: boolean) => {
      if (el && !isOwn) observe(el);
    },
    [observe]
  );

  return (
    <div className="message-list-simple">
      {messages.map((msg, i) => {
        const prev = i > 0 ? messages[i - 1] : null;
        const showSeparator = needsDateSeparator(msg, prev);
        const isGrouped = shouldGroup(msg, prev);
        const isOwn = knownUserIds.has(msg.userId);

        return (
          <div
            key={msg.id || `msg-${i}`}
            data-message-id={msg.id}
            ref={(el) => messageRefCallback(el, isOwn)}
          >
            {showSeparator && <DateSeparator ts={msg.ts} />}
            <MessageBubble message={msg} isGrouped={isGrouped} />
          </div>
        );
      })}
    </div>
  );
});
