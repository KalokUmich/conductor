import { memo, useCallback } from "react";
import { useSession } from "../../contexts/SessionContext";
import { useWebSocketSend } from "../../hooks/useWebSocket";
import { getInitials, getAvatarColor } from "../../utils/format";
import type { UserInfo } from "../../types/messages";

// ============================================================
// UsersSidebar — participants list with online status + lead transfer
// ============================================================

export function UsersSidebar({ visible }: { visible: boolean }) {
  const { state } = useSession();
  const wsSend = useWebSocketSend();

  const isHost = state.permissions.sessionRole === "host";

  const handleTransferLead = useCallback(
    (targetUserId: string) => {
      wsSend({ type: "transfer_lead", targetUserId });
    },
    [wsSend]
  );

  if (!visible) return null;

  const users = Array.from(state.users.entries());

  return (
    <div className="users-sidebar animate-slide-left">
      <div className="users-header">
        <span className="users-title">Participants</span>
        <span className="users-count">{users.length}</span>
      </div>
      <div className="users-list">
        {users.map(([userId, info]) => (
          <UserRow
            key={userId}
            userId={userId}
            info={info}
            isMe={userId === state.session?.userId}
            isHost={isHost}
            onTransferLead={handleTransferLead}
          />
        ))}
        {users.length === 0 && (
          <div className="users-empty">No participants yet</div>
        )}
      </div>
    </div>
  );
}

const UserRow = memo(function UserRow({
  userId,
  info,
  isMe,
  isHost,
  onTransferLead,
}: {
  userId: string;
  info: UserInfo;
  isMe: boolean;
  isHost: boolean;
  onTransferLead: (userId: string) => void;
}) {
  const statusClass = info.online !== false ? "status-online" : "status-offline";

  return (
    <div className="user-row">
      <div className="user-avatar-sm" style={{ background: getAvatarColor(info.avatarColor || 0) }}>
        {getInitials(info.displayName)}
      </div>
      <div className="user-info">
        <span className="user-name">
          {info.displayName}
          {isMe && <span className="user-me-badge">(you)</span>}
        </span>
        <span className="user-role-sm">{info.role === "host" ? "Lead" : "Member"}</span>
      </div>
      <span className={`user-status-dot ${statusClass}`} />
      {/* Transfer Lead button: visible only to host, not on self */}
      {isHost && !isMe && (
        <button
          className="transfer-lead-btn"
          onClick={() => onTransferLead(userId)}
          title="Transfer lead to this user"
        >
          <svg viewBox="0 0 20 20" fill="currentColor" width="12" height="12">
            <path d="M8 9a3 3 0 100-6 3 3 0 000 6zM8 11a6 6 0 016 6H2a6 6 0 016-6zM16 7a1 1 0 10-2 0v1h-1a1 1 0 100 2h1v1a1 1 0 102 0v-1h1a1 1 0 100-2h-1V7z"/>
          </svg>
        </button>
      )}
    </div>
  );
});
