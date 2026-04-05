import { useCallback, useEffect, useRef } from "react";
import { useSession } from "../contexts/SessionContext";
import { useWebSocketSend } from "./useWebSocket";

// ============================================================
// useReadReceipts — IntersectionObserver for message read tracking
// ============================================================

export function useReadReceipts(scrollContainerRef: React.RefObject<HTMLDivElement | null>) {
  const { state } = useSession();
  const wsSend = useWebSocketSend();
  const observerRef = useRef<IntersectionObserver | null>(null);
  const readIdsRef = useRef<Set<string>>(new Set());

  const sendReadReceipt = useCallback(
    (messageId: string) => {
      wsSend({
        type: "read_receipt",
        messageId,
        userId: state.session?.userId,
      });
    },
    [wsSend, state.session?.userId]
  );

  useEffect(() => {
    const container = scrollContainerRef.current;
    if (!container) return;

    observerRef.current = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            const messageId = (entry.target as HTMLElement).dataset.messageId;
            if (messageId && !readIdsRef.current.has(messageId)) {
              readIdsRef.current.add(messageId);
              sendReadReceipt(messageId);
              observerRef.current?.unobserve(entry.target);
            }
          }
        });
      },
      { root: container, threshold: 0.5 }
    );

    return () => {
      observerRef.current?.disconnect();
    };
  }, [scrollContainerRef, sendReadReceipt]);

  /** Call this to observe a newly rendered message element */
  const observe = useCallback((el: HTMLElement | null) => {
    if (el && observerRef.current) {
      observerRef.current.observe(el);
    }
  }, []);

  return { observe };
}
