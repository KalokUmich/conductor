import { useCallback, useState } from "react";
import { useVSCode } from "../../contexts/VSCodeContext";
import { Modal } from "../shared/Modal";

// ============================================================
// StackTraceModal — paste and analyze stack traces
// ============================================================

interface Props {
  open: boolean;
  onClose: () => void;
}

export function StackTraceModal({ open, onClose }: Props) {
  const { send } = useVSCode();
  const [trace, setTrace] = useState("");
  const [testOutput, setTestOutput] = useState("");
  const [activeTab, setActiveTab] = useState<"trace" | "test">("trace");

  const handleShareTrace = useCallback(() => {
    if (trace.trim()) {
      send({ command: "shareStackTrace", stackTrace: trace.trim() });
      setTrace("");
      onClose();
    }
  }, [send, trace, onClose]);

  const handleShareTestOutput = useCallback(() => {
    if (testOutput.trim()) {
      send({ command: "shareTestOutput", output: testOutput.trim() });
      setTestOutput("");
      onClose();
    }
  }, [send, testOutput, onClose]);

  return (
    <Modal open={open} onClose={onClose} title="Share Stack Trace / Test Output">
      <div className="stack-modal">
        {/* Tab selector */}
        <div className="ai-config-tabs">
          <button className={`ai-config-tab ${activeTab === "trace" ? "tab-selected" : ""}`} onClick={() => setActiveTab("trace")}>Stack Trace</button>
          <button className={`ai-config-tab ${activeTab === "test" ? "tab-selected" : ""}`} onClick={() => setActiveTab("test")}>Test Output</button>
        </div>

        {activeTab === "trace" ? (
          <div className="stack-modal-content">
            <textarea
              className="text-input stack-modal-textarea"
              value={trace}
              onChange={(e) => setTrace(e.target.value)}
              placeholder="Paste your stack trace here..."
              rows={10}
            />
            <button className="btn-primary btn-wide" onClick={handleShareTrace} disabled={!trace.trim()}>
              Share Stack Trace
            </button>
          </div>
        ) : (
          <div className="stack-modal-content">
            <textarea
              className="text-input stack-modal-textarea"
              value={testOutput}
              onChange={(e) => setTestOutput(e.target.value)}
              placeholder="Paste test output here..."
              rows={10}
            />
            <button className="btn-primary btn-wide" onClick={handleShareTestOutput} disabled={!testOutput.trim()}>
              Share Test Output
            </button>
          </div>
        )}
      </div>
    </Modal>
  );
}
