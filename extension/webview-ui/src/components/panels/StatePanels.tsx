import { useCallback, useEffect, useState } from "react";
import { useSession, useSessionActions } from "../../contexts/SessionContext";
import { useVSCode, useCommand } from "../../contexts/VSCodeContext";
import type { Room } from "../../types/messages";

// ============================================================
// StatePanels — shown when not in active session
// Matches the original chat.html premium landing pages
// ============================================================

export function StatePanels() {
  const { state } = useSession();

  switch (state.conductorState) {
    case "BackendDisconnected":
      return <DisconnectedPanel />;
    case "ReadyToHost":
      return <ReadyToHostPanel />;
    default:
      return <IdlePanel />;
  }
}

// ── Time Ago Helper ───────────────────────────────────────

function formatTimeAgo(date: Date): string {
  const seconds = Math.floor((Date.now() - date.getTime()) / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days === 1) return "yesterday";
  if (days < 7) return `${days}d ago`;
  return date.toLocaleDateString();
}

// ── Aurora Background ─────────────────────────────────────

function AuroraBackground({ muted }: { muted?: boolean }) {
  return (
    <>
      <div className="aurora-orb aurora-orb-1" style={muted ? { opacity: 0.3 } : undefined} />
      <div className="aurora-orb aurora-orb-2" style={muted ? { opacity: 0.2 } : undefined} />
      <div className="aurora-orb aurora-orb-3" />
    </>
  );
}

// ── Idle Panel ────────────────────────────────────────────

function IdlePanel() {
  const { state } = useSession();
  const { startSession, joinSession } = useSessionActions();
  const [inviteUrl, setInviteUrl] = useState("");

  const handleJoin = useCallback(() => {
    const val = inviteUrl.trim();
    if (val) joinSession(val);
  }, [joinSession, inviteUrl]);

  return (
    <div className="state-panel">
      <AuroraBackground />
      <div className="state-content" style={{ position: "relative", zIndex: 10 }}>
        {/* Brand */}
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "4px" }}>
          <h2 className="brand-wordmark">Conductor</h2>
          <p className="landing-subtitle">AI-powered collaboration for engineering teams</p>
        </div>

        <hr className="divider-gradient" />

        {/* Start button — requires SSO */}
        <div className="tooltip-group" style={{ width: "100%", maxWidth: "280px" }}>
          <button
            className="btn-primary btn-wide"
            onClick={startSession}
            disabled={!state.ssoIdentity}
            style={{ padding: "10px 0", display: "flex", alignItems: "center", justifyContent: "center", gap: "10px", opacity: state.ssoIdentity ? 1 : 0.4, cursor: state.ssoIdentity ? "pointer" : "not-allowed" }}
          >
            <svg viewBox="0 0 20 20" fill="currentColor" width="16" height="16">
              <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM9.555 7.168A1 1 0 008 8v4a1 1 0 001.555.832l3-2a1 1 0 000-1.664l-3-2z" clipRule="evenodd" />
            </svg>
            Start Session
          </button>
          {!state.ssoIdentity && <div className="tooltip-text">Sign in with SSO first</div>}
        </div>

        {/* Join row */}
        <div style={{ display: "flex", alignItems: "center", gap: "8px", width: "100%", maxWidth: "280px" }}>
          <input
            type="text"
            className="input-premium"
            style={{ flex: 1 }}
            value={inviteUrl}
            onChange={(e) => setInviteUrl(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleJoin()}
            placeholder="Room ID or invite link..."
          />
          <button className="btn-success" style={{ fontSize: "var(--text-xs)", padding: "8px 16px" }} onClick={handleJoin}>
            Join
          </button>
        </div>

        {/* SSO */}
        {state.enabledSSOProviders.length > 0 && (
          <SSOSection providers={state.enabledSSOProviders} />
        )}
      </div>
    </div>
  );
}

// ── Disconnected Panel ────────────────────────────────────

