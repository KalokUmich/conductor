import { useCallback, useState } from "react";
import { useSession, useSessionActions } from "../../contexts/SessionContext";
import { useVSCode, useCommand } from "../../contexts/VSCodeContext";
import { AIConfigModal } from "../modals/AIConfigModal";
import { JiraModal } from "../modals/JiraModal";
import { RoomSettingsModal } from "../modals/RoomSettingsModal";
import { SummarizeModal } from "../modals/SummarizeModal";
import { StackTraceModal } from "../modals/StackTraceModal";
import { SetupIndexModal } from "../modals/SetupIndexModal";
import { RebuildIndexModal } from "../modals/RebuildIndexModal";

// ============================================================
// ChatHeader — brand, room info, all session controls
// ============================================================

interface ChatHeaderProps {
  showUsers: boolean;
  onToggleUsers: () => void;
}

export function ChatHeader({ showUsers, onToggleUsers }: ChatHeaderProps) {
  const { state } = useSession();
  const { confirmEndChat, quitChat, setAutoApply } = useSessionActions();
  const { send } = useVSCode();
  const [showAIConfig, setShowAIConfig] = useState(false);
  const [showJira, setShowJira] = useState(false);
  const [showRoomSettings, setShowRoomSettings] = useState(false);
  const [showSummarize, setShowSummarize] = useState(false);
  const [showStackTrace, setShowStackTrace] = useState(false);
  const [showSetupIndex, setShowSetupIndex] = useState(false);
  const [showRebuildIndex, setShowRebuildIndex] = useState(false);
  const [rebuildLoading, setRebuildLoading] = useState(false);
  const [setupLoading, setSetupLoading] = useState(false);
  const [workspaceReady, setWorkspaceReady] = useState(false);
  const [workspaceRoomId, setWorkspaceRoomId] = useState("");
  const [indexProgress, setIndexProgress] = useState<{ phase: string; pct: number; detail: string } | null>(null);

  // Listen for index rebuild result
  useCommand("indexRebuildComplete", (msg) => {
    if (msg.command !== "indexRebuildComplete") return;
    setRebuildLoading(false);
  });

  // Listen for setup & index results
  useCommand("setupAndIndexComplete", (msg) => {
    if (msg.command !== "setupAndIndexComplete") return;
    const data = msg as unknown as { success: boolean; roomId?: string };
    setSetupLoading(false);
    if (data.success) {
      setWorkspaceReady(true);
      if (data.roomId) setWorkspaceRoomId(data.roomId);
    }
  });

  // Listen for index progress
  useCommand("indexProgress", (msg) => {
    if (msg.command !== "indexProgress") return;
    const p = (msg as unknown as { payload: { phase: string; filesScanned?: number; totalFiles?: number; filesIndexed?: number; symbolsExtracted?: number; embeddingsEnqueued?: number } }).payload;
    if (p.phase === "done") {
      setIndexProgress(null);
      return;
    }
    let pct = 0;
    let detail = "";
    if (p.phase === "scanning") {
      pct = p.totalFiles ? Math.round(((p.filesScanned || 0) / p.totalFiles) * 100) : 5;
      detail = `Scanning… (${p.filesScanned || 0} files)`;
    } else if (p.phase === "extracting") {
      const filePct = p.totalFiles ? (p.filesIndexed || 0) / p.totalFiles : 0;
      pct = Math.min(90, Math.max(10, Math.round(filePct * 80) + 10));
      detail = `Building index… (${p.filesIndexed || 0}/${p.totalFiles || "?"} files)`;
    } else if (p.phase === "embedding") {
      pct = 70 + Math.min(28, Math.round(((p.embeddingsEnqueued || 0) / Math.max(p.symbolsExtracted || 1, 1)) * 28));
      detail = `Embedding… (${p.embeddingsEnqueued || 0} queued)`;
    }
    setIndexProgress({ phase: p.phase, pct, detail });
  });

  const handleRebuildConfirm = useCallback(() => {
    setShowRebuildIndex(false);
    setRebuildLoading(true);
    send({ command: "rebuildIndex" });
  }, [send]);

  const handleOpenWorkspace = useCallback(() => {
    if (workspaceRoomId) {
      send({ command: "openConductorWorkspace", roomId: workspaceRoomId });
    }
  }, [send, workspaceRoomId]);

  const isHosting = state.conductorState === "Hosting";
  const isLocalSession = !!state.session?.isLocal;
  const isLead = state.permissions.sessionRole === "host";
  const roomId = state.session?.roomId || "";
  const shortRoomId = roomId.slice(0, 8);

  const handleCopyInvite = useCallback(() => {
    send({ command: "copyInviteLink" });
  }, [send]);

  const handleCopyRoomId = useCallback(() => {
    if (roomId) navigator.clipboard?.writeText(roomId);
  }, [roomId]);

  return (
    <>
      <header className="chat-header">
        {/* Row 1: Brand + Controls */}
        <div className="header-row-1">
          <div className="header-brand">
            <div className="brand-icon">
              <svg viewBox="0 0 20 20" fill="currentColor" width="12" height="12">
                <path fillRule="evenodd" d="M11.3 1.046A1 1 0 0112 2v5h4a1 1 0 01.82 1.573l-7 10A1 1 0 018 18v-5H4a1 1 0 01-.82-1.573l7-10a1 1 0 011.12-.38z" clipRule="evenodd"/>
              </svg>
            </div>
            <span className="brand-name">Conductor</span>
            {roomId && (
              <div className="room-id-group">
                <span className="room-id">{shortRoomId}</span>
                <button className="icon-btn-xs" onClick={handleCopyRoomId} title="Copy Room ID">
                  <svg viewBox="0 0 20 20" fill="currentColor" width="10" height="10">
                    <path d="M8 3a1 1 0 011-1h2a1 1 0 110 2H9a1 1 0 01-1-1z"/>
                    <path d="M6 3a2 2 0 00-2 2v11a2 2 0 002 2h8a2 2 0 002-2V5a2 2 0 00-2-2 3 3 0 01-3 3H9a3 3 0 01-3-3z"/>
                  </svg>
                </button>
              </div>
            )}
          </div>

          <div className="header-controls">
            {/* AI Config */}
            {isLead && (
              <button className="icon-btn" onClick={() => setShowAIConfig(true)} title="AI Configuration">
                <svg viewBox="0 0 20 20" fill="currentColor" width="16" height="16">
                  <path fillRule="evenodd" d="M11.3 1.046A1 1 0 0112 2v5h4a1 1 0 01.82 1.573l-7 10A1 1 0 018 18v-5H4a1 1 0 01-.82-1.573l7-10a1 1 0 011.12-.38z" clipRule="evenodd"/>
                </svg>
              </button>
            )}

            {/* Workflow */}
            <button className="icon-btn" onClick={() => send({ command: "showWorkflow" })} title="View Agent Workflows">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" width="16" height="16">
                <circle cx="5" cy="12" r="2"/><circle cx="19" cy="6" r="2"/><circle cx="19" cy="18" r="2"/>
                <line x1="7" y1="12" x2="17" y2="6"/><line x1="7" y1="12" x2="17" y2="18"/>
              </svg>
            </button>

            {/* Rebuild Index — clears cache, next AI query rebuilds lazily */}
            {isHosting && (workspaceReady || isLocalSession) && (
              <button className="icon-btn" onClick={() => setShowRebuildIndex(true)} title="Clear Code Index Cache" disabled={rebuildLoading}>
                <svg viewBox="0 0 20 20" fill="currentColor" width="16" height="16">
                  <path fillRule="evenodd" d="M4 2a1 1 0 011 1v2.101a7.002 7.002 0 0111.601 2.566 1 1 0 11-1.885.666A5.002 5.002 0 005.999 7H9a1 1 0 010 2H4a1 1 0 01-1-1V3a1 1 0 011-1zm.008 9.057a1 1 0 011.276.61A5.002 5.002 0 0014.001 13H11a1 1 0 110-2h5a1 1 0 011 1v5a1 1 0 11-2 0v-2.101a7.002 7.002 0 01-11.601-2.566 1 1 0 01.61-1.276z" clipRule="evenodd"/>
                </svg>
              </button>
            )}

            {/* Room Settings */}
            {isLead && (
              <button className="icon-btn" onClick={() => setShowRoomSettings(true)} title="Room Settings">
                <svg viewBox="0 0 20 20" fill="currentColor" width="16" height="16">
                  <path fillRule="evenodd" d="M11.49 3.17c-.38-1.56-2.6-1.56-2.98 0a1.532 1.532 0 01-2.286.948c-1.372-.836-2.942.734-2.106 2.106.54.886.061 2.042-.947 2.287-1.561.379-1.561 2.6 0 2.978a1.532 1.532 0 01.947 2.287c-.836 1.372.734 2.942 2.106 2.106a1.532 1.532 0 012.287.947c.379 1.561 2.6 1.561 2.978 0a1.533 1.533 0 012.287-.947c1.372.836 2.942-.734 2.106-2.106a1.533 1.533 0 01.947-2.287c1.561-.379 1.561-2.6 0-2.978a1.532 1.532 0 01-.947-2.287c.836-1.372-.734-2.942-2.106-2.106a1.532 1.532 0 01-2.287-.947zM10 13a3 3 0 100-6 3 3 0 000 6z" clipRule="evenodd"/>
                </svg>
              </button>
            )}

            {/* Users sidebar toggle */}
            <button className={`icon-btn ${showUsers ? "icon-btn-active" : ""}`} onClick={onToggleUsers} title="Participants">
              <svg viewBox="0 0 20 20" fill="currentColor" width="16" height="16">
                <path d="M9 6a3 3 0 11-6 0 3 3 0 016 0zM17 6a3 3 0 11-6 0 3 3 0 016 0zM12.93 17c.046-.327.07-.66.07-1a6.97 6.97 0 00-1.5-4.33A5 5 0 0119 16v1h-6.07zM6 11a5 5 0 015 5v1H1v-1a5 5 0 015-5z"/>
              </svg>
            </button>

            {/* Auto Apply toggle */}
            {isLead && (
              <div className="auto-apply-group">
                <span className="auto-label">Auto</span>
                <label className="toggle-switch toggle-sm">
                  <input type="checkbox" checked={state.autoApplyEnabled} onChange={(e) => setAutoApply(e.target.checked)} />
                  <span className="toggle-slider" />
                </label>
              </div>
            )}

            {/* SSO badge or sign-in button */}
            {state.ssoIdentity ? (
              <span className="sso-badge" title={state.ssoIdentity.email}>
                {state.ssoIdentity.name?.split(" ")[0] || state.ssoIdentity.email.split("@")[0]}
              </span>
            ) : state.enabledSSOProviders.length > 0 ? (
              <button
                className="action-btn-xs action-brand"
                onClick={() => send({ command: "ssoLogin", provider: state.enabledSSOProviders[0] })}
                title="Sign in with SSO"
              >
                Sign In
              </button>
            ) : null}

            {/* Role badge */}
            <span className="role-chip">{isLead ? "Lead" : "Member"}</span>
          </div>
        </div>

        {/* Workspace action rows — only for online mode (remote git repos) */}
        {isHosting && !isLocalSession && !workspaceReady && (
          <div className="header-workspace-row">
            <span className="workspace-row-label">Workspace</span>
            <button
              className="action-btn-xs action-brand"
              onClick={() => setShowSetupIndex(true)}
              disabled={setupLoading}
            >
              {setupLoading ? "Setting up..." : "Git Repo"}
            </button>
          </div>
        )}
        {isHosting && !isLocalSession && workspaceReady && workspaceRoomId && (
          <div className="header-workspace-row">
            <span className="workspace-row-label">Remote code</span>
            <button className="action-btn-xs action-success" onClick={handleOpenWorkspace}>
              Open Workspace
            </button>
          </div>
        )}
        {/* Index progress — only visible while rebuilding */}
        {rebuildLoading && (
          <div className="header-workspace-row">
            <span className="workspace-row-label">Clearing index cache...</span>
          </div>
        )}
        {indexProgress && (
          <div className="index-progress-bar">
            <div className="index-progress-track">
              <div className="index-progress-fill" style={{ width: indexProgress.pct + "%" }} />
            </div>
            <span className="index-progress-text">{indexProgress.detail}</span>
          </div>
        )}

        {/* Row 2: Session action bar */}
        <div className="header-row-2">
          {isHosting ? (
            <>
              <span className="live-badge"><span className="live-dot" />Live</span>
              <span className="header-divider" />
              <button className="action-btn action-brand" onClick={handleCopyInvite}>
                <svg viewBox="0 0 20 20" fill="currentColor" width="10" height="10">
                  <path d="M8 9a3 3 0 100-6 3 3 0 000 6zM8 11a6 6 0 016 6H2a6 6 0 016-6zM16 7a1 1 0 10-2 0v1h-1a1 1 0 100 2h1v1a1 1 0 102 0v-1h1a1 1 0 100-2h-1V7z"/>
                </svg>
                Invite
              </button>
              <button className="action-btn action-muted" onClick={quitChat}>Leave</button>
            </>
          ) : (
            <>
              <span className="joined-badge"><span className="joined-dot" />Joined</span>
              <button className="action-btn action-muted" onClick={quitChat}>Leave</button>
            </>
          )}

          {isLead && (
            <>
              <span className="header-divider" />
              <button className="action-btn action-brand" onClick={() => setShowSummarize(true)}>
                <svg viewBox="0 0 24 24" fill="currentColor" width="10" height="10">
                  <path d="M12 2L13.09 8.26L18 6L14.74 10.91L21 12L14.74 13.09L18 18L13.09 15.74L12 22L10.91 15.74L6 18L9.26 13.09L3 12L9.26 10.91L6 6L10.91 8.26L12 2Z"/>
                </svg>
                Distill
              </button>
              <button className="action-btn action-danger" onClick={confirmEndChat}>
                <svg viewBox="0 0 20 20" fill="currentColor" width="10" height="10">
                  <path fillRule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clipRule="evenodd"/>
                </svg>
                End
              </button>
            </>
          )}
        </div>
      </header>

      {/* Modals */}
      <AIConfigModal open={showAIConfig} onClose={() => setShowAIConfig(false)} />
      <JiraModal open={showJira} onClose={() => setShowJira(false)} />
      <RoomSettingsModal open={showRoomSettings} onClose={() => setShowRoomSettings(false)} />
      <SummarizeModal open={showSummarize} onClose={() => setShowSummarize(false)} />
      <StackTraceModal open={showStackTrace} onClose={() => setShowStackTrace(false)} />
      <SetupIndexModal open={showSetupIndex} onClose={() => setShowSetupIndex(false)} />
      <RebuildIndexModal open={showRebuildIndex} onClose={() => setShowRebuildIndex(false)} onConfirm={handleRebuildConfirm} />
    </>
  );
}
