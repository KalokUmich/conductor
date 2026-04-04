/** Escape HTML special characters */
export function escapeHtml(text: string): string {
  const map: Record<string, string> = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  };
  return text.replace(/[&<>"']/g, (c) => map[c]);
}

/** Format a Unix timestamp to HH:MM */
export function formatTime(ts: number): string {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** Format a Unix timestamp to a date string for separators */
export function formatDate(ts: number): string {
  const d = new Date(ts * 1000);
  const now = new Date();
  const isToday =
    d.getDate() === now.getDate() &&
    d.getMonth() === now.getMonth() &&
    d.getFullYear() === now.getFullYear();

  const yesterday = new Date(now);
  yesterday.setDate(yesterday.getDate() - 1);
  const isYesterday =
    d.getDate() === yesterday.getDate() &&
    d.getMonth() === yesterday.getMonth() &&
    d.getFullYear() === yesterday.getFullYear();

  if (isToday) return "Today";
  if (isYesterday) return "Yesterday";
  return d.toLocaleDateString([], {
    weekday: "short",
    month: "short",
    day: "numeric",
  });
}

/** Get initials from a display name */
export function getInitials(name: string): string {
  return name
    .split(/\s+/)
    .slice(0, 2)
    .map((w) => w[0]?.toUpperCase() || "")
    .join("");
}

/** Shorten raw model IDs for display */
export function abbreviateModelId(raw: string): string {
  if (!raw) return "";
  let id = raw.replace(/^(?:[a-z]{2}\.)?anthropic\./, "");
  id = id.replace(/-\d{8}(-v\d+:\d+)?$/, "");
  id = id.replace(/-v\d+:\d+$/, "");
  return id;
}

/** Format file size in bytes to human-readable string */
export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Avatar color palette */
export const AVATAR_COLORS = [
  "var(--c-accent-600)",
  "#059669",    // emerald
  "#d97706",    // amber
  "#dc2626",    // red
  "#7c3aed",    // violet
  "#2563eb",    // blue
  "#db2777",    // pink
  "#0891b2",    // cyan
  "#65a30d",    // lime
  "#ea580c",    // orange
] as const;

export function getAvatarColor(index: number): string {
  return AVATAR_COLORS[index % AVATAR_COLORS.length];
}
