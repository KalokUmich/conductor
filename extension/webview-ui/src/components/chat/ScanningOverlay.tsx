import { useEffect, useRef, useState } from "react";
import { useVSCode, useCommand } from "../../contexts/VSCodeContext";

// ============================================================
// ScanningOverlay — workspace indexing progress + branch change
// ============================================================

interface IndexState {
  visible: boolean;
  phase: string;
  detail: string;
  progress: number;
  branchFrom: string;
  branchTo: string;
  modeBadge: string;
}

export function ScanningOverlay() {
  const [state, setState] = useState<IndexState>({
    visible: false,
    phase: "",
    detail: "",
    progress: 0,
    branchFrom: "",
    branchTo: "",
    modeBadge: "",
  });
  const branchInfoRef = useRef<{ from: string; to: string } | null>(null);
  const { onAny } = useVSCode();

  // Listen for indexBranchChanged — store for next indexProgress
  useCommand("indexBranchChanged", (msg) => {
    if (msg.command !== "indexBranchChanged") return;
    const data = msg as unknown as { from: string; to: string };
    branchInfoRef.current = { from: data.from || "?", to: data.to || "?" };
  });

  // Listen for indexProgress
  useEffect(() => {
    return onAny((msg) => {
      const cmd = (msg as unknown as { command: string }).command;
      if (cmd !== "indexProgress") return;
      const p = (msg as unknown as { payload: Record<string, unknown> }).payload;
      if (!p) return;

      if (p.phase === "done") {
        setState((s) => ({ ...s, visible: false }));
        branchInfoRef.current = null;
        return;
      }

      // Calculate progress based on phase
      let pct = 0;
      let phaseLabel = "";
      let detailLabel = "";
      const filesScanned = (p.filesScanned as number) || 0;
      const totalFiles = (p.totalFiles as number) || 0;
      const filesIndexed = (p.filesIndexed as number) || 0;
      const symbolsExtracted = (p.symbolsExtracted as number) || 0;
      const embeddingsEnqueued = (p.embeddingsEnqueued as number) || 0;
      const isIncremental = p.isIncremental as boolean;
      const staleFilesCount = p.staleFilesCount as number | undefined;
      const embeddingEnabled = p.embeddingEnabled as boolean;

      // Only show overlay for significant operations
      const showOverlay =
        !isIncremental &&
        (!!branchInfoRef.current || staleFilesCount === undefined || (staleFilesCount || 0) > 20);

      if (p.phase === "scanning") {
        pct = totalFiles > 0 ? Math.round((filesScanned / totalFiles) * 100) : 5;
        phaseLabel = "Scanning workspace";
        detailLabel = `Collecting file metadata... (${filesScanned} files)`;
      } else if (p.phase === "extracting") {
        const filePct = totalFiles > 0 ? filesIndexed / totalFiles : 0;
        pct = Math.min(90, Math.max(10, Math.round(filePct * 80) + 10));
        phaseLabel = "Building AST index";
        detailLabel = filesIndexed
          ? `Extracting symbols... (${filesIndexed}/${totalFiles} files)`
          : `Extracting symbols... (${symbolsExtracted} symbols)`;
      } else if (p.phase === "embedding") {
        pct = 70 + Math.min(28, Math.round((embeddingsEnqueued / Math.max(symbolsExtracted, 1)) * 28));
        phaseLabel = "Generating embeddings";
        detailLabel = `Embedding symbols... (${embeddingsEnqueued} queued)`;
      }

      const bi = branchInfoRef.current;
      setState({
        visible: showOverlay,
        phase: phaseLabel,
        detail: detailLabel,
        progress: pct,
        branchFrom: bi?.from || "",
        branchTo: bi?.to || "",
        modeBadge: !embeddingEnabled ? "AST only — embedding unavailable" : "",
      });
    });
  }, [onAny]);

  // Listen for setup & index progress (uses scanning overlay)
  useEffect(() => {
    return onAny((msg) => {
      const cmd = (msg as unknown as { command: string }).command;
      if (cmd === "setupAndIndexProgress") {
        const data = msg as unknown as { detail?: string; percent?: number };
        setState({
          visible: true,
          phase: "Setting up workspace",
          detail: data.detail || "Cloning repository...",
          progress: data.percent || 0,
          branchFrom: "",
          branchTo: "",
          modeBadge: "",
        });
      } else if (cmd === "setupAndIndexComplete") {
        setState((s) => ({ ...s, visible: false }));
      }
    });
  }, [onAny]);

  useCommand("indexRebuildComplete", (msg) => {
    if (msg.command !== "indexRebuildComplete") return;
    setState((s) => ({ ...s, visible: false }));
    branchInfoRef.current = null;
  });

  if (!state.visible) return null;

  return (
    <div className="scanning-overlay animate-fade-in">
      <div className="scanning-content">
        {/* Branch change banner */}
        {state.branchFrom && state.branchTo && (
          <div className="branch-banner">
            Branch changed: {state.branchFrom} → {state.branchTo} — rebuilding index
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

        {/* AST-only mode badge */}
        {state.modeBadge && <div className="scanning-mode-badge">{state.modeBadge}</div>}
      </div>
    </div>
  );
}
