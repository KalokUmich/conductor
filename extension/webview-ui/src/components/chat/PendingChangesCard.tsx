import { useCallback, useEffect, useRef, useState } from "react";
import { useSession } from "../../contexts/SessionContext";
import { useVSCode, useCommand } from "../../contexts/VSCodeContext";

// ============================================================
// PendingChangesCard — show/apply/discard code changes
// ============================================================

interface PCChange {
  filePath: string;
  operation: string;
  diff?: string;
  addedLines?: number;
  removedLines?: number;
}

interface PendingState {
  visible: boolean;
  currentChange: PCChange | null;
  currentIndex: number;
  totalChanges: number;
  policyApproved: boolean;
  policyReason: string;
}

export function PendingChangesCard() {
  const { send } = useVSCode();
  const { state: sessionState } = useSession();
  const autoApplyRef = useRef(sessionState.autoApplyEnabled);
  autoApplyRef.current = sessionState.autoApplyEnabled;

  const [state, setState] = useState<PendingState>({
    visible: false,
    currentChange: null,
    currentIndex: 0,
    totalChanges: 0,
    policyApproved: true,
    policyReason: "",
  });

  useCommand("showCurrentChange", (msg) => {
    if (msg.command !== "showCurrentChange") return;
    const change = msg.currentChange as PCChange;
    const policy = msg.policyResult as { approved?: boolean; reason?: string } | undefined;
    const approved = policy?.approved !== false;

    // Auto-apply: if enabled and policy allows, apply immediately
    if (autoApplyRef.current && approved && change) {
      send({ command: "applyChanges", changeSet: { changes: [change as never] } });
      return;
    }

    setState({
      visible: true,
      currentChange: change,
      currentIndex: msg.currentIndex,
      totalChanges: msg.totalChanges,
      policyApproved: approved,
      policyReason: policy?.reason || "",
    });
  });

  useCommand("allChangesComplete", () => {
    setState((s) => ({ ...s, visible: false, currentChange: null }));
  });

  const handleApply = useCallback(() => {
    if (!state.currentChange) return;
    send({ command: "applyChanges", changeSet: { changes: [state.currentChange as never] } });
  }, [send, state.currentChange]);

  const handleViewDiff = useCallback(() => {
    if (!state.currentChange) return;
    send({ command: "viewDiff", changeSet: { changes: [state.currentChange as never] } });
  }, [send, state.currentChange]);

  const handleDiscard = useCallback(() => {
    send({ command: "discardChanges" });
    setState((s) => ({ ...s, visible: false, currentChange: null }));
  }, [send]);

  if (!state.visible || !state.currentChange) return null;

  const change = state.currentChange;
  const fileName = change.filePath?.split("/").pop() || "unknown";

  return (
    <div className="pending-changes-card animate-slide-up">
      {/* Header */}
      <div className="pc-header">
        <h4 className="pc-title">Pending Changes</h4>
        <span className="pc-progress">{state.currentIndex + 1} / {state.totalChanges}</span>
        <button className="pc-dismiss" onClick={handleDiscard} aria-label="Dismiss">×</button>
      </div>

      {/* File info */}
      <div className="pc-file">
        <span className="pc-file-icon">📄</span>
        <span className="pc-filename">{fileName}</span>
        <span className="pc-filepath">{change.filePath}</span>
      </div>

      {/* Stats */}
      <div className="pc-stats">
        {change.addedLines != null && <span className="pc-added">+{change.addedLines}</span>}
        {change.removedLines != null && <span className="pc-removed">-{change.removedLines}</span>}
      </div>

      {/* Policy status */}
      <div className={`pc-policy ${state.policyApproved ? "pc-policy-ok" : "pc-policy-warn"}`}>
        <span className="pc-policy-dot" />
        <span>{state.policyApproved ? "Safe to apply" : state.policyReason || "Review recommended"}</span>
      </div>

      {/* Actions */}
      <div className="pc-actions">
        <button className="btn-secondary btn-sm" onClick={handleViewDiff}>View Diff</button>
        <button className="btn-primary btn-sm" onClick={handleApply}>Apply</button>
      </div>
    </div>
  );
}
