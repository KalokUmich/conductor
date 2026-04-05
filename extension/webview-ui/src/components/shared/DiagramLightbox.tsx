import { useCallback, useEffect } from "react";

// ============================================================
// DiagramLightbox — fullscreen overlay for mermaid diagrams
// ============================================================

interface Props {
  svgHtml: string;
  onClose: () => void;
}

export function DiagramLightbox({ svgHtml, onClose }: Props) {
  // Close on Escape
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  // Close on backdrop click
  const handleBackdrop = useCallback(
    (e: React.MouseEvent) => {
      if (e.target === e.currentTarget) onClose();
    },
    [onClose]
  );

  return (
    <div className="diagram-lightbox" onClick={handleBackdrop}>
      <div className="diagram-lightbox-topbar">
        <button className="diagram-lightbox-close" onClick={onClose}>
          ×
        </button>
      </div>
      <div className="diagram-lightbox-body">
        <div
          className="diagram-lightbox-content"
          dangerouslySetInnerHTML={{ __html: svgHtml }}
        />
      </div>
    </div>
  );
}
