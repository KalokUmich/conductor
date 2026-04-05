import { Modal } from "../shared/Modal";

// ============================================================
// RebuildIndexModal — confirm before deleting & rebuilding index
// ============================================================

interface Props {
  open: boolean;
  onClose: () => void;
  onConfirm: () => void;
}

export function RebuildIndexModal({ open, onClose, onConfirm }: Props) {
  return (
    <Modal open={open} onClose={onClose} title="Rebuild Workspace Index?">
      <div className="modal-form">
        <p className="form-description">
          This will delete all cached embeddings, LSP data, and file metadata, then
          re-scan and re-embed the workspace. Embedding API calls will be re-issued.
          This may take a while depending on workspace size.
        </p>
        <div className="modal-actions">
          <button className="action-btn" onClick={onClose}>
            Cancel
          </button>
          <button className="action-btn action-warn" onClick={onConfirm}>
            Rebuild
          </button>
        </div>
      </div>
    </Modal>
  );
}
