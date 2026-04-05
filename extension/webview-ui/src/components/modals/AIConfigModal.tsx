import { useCallback, useEffect, useState } from "react";
import { useVSCode } from "../../contexts/VSCodeContext";
import { useSession } from "../../contexts/SessionContext";
import { Modal } from "../shared/Modal";
import { abbreviateModelId } from "../../utils/format";

// ============================================================
// AI Config Modal — Model selection, explorer toggle
// ============================================================

interface Props {
  open: boolean;
  onClose: () => void;
}

interface AIStatusData {
  summary_enabled?: boolean;
  active_provider?: string;
  active_model?: string;
  providers?: Array<{ name: string; enabled: boolean; healthy: boolean }>;
  models?: Array<{ id: string; provider: string; display_name: string; available: boolean; classifier?: boolean }>;
  classifier_enabled?: boolean;
  active_classifier?: string;
  error?: string;
}

export function AIConfigModal({ open, onClose }: Props) {
  const { send, onAny } = useVSCode();
  const { state: sessionState } = useSession();
  const [status, setStatus] = useState<AIStatusData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [activeTab, setActiveTab] = useState<"models" | "swarm">("models");
  const [selectedModel, setSelectedModel] = useState("");
  const [explorerEnabled, setExplorerEnabled] = useState(true);
  const [selectedExplorer, setSelectedExplorer] = useState("");

  // Listen for aiStatus response (command name from extension is 'aiStatus')
  useEffect(() => {
    const unsub = onAny((msg) => {
      const cmd = (msg as unknown as { command: string }).command;
      if (cmd === "aiStatus") {
        setLoading(false);
        const data = (msg as unknown as { data?: AIStatusData }).data;
        if (data?.error) {
          setError(data.error);
          return;
        }
        if (data) {
          setStatus(data);
          setError("");
          if (data.active_model) setSelectedModel(data.active_model);
        }
      }
      if (cmd === "setAiModelResult") {
        const data = (msg as unknown as { data?: { success: boolean; active_model?: string } }).data;
        if (data?.success && data.active_model) {
          setSelectedModel(data.active_model);
        }
      }
    });
    return unsub;
  }, [onAny]);

  // Fetch status on open
  useEffect(() => {
    if (open) {
      setLoading(true);
      setError("");
      send({ command: "getAiStatus" });
    }
  }, [open, send]);

  const handleModelChange = useCallback(
    (modelId: string) => {
      setSelectedModel(modelId);
      send({ command: "setAiModel", modelId });
    },
    [send]
  );

  const handleRefresh = useCallback(() => {
    setLoading(true);
    setError("");
    send({ command: "getAiStatus" });
  }, [send]);

  const availableModels = status?.models?.filter((m) => m.available) || [];
  const explorerModels = availableModels.filter((m) => !m.classifier);
  const providers = status?.providers || [];

  return (
    <Modal open={open} onClose={onClose} title="AI Configuration">
      <div className="ai-config">
        {/* Tab bar */}
        <div className="ai-config-tabs">
          <button className={`ai-config-tab ${activeTab === "models" ? "tab-selected" : ""}`} onClick={() => setActiveTab("models")}>Models</button>
          <button className={`ai-config-tab ${activeTab === "swarm" ? "tab-selected" : ""}`} onClick={() => setActiveTab("swarm")}>Agent Swarm</button>
        </div>

        {/* Error */}
        {error && (
          <div style={{ padding: "8px 12px", borderRadius: "var(--radius-md)", background: "rgba(248,113,113,0.08)", border: "1px solid rgba(248,113,113,0.2)", color: "var(--c-error)", fontSize: "var(--text-xs)" }}>
            {error}
          </div>
        )}

        {/* Loading */}
        {loading && (
          <div style={{ display: "flex", alignItems: "center", justifyContent: "center", padding: "24px", gap: "8px" }}>
            <div className="animate-spin" style={{ width: "16px", height: "16px", border: "2px solid var(--c-accent-400)", borderTopColor: "transparent", borderRadius: "50%" }} />
            <span style={{ color: "var(--c-text-tertiary)", fontSize: "var(--text-sm)" }}>Loading...</span>
          </div>
        )}

        {!loading && activeTab === "models" && (
          <div className="ai-config-section stagger-children">
            {/* AI Status */}
            <div className="config-card">
              <div className="config-card-header">
                <span className="config-icon config-icon-brand">⚡</span>
                <div>
                  <span className="config-label">AI Distillation</span>
                  <span className="config-hint">{status?.summary_enabled ? "Enabled" : "Disabled"}</span>
                </div>
                <span style={{ marginLeft: "auto", padding: "2px 10px", borderRadius: "var(--radius-full)", fontSize: "var(--text-xs)", fontWeight: 500, background: status?.summary_enabled ? "rgba(74,222,128,0.12)" : "rgba(248,113,113,0.12)", color: status?.summary_enabled ? "var(--c-success)" : "var(--c-error)" }}>
                  {status?.summary_enabled ? "ON" : "OFF"}
                </span>
              </div>
            </div>

            {/* Active Model */}
            <div className="config-card">
              <div className="config-card-header">
                <span className="config-icon config-icon-accent">🧠</span>
                <div>
                  <span className="config-label">Active Model</span>
                  <span className="config-hint">For summarization and code explanation</span>
                </div>
              </div>
              <select className="config-select" value={selectedModel} onChange={(e) => handleModelChange(e.target.value)}>
                <option value="">Select a model...</option>
                {availableModels.map((m) => (
                  <option key={m.id} value={m.id}>{abbreviateModelId(m.id)} ({m.provider})</option>
                ))}
              </select>
            </div>

            {/* Explorer Model */}
            <div className="config-card">
              <div className="config-card-header">
                <span className="config-icon config-icon-accent">📖</span>
                <div>
                  <span className="config-label">Explorer Model</span>
                  <span className="config-hint">Used by specialist sub-agents</span>
                </div>
                <label className="toggle-switch" style={{ marginLeft: "auto" }}>
                  <input type="checkbox" checked={explorerEnabled} onChange={() => {
                    const next = !explorerEnabled;
                    setExplorerEnabled(next);
                    send({ command: "setExplorer", explorer: next ? selectedExplorer : "", enabled: next });
                  }} />
                  <span className="toggle-slider" />
                </label>
              </div>
              {explorerEnabled && (
                <select className="config-select" value={selectedExplorer} onChange={(e) => { setSelectedExplorer(e.target.value); send({ command: "setExplorer", explorer: e.target.value, enabled: true }); }}>
                  <option value="">Select explorer...</option>
                  {explorerModels.map((m) => (
                    <option key={m.id} value={m.id}>{abbreviateModelId(m.id)}</option>
                  ))}
                </select>
              )}
            </div>

            {/* Provider Health */}
            {providers.length > 0 && (
              <div className="config-card">
                <span className="config-label" style={{ marginBottom: "8px", display: "block" }}>Provider Health</span>
                <div className="provider-list">
                  {providers.map((p) => (
                    <div key={p.name} className="provider-row">
                      <span className={`provider-dot ${p.healthy ? "dot-healthy" : "dot-error"}`} />
                      <span className="provider-name">{p.name}</span>
                      <span className="provider-status">{p.enabled ? (p.healthy ? "healthy" : "unhealthy") : "disabled"}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {!loading && activeTab === "swarm" && (
          <AgentSwarmTab
            backendUrl={sessionState.session?.backendUrl || ""}
            selectedModel={selectedModel}
            explorerModels={explorerModels}
            selectedExplorer={selectedExplorer}
            onExplorerChange={(v) => { setSelectedExplorer(v); send({ command: "setExplorer", explorer: v, enabled: true } as never); }}
            onOpenDiagram={() => { send({ command: "showWorkflow" }); onClose(); }}
          />
        )}

        {/* Footer */}
        <div className="ai-config-footer">
          <button className="btn-secondary btn-sm" onClick={handleRefresh}>↻ Refresh</button>
        </div>
      </div>
    </Modal>
  );
}

// ============================================================
// Agent Swarm Tab — loads from /api/brain/swarms
// ============================================================

interface SwarmAgent {
  name: string;
  role?: string;
  tools?: string[];
}

interface SwarmData {
  name: string;
  description: string;
  type: string;
  mode?: string;
  agents: SwarmAgent[];
  arbitrator?: string;
}

function AgentSwarmTab({
  backendUrl,
  selectedModel,
  explorerModels,
  selectedExplorer,
  onExplorerChange,
  onOpenDiagram,
}: {
  backendUrl?: string;
  selectedModel: string;
  explorerModels: Array<{ id: string; provider: string; display_name: string }>;
  selectedExplorer: string;
  onExplorerChange: (v: string) => void;
  onOpenDiagram: () => void;
}) {
  const [swarms, setSwarms] = useState<SwarmData[]>([]);
  const [selectedSwarm, setSelectedSwarm] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // Load swarms from backend using live session URL
  useEffect(() => {
    if (!backendUrl) return;
    fetch(`${backendUrl}/api/brain/swarms`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((data) => {
        const all = [...(data.specialized_brains || []), ...(data.swarms || [])];
        setSwarms(all);
        if (all.length > 0) setSelectedSwarm(all[0].name);
        setLoading(false);
      })
      .catch((e) => {
        setError(String(e));
        setLoading(false);
      });
  }, [backendUrl]);

  const currentSwarm = swarms.find((s) => s.name === selectedSwarm);

  if (loading) {
    return (
      <div className="ai-config-section" style={{ display: "flex", alignItems: "center", justifyContent: "center", padding: "40px 0", gap: "12px" }}>
        <div className="animate-spin" style={{ width: "20px", height: "20px", border: "2px solid var(--c-accent-400)", borderTopColor: "transparent", borderRadius: "50%" }} />
        <span style={{ color: "var(--c-text-tertiary)", fontSize: "var(--text-sm)" }}>Loading agent swarm...</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="ai-config-section">
        <div style={{ padding: "16px", borderRadius: "var(--radius-lg)", background: "rgba(248,113,113,0.08)", border: "1px solid rgba(248,113,113,0.2)", color: "var(--c-error)", fontSize: "var(--text-sm)", textAlign: "center" }}>
          Failed to load workflows
        </div>
      </div>
    );
  }

  return (
    <div className="ai-config-section" style={{ marginTop: "var(--space-3)" }}>
      {/* Workflow selector */}
      <div className="form-field">
        <label className="form-label">Workflow</label>
        <select className="config-select" value={selectedSwarm} onChange={(e) => setSelectedSwarm(e.target.value)}>
          {swarms.map((s) => (
            <option key={s.name} value={s.name}>{s.name} — {s.description}</option>
          ))}
        </select>
      </div>

      {/* Swarm detail */}
      {currentSwarm && (
        <div className="config-card" style={{ background: "linear-gradient(135deg, var(--c-tint-bg), var(--c-surface-card))", borderColor: "var(--c-tint-bg)" }}>
          <p style={{ fontSize: "var(--text-sm)", color: "var(--c-text-secondary)", lineHeight: "var(--leading-relaxed)" }}>{currentSwarm.description}</p>
          <p style={{ fontSize: "var(--text-xs)", color: "var(--c-text-tertiary)", marginTop: "8px" }}>
            <span style={{ color: currentSwarm.type === "brain" ? "var(--c-success)" : "var(--c-warning)" }}>
              {currentSwarm.type === "brain" ? "Specialized Brain" : "Swarm"}
            </span>
            {" · "}{currentSwarm.mode || "pipeline"}{" · "}{currentSwarm.agents.length} agents{currentSwarm.arbitrator ? " + arbitrator" : ""}
          </p>
          <button className="retry-link" style={{ marginTop: "12px" }} onClick={onOpenDiagram}>
            View interactive diagram →
          </button>

          {/* Agents */}
          {currentSwarm.agents.length > 0 && (
            <div style={{ marginTop: "12px", display: "flex", flexDirection: "column", gap: "4px" }}>
              {currentSwarm.agents.map((a) => (
                <div key={a.name} style={{ display: "flex", alignItems: "center", gap: "8px", padding: "6px 8px", borderRadius: "var(--radius-sm)", background: "var(--c-surface-card)" }}>
                  <span style={{ width: "6px", height: "6px", borderRadius: "50%", background: "var(--c-accent-400)", flexShrink: 0 }} />
                  <span style={{ fontSize: "var(--text-xs)", fontWeight: 600, color: "var(--c-ai-agent)" }}>{a.name}</span>
                  {a.role && <span style={{ fontSize: "0.6rem", color: "var(--c-text-tertiary)" }}>— {a.role}</span>}
                  {a.tools && a.tools.length > 0 && (
                    <span style={{ fontSize: "0.55rem", color: "var(--c-text-tertiary)", marginLeft: "auto" }}>{a.tools.length} tools</span>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Divider */}
      <div style={{ position: "relative", margin: "16px 0" }}>
        <div style={{ borderTop: "1px solid var(--c-border-subtle)" }} />
        <div style={{ position: "absolute", top: "-8px", left: "50%", transform: "translateX(-50%)", padding: "0 12px", background: "var(--c-bg-secondary)", fontSize: "0.6rem", color: "var(--c-text-tertiary)", textTransform: "uppercase", letterSpacing: "0.1em" }}>
          Global Model Settings
        </div>
      </div>

      {/* Brain Model card */}
      <div className="config-card">
        <div className="config-card-header">
          <div style={{ width: "28px", height: "28px", borderRadius: "var(--radius-md)", background: "rgba(16,185,129,0.1)", display: "flex", alignItems: "center", justifyContent: "center", fontSize: "var(--text-sm)" }}>⚡</div>
          <div>
            <span className="config-label">Brain Model</span>
            <span className="config-hint">Orchestrator + AI Summary (strong model)</span>
          </div>
        </div>
        <div style={{ marginTop: "8px", fontSize: "0.6rem", color: "var(--c-text-tertiary)", fontStyle: "italic" }}>
          Uses the active model: <strong style={{ color: "var(--c-text-tertiary)" }}>{abbreviateModelId(selectedModel) || "(none selected)"}</strong>
        </div>
      </div>

      {/* Explorer Model card */}
      <div className="config-card">
        <div className="config-card-header">
          <div style={{ width: "28px", height: "28px", borderRadius: "var(--radius-md)", background: "rgba(139,92,246,0.1)", display: "flex", alignItems: "center", justifyContent: "center", fontSize: "var(--text-sm)" }}>📖</div>
          <div>
            <span className="config-label">Explorer Model</span>
            <span className="config-hint">Used by specialist sub-agents dispatched by Brain</span>
          </div>
        </div>
        <select className="config-select" style={{ marginTop: "10px" }} value={selectedExplorer} onChange={(e) => onExplorerChange(e.target.value)}>
          <option value="">No explorer models available</option>
          {explorerModels.map((m) => (
            <option key={m.id} value={m.id}>{abbreviateModelId(m.id)}</option>
          ))}
        </select>
      </div>
    </div>
  );
}
