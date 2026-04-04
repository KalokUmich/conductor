import { memo } from "react";
import { useSession } from "../../contexts/SessionContext";
import { getInitials, getAvatarColor } from "../../utils/format";
import type { UserInfo } from "../../types/messages";

// ============================================================
// UsersSidebar — participants list with online status
// ============================================================

export function UsersSidebar({ visible }: { visible: boolean }) {
  const { state } = useSession();

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
          <UserRow key={userId} userId={userId} info={info} isMe={userId === state.session?.userId} />
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
}: {
  userId: string;
  info: UserInfo;
  isMe: boolean;
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
    </div>
  );
});
