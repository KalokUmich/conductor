import { useCallback, useEffect, useState } from "react";
import { useVSCode, useCommand } from "../../contexts/VSCodeContext";
import { Modal } from "../shared/Modal";
import { showToast } from "../shared/Toast";

// ============================================================
// Jira Modal — ticket creation, project/issue type selection
// ============================================================

interface Props {
  open: boolean;
  onClose: () => void;
}

interface JiraProject {
  key: string;
  name: string;
}

export function JiraModal({ open, onClose }: Props) {
  const { send } = useVSCode();
  const [connected, setConnected] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [projectKey, setProjectKey] = useState("");
  const [issueTypes, setIssueTypes] = useState<Array<{ name: string; id: string }>>([]);
  const [selectedIssueType, setSelectedIssueType] = useState("");
  const [summary, setSummary] = useState("");
  const [description, setDescription] = useState("");
  const [priorities, setPriorities] = useState<Array<{ name: string }>>([]);
  const [selectedPriority, setSelectedPriority] = useState("");
  const [components, setComponents] = useState<string[]>([]);
  const [selectedComponents, setSelectedComponents] = useState<string[]>([]);
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    if (open) {
      send({ command: "jiraCheckStatus" });
    }
  }, [open, send]);

  useCommand("jiraConnected", (msg) => {
    if (msg.command !== "jiraConnected") return;
    setConnected(true);
    setConnecting(false);
    send({ command: "jiraCheckStatus" });
  });

  useCommand("jiraDisconnected", () => {
    setConnected(false);
  });

  useCommand("jiraIssueTypes", (msg) => {
    if (msg.command !== "jiraIssueTypes") return;
    setIssueTypes(msg.types);
    if (msg.types.length > 0) setSelectedIssueType(msg.types[0].name);
  });

  useCommand("jiraCreateMeta", (msg) => {
    if (msg.command !== "jiraCreateMeta") return;
    if (msg.priorities) setPriorities(msg.priorities);
    if (msg.components) setComponents(msg.components.map((c) => c.name));
  });

  useCommand("jiraIssueCreated", (msg) => {
    if (msg.command !== "jiraIssueCreated") return;
    setCreating(false);
    showToast(`${msg.key} created!`, "success");
    setSummary("");
    setDescription("");
    onClose();
  });

  useCommand("jiraError", (msg) => {
    if (msg.command !== "jiraError") return;
    setCreating(false);
    showToast(msg.error || "Jira error", "error");
  });

  const handleConnect = useCallback(() => {
    setConnecting(true);
    send({ command: "jiraConnect" });
  }, [send]);

  const handleProjectChange = useCallback(
    (key: string) => {
      setProjectKey(key);
      send({ command: "jiraGetIssueTypes", projectKey: key });
      send({ command: "jiraGetCreateMeta", projectKey: key });
    },
    [send]
  );

  const handleCreate = useCallback(() => {
    if (!summary.trim() || !projectKey) return;
    setCreating(true);
    send({
      command: "jiraCreateIssue",
      projectKey,
      summary: summary.trim(),
      description: description.trim(),
      issueType: selectedIssueType || "Task",
      priority: selectedPriority || undefined,
      components: selectedComponents.length > 0 ? selectedComponents : undefined,
    });
  }, [send, projectKey, summary, description, selectedIssueType, selectedPriority, selectedComponents]);

  const toggleComponent = useCallback((comp: string) => {
    setSelectedComponents((prev) =>
      prev.includes(comp) ? prev.filter((c) => c !== comp) : [...prev, comp]
    );
  }, []);

  return (
    <Modal open={open} onClose={onClose} title="Create Jira Ticket">
      {!connected ? (
        <div className="jira-auth">
          <p className="config-hint" style={{ textAlign: "center", marginBottom: "var(--space-4)" }}>
            Connect your Jira account to create tickets
          </p>
          <button
            className="btn-primary btn-wide"
            onClick={handleConnect}
            disabled={connecting}
          >
            {connecting ? "Connecting..." : "Connect to Jira"}
          </button>
        </div>
      ) : (
        <div className="jira-form stagger-children">
          {/* Project */}
          <div className="form-field">
            <label className="form-label">Project Key</label>
            <input
              type="text"
              className="text-input"
              value={projectKey}
              onChange={(e) => handleProjectChange(e.target.value.toUpperCase())}
              placeholder="e.g. PROJ"
            />
          </div>

          {/* Issue Type */}
          {issueTypes.length > 0 && (
            <div className="form-field">
              <label className="form-label">Issue Type</label>
              <select
                className="config-select"
                value={selectedIssueType}
                onChange={(e) => setSelectedIssueType(e.target.value)}
              >
                {issueTypes.map((t) => (
                  <option key={t.id} value={t.name}>{t.name}</option>
                ))}
              </select>
            </div>
          )}

          {/* Summary */}
          <div className="form-field">
            <label className="form-label">Summary</label>
            <input
              type="text"
              className="text-input"
              value={summary}
              onChange={(e) => setSummary(e.target.value)}
              placeholder="Brief description of the issue"
            />
          </div>

          {/* Description */}
          <div className="form-field">
            <label className="form-label">Description</label>
            <textarea
              className="text-input"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Detailed description..."
              rows={4}
              style={{ resize: "vertical" }}
            />
          </div>

          {/* Priority */}
          {priorities.length > 0 && (
            <div className="form-field">
              <label className="form-label">Priority</label>
              <select
                className="config-select"
                value={selectedPriority}
                onChange={(e) => setSelectedPriority(e.target.value)}
              >
                <option value="">Default</option>
                {priorities.map((p) => (
                  <option key={p.name} value={p.name}>{p.name}</option>
                ))}
              </select>
            </div>
          )}

          {/* Components */}
          {components.length > 0 && (
            <div className="form-field">
              <label className="form-label">Components</label>
              <div className="component-chips">
                {components.map((c) => (
                  <button
                    key={c}
                    className={`chip ${selectedComponents.includes(c) ? "chip-selected" : ""}`}
                    onClick={() => toggleComponent(c)}
                  >
                    {c}
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* Create button */}
          <button
            className="btn-primary btn-wide"
            onClick={handleCreate}
            disabled={creating || !summary.trim() || !projectKey}
          >
            {creating ? "Creating..." : "Create Ticket"}
          </button>
        </div>
      )}
    </Modal>
  );
}
