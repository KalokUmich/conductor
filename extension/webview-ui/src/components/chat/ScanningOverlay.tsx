import { useEffect, useState } from "react";
import { useVSCode } from "../../contexts/VSCodeContext";
import { useCommand } from "../../contexts/VSCodeContext";

// ============================================================
// ScanningOverlay — workspace indexing progress
// ============================================================

interface IndexState {
  visible: boolean;
  phase: string;
  detail: string;
  progress: number;
  branchChanged: boolean;
  branchName: string;
}

export function ScanningOverlay() {
  const [state, setState] = useState<IndexState>({
    visible: false,
    phase: "",
    detail: "",
    progress: 0,
    branchChanged: false,
    branchName: "",
  });

  const { onAny } = useVSCode();

  // Listen for indexProgress (not in typed commands, uses onAny)
  useEffect(() => {
    return onAny((msg) => {
      if (msg.command !== ("indexProgress" as string)) return;
      const payload = (msg as unknown as Record<string, unknown>).payload as Record<string, unknown> | undefined;
      if (!payload) return;
      setState({
        visible: true,
        phase: (payload.phase as string) || "Indexing...",
        detail: (payload.detail as string) || "",
        progress: (payload.progress as number) || 0,
        branchChanged: (payload.branchChanged as boolean) || false,
        branchName: (payload.branchName as string) || "",
      });
    });
  }, [onAny]);

  useCommand("indexRebuildComplete", (msg) => {
    if (msg.command !== "indexRebuildComplete") return;
    setState((s) => ({ ...s, visible: false }));
  });

  if (!state.visible) return null;

  return (
    <div className="scanning-overlay animate-fade-in">
      <div className="scanning-content">
        {/* Branch change banner */}
        {state.branchChanged && (
          <div className="branch-banner">
            <svg viewBox="0 0 20 20" fill="currentColor" width="14" height="14">
              <path fillRule="evenodd" d="M5 2a2 2 0 00-2 2v14l3.5-2 3.5 2 3.5-2 3.5 2V4a2 2 0 00-2-2H5zm4.707 3.707a1 1 0 00-1.414-1.414l-3 3a1 1 0 000 1.414l3 3a1 1 0 001.414-1.414L8.414 9H10a3 3 0 013 3v1a1 1 0 102 0v-1a5 5 0 00-5-5H8.414l1.293-1.293z" clipRule="evenodd"/>
            </svg>
            <span>Branch changed to <strong>{state.branchName}</strong></span>
          </div>
        )}

        {/* Spinner */}
        <div className="scanning-spinner">
          <div className="scanning-ring" />
          <div className="scanning-ring-inner" />
        </div>

        {/* Phase text */}
        <div className="scanning-phase">{state.phase}</div>
        {state.detail && <div className="scanning-detail">{state.detail}</div>}

        {/* Progress bar */}
        {state.progress > 0 && (
          <div className="scanning-progress">
            <div className="scanning-progress-bar" style={{ width: `${Math.min(state.progress, 100)}%` }} />
          </div>
        )}
        {state.progress > 0 && (
          <div className="scanning-percent">{Math.round(state.progress)}%</div>
        )}
      </div>
    </div>
  );
}
