import { useEffect, useState, type RefObject } from "react";

// ============================================================
// useContainerWidth — ResizeObserver-based responsive breakpoints
//
// VS Code WebViews don't support CSS media queries on panel width.
// This hook observes the container element and returns a breakpoint
// class name that can be applied to the root element.
//
// Breakpoints:
//   narrow:  <350px  — icons only, full-width messages
//   default: 350-500px — standard sidebar layout
//   wide:    >500px  — split layout, extra detail
// ============================================================

export type WidthBreakpoint = "narrow" | "default" | "wide";

export function useContainerWidth(ref: RefObject<HTMLElement | null>): WidthBreakpoint {
  const [breakpoint, setBreakpoint] = useState<WidthBreakpoint>("default");

  useEffect(() => {
    const el = ref.current;
    if (!el) return;

    const observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width ?? 400;
      if (width < 350) {
        setBreakpoint("narrow");
      } else if (width > 500) {
        setBreakpoint("wide");
      } else {
        setBreakpoint("default");
      }
    });

    observer.observe(el);
    return () => observer.disconnect();
  }, [ref]);

  return breakpoint;
}
