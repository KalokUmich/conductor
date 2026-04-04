import { useCallback, useEffect, useState } from "react";
import { useVSCode, useCommand } from "../../contexts/VSCodeContext";
import { useSession } from "../../contexts/SessionContext";
import { Modal } from "../shared/Modal";
import { showToast } from "../shared/Toast";

// ============================================================
// Room Settings Modal — template, output mode, code style
// ============================================================

interface Props {
  open: boolean;
  onClose: () => void;
}

export function RoomSettingsModal({ open, onClose }: Props) {
  const { send } = useVSCode();
  const { state } = useSession();
  const [template, setTemplate] = useState("");
  const [outputMode, setOutputMode] = useState("unified_diff");
  const [codeStyle, setCodeStyle] = useState("");
  const [showPreview, setShowPreview] = useState(false);

  useEffect(() => {
    if (open && state.session?.roomId) {
      send({ command: "getRoomSettings", roomId: state.session.roomId });
    }
  }, [open, send, state.session?.roomId]);

  useCommand("roomSettingsSaved", (msg) => {
    if (msg.command !== "roomSettingsSaved") return;
    if (msg.ok) {
      showToast("Settings saved", "success");
      onClose();
    } else {
      showToast(msg.error || "Failed to save", "error");
    }
  });

  const handleSave = useCallback(() => {
    if (!state.session?.roomId) return;
    send({
      command: "saveRoomSettings",
      roomId: state.session.roomId,
      settings: { template, outputMode, codeStyleGuidelines: codeStyle },
    });
  }, [send, state.session?.roomId, template, outputMode, codeStyle]);

  return (
    <Modal open={open} onClose={onClose} title="Room Settings">
      <div className="room-settings stagger-children">
        {/* Template */}
        <div className="form-field">
          <label className="form-label">Style Template</label>
          <div className="template-row">
            <select className="config-select" value={template} onChange={(e) => setTemplate(e.target.value)}>
              <option value="">Default</option>
              <option value="clean_code">Clean Code</option>
              <option value="defensive">Defensive</option>
              <option value="minimal">Minimal</option>
            </select>
          </div>
        </div>

        {/* Output Mode */}
        <div className="form-field">
          <label className="form-label">Output Mode</label>
          <select className="config-select" value={outputMode} onChange={(e) => setOutputMode(e.target.value)}>
            <option value="unified_diff">Unified Diff</option>
            <option value="direct_repo_edits">Direct Repo Edits</option>
            <option value="plan_then_diff">Plan Then Diff</option>
          </select>
        </div>

        {/* Code Style Guidelines */}
        <div className="form-field">
          <label className="form-label">
            Code Style Guidelines
            <button className="form-toggle" onClick={() => setShowPreview(!showPreview)}>
              {showPreview ? "Edit" : "Preview"}
            </button>
          </label>
          {showPreview ? (
            <div className="code-style-preview">{codeStyle || "(empty)"}</div>
          ) : (
            <textarea
              className="text-input"
              value={codeStyle}
              onChange={(e) => setCodeStyle(e.target.value)}
              placeholder="Add custom code style guidelines..."
              rows={6}
              style={{ resize: "vertical" }}
            />
          )}
        </div>

        <button className="btn-primary btn-wide" onClick={handleSave}>Save Settings</button>
      </div>
    </Modal>
  );
}
