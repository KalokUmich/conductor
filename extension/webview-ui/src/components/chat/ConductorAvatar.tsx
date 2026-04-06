/**
 * Conductor AI mascot avatar — 6 expression variants with auto dark/light mode.
 *
 * Base character: friendly robot conductor with antenna, rounded head,
 * expressive eyes. Brand orange (#f19335) on dark, dark (#2d2b29) on light.
 *
 * Designed by ChatGPT 4o, integrated by Claude Opus.
 */

export type AvatarVariant =
  | "idle"
  | "thinking"
  | "reviewing"
  | "happy"
  | "concerned"
  | "error";

type Props = {
  variant?: AvatarVariant;
  size?: number;
  className?: string;
};

export function ConductorAvatar({
  variant = "idle",
  size = 24,
  className,
}: Props) {
  // Auto-detect mode from CSS custom property (set by VS Code theme)
  // Falls back to dark if not in browser context
  const colors = useThemeColors();

  const common = (
    <>
      <line x1="16" y1="4.5" x2="16" y2="7" stroke={colors.detail} strokeWidth="1.8" strokeLinecap="round" />
      <circle cx="16" cy="3" r="1.6" fill={colors.detail} className={variant === "thinking" ? "antenna-pulse" : undefined} />
      <rect x="6" y="7" width="20" height="19" rx="7" fill={colors.head} />
    </>
  );

  const faces: Record<AvatarVariant, React.ReactNode> = {
    idle: (
      <>
        <ellipse cx="12.4" cy="15" rx="1.7" ry="1.45" fill={colors.detail} className="eye-blink" />
        <ellipse cx="19.6" cy="15" rx="1.7" ry="1.45" fill={colors.detail} className="eye-blink" />
        <path d="M11.2 20.4C12.6 21.8 14 22.3 16 22.3C18 22.3 19.4 21.8 20.8 20.4" fill="none" stroke={colors.detail} strokeWidth="1.8" strokeLinecap="round" />
      </>
    ),
    thinking: (
      <>
        <ellipse cx="12.2" cy="15.2" rx="1.75" ry="0.95" fill={colors.detail} />
        <ellipse cx="19.5" cy="15" rx="1.65" ry="1.45" fill={colors.detail} />
        <path d="M12 21.1H20" fill="none" stroke={colors.detail} strokeWidth="1.8" strokeLinecap="round" />
        <circle cx="23.3" cy="10.6" r="0.95" fill={colors.detail} className="thinking-dot thinking-dot-1" />
        <circle cx="25.6" cy="12.9" r="0.8" fill={colors.detail} className="thinking-dot thinking-dot-2" />
        <circle cx="22.7" cy="14.4" r="0.65" fill={colors.detail} className="thinking-dot thinking-dot-3" />
      </>
    ),
    reviewing: (
      <>
        <ellipse cx="12.2" cy="15" rx="1.6" ry="1.45" fill={colors.detail} />
        <circle cx="19.2" cy="15" r="0.95" fill={colors.detail} />
        <circle cx="19.2" cy="15" r="2.4" fill="none" stroke={colors.detail} strokeWidth="1.4" />
        <line x1="20.9" y1="16.8" x2="22.8" y2="18.7" stroke={colors.detail} strokeWidth="1.4" strokeLinecap="round" />
        <path d="M12 20.8C13.2 21.7 14.4 22 16 22C17.6 22 18.8 21.7 20 20.8" fill="none" stroke={colors.detail} strokeWidth="1.8" strokeLinecap="round" />
      </>
    ),
    happy: (
      <>
        <circle cx="12.2" cy="15" r="1.9" fill={colors.detail} />
        <circle cx="19.7" cy="15" r="1.9" fill={colors.detail} />
        <path d="M11 19.7C12.2 22.6 13.8 23.5 16 23.5C18.2 23.5 19.8 22.6 21 19.7" fill="none" stroke={colors.detail} strokeWidth="1.8" strokeLinecap="round" />
        <path d="M23.8 10.2V12.1M22.85 11.15H24.75" fill="none" stroke={colors.detail} strokeWidth="1.4" strokeLinecap="round" />
      </>
    ),
    concerned: (
      <>
        <path d="M10.6 12.3L14 11.5" fill="none" stroke={colors.detail} strokeWidth="1.6" strokeLinecap="round" />
        <ellipse cx="12.2" cy="14.3" rx="1.55" ry="1.4" fill={colors.detail} />
        <ellipse cx="19.8" cy="15.5" rx="1.55" ry="1.4" fill={colors.detail} />
        <path d="M11.3 22C12.9 20.8 14.5 20.3 16 20.3C17.5 20.3 19.1 20.8 20.7 22" fill="none" stroke={colors.detail} strokeWidth="1.8" strokeLinecap="round" />
      </>
    ),
    error: (
      <>
        <path d="M10.8 13.7L13.6 16.5M13.6 13.7L10.8 16.5" fill="none" stroke={colors.detail} strokeWidth="1.7" strokeLinecap="round" />
        <path d="M18.4 13.7L21.2 16.5M21.2 13.7L18.4 16.5" fill="none" stroke={colors.detail} strokeWidth="1.7" strokeLinecap="round" />
        <path d="M10.7 21.1L12.8 19.9L14.9 21.1L17 19.9L19.1 21.1L21.2 19.9" fill="none" stroke={colors.detail} strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
        <path d="M23.1 10.8C24.5 10.5 25.2 11.7 24.3 12.5C23.3 13.3 22.6 14 23.8 14.7" fill="none" stroke={colors.detail} strokeWidth="1.3" strokeLinecap="round" />
      </>
    ),
  };

  return (
    <svg
      viewBox="0 0 32 32"
      width={size}
      height={size}
      className={className}
      role="img"
      aria-label={`Conductor AI — ${variant}`}
    >
      {common}
      {faces[variant]}
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Theme detection — reads from CSS custom properties
// ---------------------------------------------------------------------------

function useThemeColors(): { head: string; detail: string } {
  // In VS Code WebView, body has .vscode-dark or .vscode-light class.
  // Check at render time (no need for state — theme changes reload the WebView).
  if (typeof document === "undefined") {
    return { head: "#f19335", detail: "#ffffff" };
  }
  const isDark = document.body.classList.contains("vscode-dark")
    || document.body.classList.contains("vscode-high-contrast");
  return isDark
    ? { head: "#f19335", detail: "#ffffff" }
    : { head: "#2d2b29", detail: "#df600a" };
}
