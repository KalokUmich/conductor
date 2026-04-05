import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ALL_COMMANDS, type SlashCommand } from "../../utils/slashCommands";

// ============================================================
// CommandPalette — Cmd+K fuzzy search across all commands
//
// Inspired by VS Code, Linear, and Superhuman command palettes.
// Material glass styling, top-center positioning.
// ============================================================

interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
  onSelect: (command: SlashCommand) => void;
}

export const CommandPalette = memo(function CommandPalette({
  open,
  onClose,
  onSelect,
}: CommandPaletteProps) {
  const [query, setQuery] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const [selectedIndex, setSelectedIndex] = useState(0);

  // Fuzzy filter commands
  const filtered = useMemo(() => {
    if (!query.trim()) return ALL_COMMANDS;
    const q = query.toLowerCase();
    return ALL_COMMANDS.filter(
      (cmd) =>
        cmd.name.toLowerCase().includes(q) ||
        cmd.description.toLowerCase().includes(q)
    );
  }, [query]);

  // Reset on open
  useEffect(() => {
    if (open) {
      setQuery("");
      setSelectedIndex(0);
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  // Clamp selected index
  useEffect(() => {
    if (selectedIndex >= filtered.length) {
      setSelectedIndex(Math.max(0, filtered.length - 1));
    }
  }, [filtered.length, selectedIndex]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        setSelectedIndex((i) => Math.min(i + 1, filtered.length - 1));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setSelectedIndex((i) => Math.max(i - 1, 0));
      } else if (e.key === "Enter" && filtered.length > 0) {
        e.preventDefault();
        onSelect(filtered[selectedIndex]);
        onClose();
      }
    },
    [filtered, selectedIndex, onClose, onSelect]
  );

  if (!open) return null;

  const categoryIcon = (cat: string) =>
    cat === "action" ? "⚡" : cat === "agent" ? "🤖" : "📎";

  return (
    <div className="cmd-palette-overlay" onClick={onClose}>
      <div className="cmd-palette" onClick={(e) => e.stopPropagation()} onKeyDown={handleKeyDown}>
        <div className="cmd-palette-input-row">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <circle cx="11" cy="11" r="8" />
            <path d="m21 21-4.35-4.35" />
          </svg>
          <input
            ref={inputRef}
            className="cmd-palette-input"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Type a command..."
            aria-label="Command palette search"
          />
          <kbd className="cmd-palette-esc">Esc</kbd>
        </div>
        <div className="cmd-palette-list" role="listbox">
          {filtered.map((cmd, i) => (
            <button
              key={cmd.name}
              className={`cmd-palette-item ${i === selectedIndex ? "cmd-palette-active" : ""}`}
              onClick={() => { onSelect(cmd); onClose(); }}
              role="option"
              aria-selected={i === selectedIndex}
            >
              <span className="cmd-palette-icon">{categoryIcon(cmd.category)}</span>
              <span className="cmd-palette-name">{cmd.name}</span>
              <span className="cmd-palette-desc">{cmd.description}</span>
            </button>
          ))}
          {filtered.length === 0 && (
            <div className="cmd-palette-empty">No commands found</div>
          )}
        </div>
      </div>
    </div>
  );
});
