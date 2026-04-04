import { useCallback, useEffect, useRef } from "react";
import { useChat } from "../contexts/ChatContext";
import { useSession } from "../contexts/SessionContext";
import { useVSCode, useCommand } from "../contexts/VSCodeContext";
import type { ChatMessage } from "../types/messages";

// ============================================================
// useHistoryPagination — scroll-to-top loads older messages
// ============================================================

const SCROLL_THRESHOLD = 50; // px from top to trigger load

export function useHistoryPagination(
  scrollContainerRef: React.RefObject<HTMLDivElement | null>
) {
  const { state: chatState, dispatch } = useChat();
  const { state: sessionState } = useSession();
  const { send } = useVSCode();
  const loadingRef = useRef(false);

  // Listen for history loaded
  useCommand("historyLoaded", (msg) => {
    if (msg.command !== "historyLoaded") return;
    const data = msg as { messages?: unknown[]; hasMore?: boolean };
    loadingRef.current = false;

    if (data.messages && Array.isArray(data.messages) && data.messages.length > 0) {
      // Sort chronologically and add to front
      const sorted = (data.messages as ChatMessage[]).sort((a, b) => a.ts - b.ts);
      dispatch({ type: "ADD_MESSAGES_BATCH", messages: sorted });
    }

    dispatch({
      type: "SET_HAS_MORE_HISTORY",
      hasMore: (data as { hasMore?: boolean }).hasMore === true,
    });
  });

  // Also handle local messages loaded
  useCommand("localMessagesLoaded", (msg) => {
    if (msg.command !== "localMessagesLoaded") return;
    const data = msg as { messages?: unknown[] };
    if (data.messages && Array.isArray(data.messages) && data.messages.length > 0) {
      const sorted = (data.messages as ChatMessage[]).sort((a, b) => a.ts - b.ts);
      dispatch({ type: "ADD_MESSAGES_BATCH", messages: sorted });
    }
  });

  // Load more on scroll to top
  const handleScroll = useCallback(() => {
    const el = scrollContainerRef.current;
    if (!el) return;

    if (
      el.scrollTop < SCROLL_THRESHOLD &&
      chatState.hasMoreHistory &&
      !loadingRef.current &&
      sessionState.session?.roomId
    ) {
      loadingRef.current = true;
      dispatch({ type: "SET_HISTORY_LOADING", loading: true });

      // Get oldest message timestamp
      const oldest = chatState.messages[0]?.ts;
      if (!oldest) {
        loadingRef.current = false;
        dispatch({ type: "SET_HAS_MORE_HISTORY", hasMore: false });
        return;
      }

      send({
        command: "loadHistory",
        roomId: sessionState.session.roomId,
      } as never); // The extension handles `before` param internally
    }
  }, [chatState.hasMoreHistory, chatState.messages, dispatch, scrollContainerRef, send, sessionState.session?.roomId]);

  useEffect(() => {
    const el = scrollContainerRef.current;
    if (!el) return;
    el.addEventListener("scroll", handleScroll, { passive: true });
    return () => el.removeEventListener("scroll", handleScroll);
  }, [scrollContainerRef, handleScroll]);
}
