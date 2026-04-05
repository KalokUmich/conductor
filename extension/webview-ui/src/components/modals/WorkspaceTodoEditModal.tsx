import { useCallback, useEffect, useState } from "react";
import { Modal } from "../shared/Modal";
import { useVSCode, useCommand } from "../../contexts/VSCodeContext";
import type { WorkspaceItem } from "../../types/messages";

// ============================================================
// WorkspaceTodoEditModal — edit TODO title/description in file
// ============================================================

interface Props {
  open: boolean;
  onClose: () => void;
  todo: WorkspaceItem | null;
  onSaved?: (title: string, description: string) => void;
}

export function WorkspaceTodoEditModal({ open, onClose, todo, onSaved }: Props) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const { send } = useVSCode();

  // Populate fields when opening
  useEffect(() => {
    if (open && todo) {
      setTitle(todo.title);
      setDescription(todo.description || "");
    }
  }, [open, todo]);

  // Listen for save result
  useCommand("workspaceTodoUpdated", (msg) => {
    if (msg.command !== "workspaceTodoUpdated") return;
    const data = msg as unknown as { ok: boolean };
    if (!data.ok) {
      // Could show toast — for now just log
      console.error("[WorkspaceTodoEditModal] Failed to save TODO to file");
    }
  });

  const handleSave = useCallback(() => {
    if (!todo) return;
    const newTitle = title.trim();
    if (!newTitle) return;
    const newDescription = description.trim();

    send({
      command: "updateWorkspaceTodo",
      payload: {
        filePath: todo.filePath || "",
        lineNumber: todo.lineNumber || 1,
        newTitle,
        newDescription,
        descriptionLine: todo.descriptionLine,
        commentPrefix: todo.commentPrefix || "//",
        rawTag: todo.rawTag,
        blockEndLine: todo.blockEndLine,
      },
    });

    onSaved?.(newTitle, newDescription);
    onClose();
  }, [todo, title, description, send, onSaved, onClose]);

  if (!todo) return null;

  const location = todo.relativePath
    ? `${todo.relativePath}:${todo.lineNumber}`
    : todo.filePath || "";

  return (
    <Modal open={open} onClose={onClose} title="Edit Code TODO">
      <div className="modal-form">
        <div className="form-hint">{location}</div>
        <div className="form-field">
          <input
            type="text"
            className="text-input"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleSave()}
            placeholder="Task title *"
            maxLength={200}
            autoFocus
          />
        </div>
        <div className="form-field">
          <textarea
            className="text-input textarea-input"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Description (optional) — uses // TODO_DESC: comment"
            rows={3}
          />
        </div>
        <div className="modal-actions">
          <button className="action-btn" onClick={onClose}>
            Cancel
          </button>
          <button
            className="action-btn action-brand"
            onClick={handleSave}
            disabled={!title.trim()}
          >
            Save to file
          </button>
        </div>
      </div>
    </Modal>
  );
}
