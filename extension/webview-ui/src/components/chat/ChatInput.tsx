import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ChangeEvent,
} from "react";
import { useChat } from "../../contexts/ChatContext";
import { useSession } from "../../contexts/SessionContext";
import { useVSCode } from "../../contexts/VSCodeContext";
import { useWebSocketSend, getTypingDisplayNames } from "../../hooks/useWebSocket";
import { formatFileSize } from "../../utils/format";

// ============================================================
// ChatInput — slash commands, code attachments, file upload,
//             action buttons, typing indicator, auto-resize
// ============================================================

interface SlashCommand {
  name: string;
  description: string;
  hint: string;
  transform: (args: string) => string;
  isAI?: boolean;
}

const SLASH_COMMANDS: SlashCommand[] = [
  { name: "/ask", description: "Ask AI a question", hint: "Type your question...", transform: (args) => args, isAI: true },
  { name: "/pr", description: "Request a code review", hint: "Describe the PR or paste a link...", transform: (args) => `[query_type:code_review] ${args}`, isAI: true },
  { name: "/jira", description: "Create or search Jira issues", hint: "Describe the task or search query...", transform: (args) => `[query_type:issue_tracking] ${args}`, isAI: true },
];

export function ChatInput() {
  const [value, setValue] = useState("");
  const [slashMenu, setSlashMenu] = useState<{ visible: boolean; items: SlashCommand[]; activeIndex: number }>({ visible: false, items: [], activeIndex: 0 });
  const [ghostHint, setGhostHint] = useState("");
  const [attachedSnippet, setAttachedSnippet] = useState<Record<string, unknown> | null>(null);
  const [attachedFile, setAttachedFile] = useState<{ file: File; previewUrl?: string } | null>(null);
  const [fileCaption, setFileCaption] = useState("");

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const typingTimeoutRef = useRef<ReturnType<typeof setTimeout>>();

  const { state: chatState, askAI } = useChat();
  const { state: sessionState } = useSession();
  const { send } = useVSCode();
  const wsSend = useWebSocketSend();

  // Auto-resize textarea
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 150)}px`;
  }, [value]);

  // Slash command detection
  useEffect(() => {
    if (value.startsWith("/")) {
      const input = value.toLowerCase();
      const matches = SLASH_COMMANDS.filter((c) => c.name.startsWith(input.split(" ")[0]));
      if (matches.length > 0 && !value.includes(" ")) {
        setSlashMenu({ visible: true, items: matches, activeIndex: 0 });
        if (matches[0].name.startsWith(input)) {
          setGhostHint(matches[0].name.slice(input.length));
        }
        return;
      }
    }
    setSlashMenu((s) => ({ ...s, visible: false }));
    setGhostHint("");
  }, [value]);

  // Send typing indicator (debounced)
  const sendTypingIndicator = useCallback(() => {
    if (typingTimeoutRef.current) return; // Already sent recently
    wsSend({ type: "typing", userId: sessionState.session?.userId });
    typingTimeoutRef.current = setTimeout(() => {
      typingTimeoutRef.current = undefined;
    }, 1000);
  }, [wsSend, sessionState.session?.userId]);

  const handleSend = useCallback(() => {
    const text = value.trim();
    if (!text && !attachedFile && !attachedSnippet) return;

    // Handle file upload via extension (not WebSocket)
    if (attachedFile) {
      const reader = new FileReader();
      reader.onload = () => {
        const base64 = (reader.result as string).split(",")[1];
        send({
          command: "uploadFile",
          roomId: sessionState.session?.roomId || "",
          userId: sessionState.session?.userId || "",
          displayName: sessionState.session?.displayName || "User",
          fileData: base64,
          fileName: attachedFile.file.name,
          mimeType: attachedFile.file.type,
          caption: fileCaption || text || undefined,
        });
      };
      reader.readAsDataURL(attachedFile.file);
      setAttachedFile(null);
      setFileCaption("");
      setValue("");
      if (textareaRef.current) textareaRef.current.style.height = "auto";
      return; // File upload is handled separately
    }

    // Check for slash commands / @AI
    let query = text;
    let isAIQuery = false;

    for (const cmd of SLASH_COMMANDS) {
      if (text.startsWith(cmd.name + " ") || text === cmd.name) {
        const args = text.slice(cmd.name.length).trim();
        query = cmd.transform(args);
        isAIQuery = !!cmd.isAI;
        break;
      }
    }

    if (text.startsWith("@AI ") || text.startsWith("@ai ")) {
      query = text.slice(4);
      isAIQuery = true;
    }

    if (isAIQuery) {
      askAI(query, attachedSnippet || undefined);
    } else {
      // Send via WebSocket — include code snippet if attached
      const msg: Record<string, unknown> = {
        displayName: sessionState.session?.displayName || "User",
        content: text || "",
      };
      if (attachedSnippet) {
        msg.type = "code_snippet";
        msg.codeSnippet = attachedSnippet;
      }
      wsSend(msg);
    }

    setValue("");
    setAttachedSnippet(null);
    setGhostHint("");
    if (textareaRef.current) textareaRef.current.style.height = "auto";
  }, [value, askAI, wsSend, sessionState.session, attachedSnippet, attachedFile, fileCaption, send]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      if (slashMenu.visible) {
        if (e.key === "ArrowDown") { e.preventDefault(); setSlashMenu((s) => ({ ...s, activeIndex: (s.activeIndex + 1) % s.items.length })); return; }
        if (e.key === "ArrowUp") { e.preventDefault(); setSlashMenu((s) => ({ ...s, activeIndex: (s.activeIndex - 1 + s.items.length) % s.items.length })); return; }
        if (e.key === "Enter" || e.key === "Tab") {
          e.preventDefault();
          const selected = slashMenu.items[slashMenu.activeIndex];
          setValue(selected.name + " ");
          setSlashMenu((s) => ({ ...s, visible: false }));
          setGhostHint("");
          return;
        }
        if (e.key === "Escape") { e.preventDefault(); setSlashMenu((s) => ({ ...s, visible: false })); setGhostHint(""); return; }
      }
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSend(); }
    },
    [slashMenu, handleSend]
  );

  const handleInput = useCallback(() => {
    sendTypingIndicator();
  }, [sendTypingIndicator]);

  // File selection
  const handleFileSelect = useCallback((e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const previewUrl = file.type.startsWith("image/") ? URL.createObjectURL(file) : undefined;
    setAttachedFile({ file, previewUrl });
    e.target.value = ""; // Reset for re-selection
  }, []);

  const clearFile = useCallback(() => {
    if (attachedFile?.previewUrl) URL.revokeObjectURL(attachedFile.previewUrl);
    setAttachedFile(null);
    setFileCaption("");
  }, [attachedFile]);

  // Code snippet from editor — request and listen for response
  const handleAttachCode = useCallback(() => {
    send({ command: "getCodeSnippet", filePath: "", startLine: 0, endLine: 0 });
  }, [send]);

  // Listen for uploadFileResult — send file message via WebSocket
  useEffect(() => {
    const handler = (event: MessageEvent) => {
      const msg = event.data;
      if (msg?.command === "uploadFileResult") {
        if (msg.success && msg.result) {
          const result = msg.result;
          // Send file message to chat via WebSocket (same as old chat.html)
          wsSend({
            type: "file",
            id: crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(),
            displayName: sessionState.session?.displayName || "User",
            fileId: result.id,
            originalFilename: result.original_filename,
            fileType: result.file_type,
            mimeType: result.mime_type,
            sizeBytes: result.size_bytes,
            downloadUrl: result.download_url,
            caption: fileCaption || undefined,
            ts: Date.now() / 1000,
          });
        } else if (msg.error) {
          send({ command: "alert", text: `Upload failed: ${msg.error}` });
        }
      }
    };
    window.addEventListener("message", handler);
    return () => window.removeEventListener("message", handler);
  }, [wsSend, sessionState.session?.displayName, fileCaption, send]);

  // Listen for codeSnippet response from extension
  useEffect(() => {
    const handler = (event: MessageEvent) => {
      const msg = event.data;
      if (msg?.command === "codeSnippet") {
        if (msg.error) {
          // Show error as alert via extension
          send({ command: "alert", text: msg.error });
          return;
        }
        setAttachedSnippet({
          code: msg.code,
          filename: msg.filename,
          relativePath: msg.relativePath,
          startLine: msg.startLine,
          endLine: msg.endLine,
          language: msg.language,
        });
      }
    };
    window.addEventListener("message", handler);
    return () => window.removeEventListener("message", handler);
  }, [send]);

  // Stack trace
  const [showStackInput, setShowStackInput] = useState(false);
  const [stackTrace, setStackTrace] = useState("");

  const handleShareStackTrace = useCallback(() => {
    if (stackTrace.trim()) {
      send({ command: "shareStackTrace", stackTrace: stackTrace.trim() });
      setStackTrace("");
      setShowStackInput(false);
    }
  }, [send, stackTrace]);

  const selectSlashCommand = useCallback((index: number) => {
    const selected = slashMenu.items[index];
    setValue(selected.name + " ");
    setSlashMenu((s) => ({ ...s, visible: false }));
    setGhostHint("");
    textareaRef.current?.focus();
  }, [slashMenu.items]);

  const isDisabled = chatState.isAIThinking;

  // Typing indicator display — show actual names
  const typingNames = getTypingDisplayNames(sessionState.session?.userId);

  return (
    <div className="chat-input-container">
      {/* Typing indicator */}
      {typingNames.length > 0 && (
        <div className="typing-indicator-bar animate-fade-in">
          <div className="typing-dots"><span /><span /><span /></div>
          <span className="typing-text">
            {typingNames.length === 1
              ? `${typingNames[0]} is typing...`
              : `${typingNames.join(", ")} are typing...`}
          </span>
        </div>
      )}

      {/* Attached code snippet preview */}
      {attachedSnippet && (
        <div className="snippet-preview animate-slide-up">
          <span className="snippet-icon">{"</>"}</span>
          <span className="snippet-name">{(attachedSnippet as Record<string, string>).relativePath || "Code snippet"}</span>
          <button className="snippet-remove" onClick={() => setAttachedSnippet(null)} aria-label="Remove attachment">×</button>
        </div>
      )}

      {/* Attached file preview */}
      {attachedFile && (
        <div className="file-preview-panel animate-slide-up">
          {attachedFile.previewUrl && (
            <img src={attachedFile.previewUrl} alt="Preview" className="file-preview-image" />
          )}
          <div className="file-preview-info">
            <span className="file-preview-icon">{attachedFile.file.type.startsWith("image/") ? "🖼" : "📎"}</span>
            <span className="file-preview-name">{attachedFile.file.name}</span>
            <span className="file-preview-size">{formatFileSize(attachedFile.file.size)}</span>
          </div>
          <input
            type="text"
            className="file-caption-input"
            value={fileCaption}
            onChange={(e) => setFileCaption(e.target.value)}
            placeholder="Add a caption..."
          />
          <div className="file-preview-actions">
            <button className="btn-primary btn-sm" onClick={handleSend}>Upload</button>
            <button className="btn-secondary btn-sm" onClick={clearFile}>Remove</button>
          </div>
        </div>
      )}

      {/* Stack trace input */}
      {showStackInput && (
        <div className="stack-input-panel animate-slide-up">
          <textarea
            className="stack-textarea"
            value={stackTrace}
            onChange={(e) => setStackTrace(e.target.value)}
            placeholder="Paste stack trace here..."
            rows={5}
          />
          <div className="stack-input-actions">
            <button className="btn-primary btn-sm" onClick={handleShareStackTrace} disabled={!stackTrace.trim()}>Share</button>
            <button className="btn-secondary btn-sm" onClick={() => { setShowStackInput(false); setStackTrace(""); }}>Cancel</button>
          </div>
        </div>
      )}

      {/* Slash command menu */}
      {slashMenu.visible && (
        <div className="slash-menu animate-slide-down">
          {slashMenu.items.map((cmd, i) => (
            <button
              key={cmd.name}
              className={`slash-item ${i === slashMenu.activeIndex ? "slash-active" : ""}`}
              onClick={() => selectSlashCommand(i)}
              onMouseEnter={() => setSlashMenu((s) => ({ ...s, activeIndex: i }))}
            >
              <span className="slash-name">{cmd.name}</span>
              <span className="slash-desc">{cmd.description}</span>
            </button>
          ))}
        </div>
      )}

      {/* Action buttons row */}
      <div className="input-actions-row">
        {/* File upload */}
        <input ref={fileInputRef} type="file" className="hidden-input" onChange={handleFileSelect} />
        <button className="input-action-btn" onClick={() => fileInputRef.current?.click()} title="Attach file" disabled={isDisabled}>
          <svg viewBox="0 0 20 20" fill="currentColor" width="14" height="14">
            <path fillRule="evenodd" d="M8 4a3 3 0 00-3 3v4a5 5 0 0010 0V7a1 1 0 112 0v4a7 7 0 11-14 0V7a5 5 0 0110 0v4a3 3 0 11-6 0V7a1 1 0 012 0v4a1 1 0 102 0V7a3 3 0 00-3-3z" clipRule="evenodd"/>
          </svg>
        </button>

        {/* Code snippet */}
        <button className="input-action-btn" onClick={handleAttachCode} title="Attach code snippet" disabled={isDisabled}>
          <svg viewBox="0 0 20 20" fill="currentColor" width="14" height="14">
            <path fillRule="evenodd" d="M12.316 3.051a1 1 0 01.633 1.265l-4 12a1 1 0 11-1.898-.632l4-12a1 1 0 011.265-.633zM5.707 6.293a1 1 0 010 1.414L3.414 10l2.293 2.293a1 1 0 11-1.414 1.414l-3-3a1 1 0 010-1.414l3-3a1 1 0 011.414 0zm8.586 0a1 1 0 011.414 0l3 3a1 1 0 010 1.414l-3 3a1 1 0 11-1.414-1.414L16.586 10l-2.293-2.293a1 1 0 010-1.414z" clipRule="evenodd"/>
          </svg>
        </button>

        {/* Stack trace */}
        <button className="input-action-btn" onClick={() => setShowStackInput(!showStackInput)} title="Share stack trace" disabled={isDisabled}>
          <svg viewBox="0 0 20 20" fill="currentColor" width="14" height="14">
            <path fillRule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7 4a1 1 0 11-2 0 1 1 0 012 0zm-1-9a1 1 0 00-1 1v4a1 1 0 102 0V6a1 1 0 00-1-1z" clipRule="evenodd"/>
          </svg>
        </button>

        {/* Spacer */}
        <div style={{ flex: 1 }} />
      </div>

      {/* Input row */}
      <div className="chat-input-row">
        <div className="textarea-wrapper">
          <textarea
            ref={textareaRef}
            className="chat-textarea"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={handleKeyDown}
            onInput={handleInput}
            placeholder={isDisabled ? "AI is thinking..." : "Message, /ask, /pr, /jira, or @AI..."}
            disabled={isDisabled}
            rows={1}
            aria-label="Chat input"
          />
          {ghostHint && <span className="ghost-hint">{value}{ghostHint}</span>}
        </div>

        <button
          className={`send-button ${value.trim() || attachedFile || attachedSnippet ? "send-active" : ""}`}
          onClick={handleSend}
          disabled={(!value.trim() && !attachedFile && !attachedSnippet) || isDisabled}
          aria-label="Send message"
        >
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z" />
          </svg>
        </button>
      </div>
    </div>
  );
}