function DisconnectedPanel() {
  const { state } = useSession();
  const { joinSession, retryConnection } = useSessionActions();
  const [inviteUrl, setInviteUrl] = useState("");

  const handleJoin = useCallback(() => {
    const val = inviteUrl.trim();
    if (val) joinSession(val);
  }, [joinSession, inviteUrl]);

  return (
    <div className="state-panel">
      <AuroraBackground muted />
      <div className="state-content" style={{ position: "relative", zIndex: 10 }}>
        {/* Warning badge */}
        <div className="warning-badge">
          <svg viewBox="0 0 20 20" fill="currentColor" width="16" height="16">
            <path fillRule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clipRule="evenodd" />
          </svg>
          Local backend not running
        </div>

        <div style={{ textAlign: "center" }}>
          <h2 className="state-title">Join Only Mode</h2>
          <p className="landing-subtitle">Join other sessions directly. Start the backend to host your own.</p>
        </div>

        {/* Disabled start button with tooltip */}
        <div style={{ position: "relative", width: "100%", maxWidth: "280px" }} className="tooltip-group">
          <button className="btn-primary btn-wide" disabled style={{ opacity: 0.4, cursor: "not-allowed" }}>
            Start Session
          </button>
          <div className="tooltip-text">Backend required to host a session</div>
        </div>

        {/* Join row */}
        <div style={{ display: "flex", alignItems: "center", gap: "8px", width: "100%", maxWidth: "280px" }}>
          <input
            type="text"
            className="input-premium"
            style={{ flex: 1 }}
            value={inviteUrl}
            onChange={(e) => setInviteUrl(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleJoin()}
            placeholder="Room ID or invite link..."
          />
          <button className="btn-success" style={{ fontSize: "var(--text-xs)", padding: "8px 16px" }} onClick={handleJoin}>
            Join
          </button>
        </div>

        {/* Retry link */}
        <button className="retry-link" onClick={retryConnection}>
          <svg viewBox="0 0 20 20" fill="currentColor" width="12" height="12">
            <path fillRule="evenodd" d="M4 2a1 1 0 011 1v2.101a7.002 7.002 0 0111.601 2.566 1 1 0 11-1.885.666A5.002 5.002 0 005.999 7H9a1 1 0 010 2H4a1 1 0 01-1-1V3a1 1 0 011-1zm.008 9.057a1 1 0 011.276.61A5.002 5.002 0 0014.001 13H11a1 1 0 110-2h5a1 1 0 011 1v5a1 1 0 11-2 0v-2.101a7.002 7.002 0 01-11.601-2.566 1 1 0 01.61-1.276z" clipRule="evenodd" />
          </svg>
          Retry connection
        </button>

        {/* SSO */}
        {state.enabledSSOProviders.length > 0 && (
          <SSOSection providers={state.enabledSSOProviders} />
        )}
      </div>
    </div>
  );
}

// ── Ready To Host Panel ───────────────────────────────────

interface LocalSessionInfo {
  roomId: string;
  workspacePath: string;
  workspaceName: string;
  displayName: string;
  ssoEmail: string;
  createdAt: string;
  lastActiveAt: string;
  messageCount: number;
}

function ReadyToHostPanel() {
  const { startSession, joinSession } = useSessionActions();
  const { send, onAny } = useVSCode();
  const { state } = useSession();
  const [inviteUrl, setInviteUrl] = useState("");
  const [mode, setMode] = useState<"local" | "online">("local");
  const [onlineRooms, setOnlineRooms] = useState<Room[]>([]);
  const [localSessions, setLocalSessions] = useState<LocalSessionInfo[]>([]);

  useCommand("onlineRoomsList", (msg) => {
    if (msg.command !== "onlineRoomsList") return;
    setOnlineRooms(msg.rooms);
  });

  // Listen for local sessions list
  useEffect(() => {
    return onAny((msg) => {
      const cmd = (msg as unknown as { command: string }).command;
      if (cmd === "localSessionsList") {
        setLocalSessions((msg as unknown as { sessions: LocalSessionInfo[] }).sessions || []);
      }
    });
  }, [onAny]);

  useEffect(() => {
    const email = state.ssoIdentity?.email || "";
    send({ command: "getOnlineRooms", email } as never);
    // Load local session history
    send({ command: "getLocalSessions", email } as never);
  }, [send, state.ssoIdentity?.email]);

  const handleStartLocal = useCallback(() => {
    // Only send startSession — it handles resetSession + workspace registration + upsertSession internally
    startSession();
  }, [startSession]);

  const handleDeleteSession = useCallback((roomId: string) => {
    send({ command: "deleteLocalSession", roomId } as never);
  }, [send]);

  const handleJoin = useCallback(() => {
    const val = inviteUrl.trim();
    if (val) joinSession(val);
  }, [joinSession, inviteUrl]);

  return (
    <div className="state-panel">
      <AuroraBackground />
      <div className="state-content" style={{ position: "relative", zIndex: 10 }}>
        {/* Connected badge */}
        <div style={{ display: "flex", alignItems: "center", gap: "6px", padding: "6px 14px", borderRadius: "var(--radius-full)", border: "1px solid rgba(52,211,153,0.2)", background: "rgba(52,211,153,0.06)", fontSize: "var(--text-xs)", fontWeight: 500, color: "#34d399" }}>
          <span style={{ width: "6px", height: "6px", borderRadius: "50%", background: "#34d399", boxShadow: "0 0 6px rgba(52,211,153,0.6)" }} />
          Backend connected
        </div>

        {/* Brand */}
        <div style={{ textAlign: "center" }}>
          <h2 className="brand-wordmark">Conductor</h2>
        </div>

        {/* SSO — always shown so user can login */}
        <SSOSection providers={state.enabledSSOProviders} />

        {/* Mode tabs */}
        <div className="mode-tabs">
          <button className={`mode-tab ${mode === "local" ? "mode-active" : ""}`} onClick={() => setMode("local")}>Local</button>
          <button className={`mode-tab ${mode === "online" ? "mode-active" : ""}`} onClick={() => setMode("online")}>Online</button>
        </div>

        {mode === "local" ? (
          <div className="ready-section" style={{ width: "100%", gap: "var(--space-3)", display: "flex", flexDirection: "column" }}>
            {/* New Session — requires SSO */}
            <div className="tooltip-group">
              <button
                className="btn-primary btn-wide"
                onClick={handleStartLocal}
                disabled={!state.ssoIdentity}
                style={{ padding: "10px 0", display: "flex", alignItems: "center", justifyContent: "center", gap: "10px", opacity: state.ssoIdentity ? 1 : 0.4, cursor: state.ssoIdentity ? "pointer" : "not-allowed" }}
              >
                <svg viewBox="0 0 20 20" fill="currentColor" width="16" height="16">
                  <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM9.555 7.168A1 1 0 008 8v4a1 1 0 001.555.832l3-2a1 1 0 000-1.664l-3-2z" clipRule="evenodd" />
                </svg>
                New Session
              </button>
              {!state.ssoIdentity && <div className="tooltip-text">Sign in with SSO first</div>}
            </div>

            {/* Recent local sessions */}
            {localSessions.length > 0 && (
              <div className="local-sessions">
                <h3 className="section-label">Recent Sessions</h3>
                <div className="local-session-list">
                  {localSessions.map((s) => {
                    const isStale = Date.now() - new Date(s.lastActiveAt).getTime() > 7 * 24 * 60 * 60 * 1000;
                    const lastActive = new Date(s.lastActiveAt);
                    const timeAgo = formatTimeAgo(lastActive);
                    // Check if this session's workspace matches current
                    const currentWs = (window as unknown as Record<string, unknown>).__conductorCurrentWorkspace as string | undefined;
                    const isDifferentWorkspace = currentWs && s.workspacePath && s.workspacePath !== currentWs;
                    return (
                      <div key={s.roomId} className={`local-session-card ${isStale ? "session-stale" : ""}`}>
                        <div className="session-card-main" onClick={() => send({ command: "rejoinRoom", roomId: s.roomId } as never)}>
                          <div className="session-card-name">{s.displayName}</div>
                          <div className="session-card-meta">
                            <span className={`session-workspace ${isDifferentWorkspace ? "session-different-ws" : ""}`}>
                              {isDifferentWorkspace ? "↗ " : ""}{s.workspaceName}
                            </span>
                            <span className="session-dot-sep">·</span>
                            <span>{s.messageCount} msgs</span>
                            <span className="session-dot-sep">·</span>
                            <span>{timeAgo}</span>
                          </div>
                          {isStale && (
                            <div className="session-stale-badge">Stale — consider deleting</div>
                          )}
                        </div>
                        <button
                          className="session-delete-btn"
                          onClick={(e) => { e.stopPropagation(); handleDeleteSession(s.roomId); }}
                          title="Delete session and chat history"
                        >
                          ×
                        </button>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        ) : (
          <>
            {/* Room list — always joinable */}
            {onlineRooms.length > 0 && (
              <div className="room-list" style={{ width: "100%" }}>
                {onlineRooms.map((room) => (
                  <RoomCard key={room.roomId} room={room} onJoin={() => joinSession(room.roomId)} />
                ))}
              </div>
            )}

            {/* Join by invite — always available */}
            <div style={{ display: "flex", alignItems: "center", gap: "8px", width: "100%", maxWidth: "280px" }}>
              <input
                type="text"
                className="input-premium"
                style={{ flex: 1 }}
                value={inviteUrl}
                onChange={(e) => setInviteUrl(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleJoin()}
                placeholder="Room ID or invite link..."
              />
              <button className="btn-success" style={{ fontSize: "var(--text-xs)", padding: "8px 16px" }} onClick={handleJoin}>
                Join
              </button>
            </div>

            {/* Host New Room — requires SSO */}
            {state.ssoIdentity && (
              <button className="btn-primary btn-wide" onClick={startSession}>
                Host New Room
              </button>
            )}
          </>
        )}
      </div>
    </div>
  );
}

// ── SSO Section (3 states: signin, pending, done) ─────────

function SSOSection({ providers }: { providers: string[] }) {
  const { send } = useVSCode();
  const { state } = useSession();
  const { ssoUIState, ssoPending, ssoIdentity, ssoProvider } = state;

  // State: Signed In
  if (ssoUIState === "done" && ssoIdentity) {
    const providerBadge = ssoProvider === "aws" ? "☁ AWS" : ssoProvider === "google" ? "G Google" : ssoProvider || "";
    return (
      <div className="sso-done-badge" style={{ width: "100%", maxWidth: "280px" }}>
        <span className="sso-provider-pill">{providerBadge}</span>
        <span className="sso-email">{ssoIdentity.email}</span>
        <button className="sso-clear-btn" onClick={() => send({ command: "ssoClearCache" })} title="Clear identity">✕</button>
      </div>
    );
  }

  // State: Pending (polling)
  if (ssoUIState === "pending" && ssoPending) {
    return (
      <div className="sso-pending-badge" style={{ width: "100%", maxWidth: "280px" }}>
        <div className="sso-spinner" />
        <span className="sso-pending-text">Waiting for SSO...</span>
        {ssoPending.userCode && <code className="sso-user-code">{ssoPending.userCode}</code>}
        <button className="sso-cancel-btn" onClick={() => send({ command: "ssoCancel" })}>Cancel</button>
      </div>
    );
  }

  // State: Sign-In buttons
  if (providers.length === 0) return null;

  return (
    <div style={{ display: "flex", gap: "8px", width: "100%", maxWidth: "280px" }}>
      {providers.map((p) => (
        <button
          key={p}
          className="btn-sso"
          style={{ flex: 1 }}
          onClick={() => send({ command: "ssoLogin", provider: p })}
        >
          {p === "aws" ? "☁ AWS SSO" : p === "google" ? "G Google" : p.charAt(0).toUpperCase() + p.slice(1)}
        </button>
      ))}
    </div>
  );
}

// ── Room Card ─────────────────────────────────────────────

function RoomCard({ room, onJoin }: { room: Room; onJoin: () => void }) {
  return (
    <button className="room-card" onClick={onJoin}>
      <div className="room-info">
        <span className="room-host">{room.hostName || room.hostEmail || "Room"}</span>
        <span className="room-meta">{room.userCount || 0} user{(room.userCount || 0) !== 1 ? "s" : ""}</span>
      </div>
      <div className={`room-status ${room.status === "active" ? "status-active" : ""}`}>
        <span className="status-dot" />{room.status || "idle"}
      </div>
    </button>
  );
}
