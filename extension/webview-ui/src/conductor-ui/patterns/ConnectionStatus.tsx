import { memo } from "react";

// ============================================================
// ConnectionStatus — thin strip at top of chat area
//
// Visual states:
//   connected:    green, 2px (almost invisible)
//   reconnecting: amber, expands to 24px with text
//   disconnected: red, 24px with retry button
// ============================================================

export type ConnectionState = "connected" | "reconnecting" | "disconnected";

interface ConnectionStatusProps {
  status: ConnectionState;
  attempt?: number;
  onRetry?: () => void;
}

export const ConnectionStatus = memo(function ConnectionStatus({
  status,
  attempt = 0,
  onRetry,
}: ConnectionStatusProps) {
  return (
    <div
      className={`connection-strip connection-${status}`}
      role="status"
      aria-label={
        status === "connected" ? "Connected" :
        status === "reconnecting" ? `Reconnecting, attempt ${attempt}` :
        "Disconnected"
      }
    >
      {status === "reconnecting" && (
        <span className="connection-text">
          Reconnecting{attempt > 1 ? ` (attempt ${attempt})` : ""}...
        </span>
      )}
      {status === "disconnected" && (
        <>
          <span className="connection-text">Connection lost</span>
          {onRetry && (
            <button className="connection-retry" onClick={onRetry}>
              Retry
            </button>
          )}
        </>
      )}
    </div>
  );
});
