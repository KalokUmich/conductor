import { useCallback, useState } from "react";
import { Modal } from "../shared/Modal";
import { useVSCode, useCommand } from "../../contexts/VSCodeContext";

// ============================================================
// SetupIndexModal — clone repo, select branch, index code
// ============================================================

interface Props {
  open: boolean;
  onClose: () => void;
}

export function SetupIndexModal({ open, onClose }: Props) {
  const [repoUrl, setRepoUrl] = useState("");
  const [patToken, setPatToken] = useState("");
  const [branches, setBranches] = useState<string[]>([]);
  const [defaultBranch, setDefaultBranch] = useState("");
  const [sourceBranch, setSourceBranch] = useState("");
  const [workingBranch, setWorkingBranch] = useState("");
  const [fetching, setFetching] = useState(false);
  const [error, setError] = useState("");
  const { send } = useVSCode();

  // Listen for branch fetch results
  useCommand("remoteBranchesLoaded", (msg) => {
    if (msg.command !== "remoteBranchesLoaded") return;
    const data = msg as unknown as { branches: string[]; defaultBranch?: string; error?: string };
    setFetching(false);
    if (data.error) {
      setError("Failed to fetch branches: " + data.error);
      return;
    }
    setBranches(data.branches || []);
    if (data.defaultBranch) {
      setDefaultBranch(data.defaultBranch);
      setSourceBranch(data.defaultBranch);
    } else if (data.branches?.length > 0) {
      setSourceBranch(data.branches[0]);
    }
  });

  const handleFetchBranches = useCallback(() => {
    const url = repoUrl.trim();
    if (!url) {
      setError("Please enter a repository URL.");
      return;
    }
    setError("");
    setFetching(true);
    send({
      command: "fetchRemoteBranches",
      repoUrl: url,
      token: patToken.trim() || null,
    });
  }, [repoUrl, patToken, send]);

  const handleSetup = useCallback(() => {
    const url = repoUrl.trim();
    if (!url || !sourceBranch) {
      setError("Repository URL and source branch are required.");
      return;
    }
    onClose();
    send({
      command: "setupWorkspaceAndIndex",
      repoUrl: url,
      sourceBranch,
      workingBranch: workingBranch.trim() || null,
      token: patToken.trim() || null,
    });
  }, [repoUrl, sourceBranch, workingBranch, patToken, send, onClose]);

  return (
    <Modal open={open} onClose={onClose} title="Setup Workspace & Index">
      <div className="modal-form">
        <div className="form-field">
          <label className="form-label">Repository URL</label>
          <input
            type="text"
            className="text-input"
            value={repoUrl}
            onChange={(e) => setRepoUrl(e.target.value)}
            placeholder="https://github.com/org/repo.git"
          />
        </div>
        <div className="form-field">
          <label className="form-label">Personal Access Token</label>
          <input
            type="password"
            className="text-input"
            value={patToken}
            onChange={(e) => setPatToken(e.target.value)}
            placeholder="ghp_... (for private repos)"
          />
        </div>
        <button
          className="action-btn action-brand full-width"
          onClick={handleFetchBranches}
          disabled={fetching}
        >
          {fetching ? "Fetching..." : "Fetch Branches"}
        </button>
        <div className="form-field">
          <label className="form-label">Source Branch</label>
          <select
            className="text-input select-input"
            value={sourceBranch}
            onChange={(e) => setSourceBranch(e.target.value)}
            disabled={branches.length === 0}
          >
            {branches.length === 0 ? (
              <option value="">-- fetch branches first --</option>
            ) : (
              branches.map((b) => (
                <option key={b} value={b}>
                  {b}
                  {b === defaultBranch ? " (default)" : ""}
                </option>
              ))
            )}
          </select>
        </div>
        <div className="form-field">
          <label className="form-label">Working Branch (optional)</label>
          <input
            type="text"
            className="text-input"
            value={workingBranch}
            onChange={(e) => setWorkingBranch(e.target.value)}
            placeholder="session/{room_id} (default)"
          />
        </div>
        {error && <div className="form-error">{error}</div>}
        <div className="modal-actions">
          <button className="action-btn" onClick={onClose}>
            Cancel
          </button>
          <button
            className="action-btn action-brand"
            onClick={handleSetup}
            disabled={!repoUrl.trim() || !sourceBranch}
          >
            Setup & Index
          </button>
        </div>
      </div>
    </Modal>
  );
}
