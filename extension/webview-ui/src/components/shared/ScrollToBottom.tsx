import { useCallback, useEffect, useState, type RefObject } from "react";

// ============================================================
// ScrollToBottom FAB — appears when user scrolls up
// ============================================================

interface Props {
  scrollContainerRef: RefObject<HTMLDivElement | null>;
  isNearBottomRef: RefObject<boolean>;
}

export function ScrollToBottom({ scrollContainerRef, isNearBottomRef }: Props) {
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    const el = scrollContainerRef.current;
    if (!el) return;

    const check = () => {
      setVisible(!isNearBottomRef.current);
    };

    el.addEventListener("scroll", check, { passive: true });
    return () => el.removeEventListener("scroll", check);
  }, [scrollContainerRef, isNearBottomRef]);

  const handleClick = useCallback(() => {
    const el = scrollContainerRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [scrollContainerRef]);

  if (!visible) return null;

  return (
    <button
      className="scroll-fab animate-scale-in"
      onClick={handleClick}
      aria-label="Scroll to bottom"
    >
      <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2">
        <path d="M7 13l5 5 5-5M7 6l5 5 5-5" />
      </svg>
    </button>
  );
}
