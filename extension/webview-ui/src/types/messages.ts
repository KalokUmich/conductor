// ============================================================
// Message types — the contract between React WebView and extension host
// ============================================================

export type MessageType =
  | "text"
  | "code_snippet"
  | "file"
  | "ai_summary"
  | "ai_code_prompt"
  | "ai_explanation"
  | "ai_answer"
  | "system"
  | "stack_trace"
  | "test_failures";

export interface ChatMessage {
  id: string;
  userId: string;
  displayName: string;
  role: "host" | "engineer" | "system";
  content: string;
  type: MessageType;
  ts: number;
  sender?: string;          // participant UUID (= userId); used by ChatRecord v2
  aiMeta?: AIMeta;           // AI-specific metadata (model, tokens, thinking)
  identitySource?: string;
  metadata?: Record<string, unknown>;
  // Code snippet fields
  codeSnippet?: {
    code: string;
    filename: string;
    relativePath?: string;
    startLine: number;
    endLine: number;
    language: string;
  };
  // File fields
  fileId?: string;
  originalFilename?: string;
  fileType?: string;
  mimeType?: string;
  sizeBytes?: number;
  downloadUrl?: string;
  caption?: string;
  // AI fields
  answer?: string;
  thinkingSteps?: ThinkingStep[];
  summary?: string;
  codePrompt?: string;
  // Plan mode — original query for plan-apply bar
  planQuery?: string;
  // Stack trace fields
  stackTrace?: StackTraceData;
  // Test failures fields
  testFailures?: TestFailuresData;
}

export interface StackTraceData {
  rawTrace: string;
  frames: StackFrame[];
}

export interface StackFrame {
  filePath: string;
  lineNumber: number;
  functionName: string;
  code?: string;
}

export interface TestFailuresData {
  framework: string;
  totalFailed: number;
  rawOutput?: string;
  tests: TestFailure[];
}

export interface TestFailure {
  name: string;
  errorMessage?: string;
  filePath?: string;
  lineNumber?: number;
}

export interface ThinkingStep {
  kind?: string;
  tool?: string;
  summary?: string;
  message?: string;
  text?: string;
  success?: boolean;
}

// ============================================================
// Chat Record v2 — participant & AI metadata
// ============================================================

export interface Participant {
  email?: string;
  name: string;
  role: "host" | "engineer" | "ai";
  status: "active" | "left";
  avatarColor?: number;
  identitySource?: string;
}

export interface AIMeta {
  model?: string;
  tokensIn?: number;
  tokensOut?: number;
  thinkingSteps?: ThinkingStep[];
}

// ============================================================
// AI Progress events (SSE stream)
// ============================================================

export type AIProgressKind =
  | "start"
  | "classify"
  | "thinking"
  | "tool_call"
  | "tool_result"
  | "agent_dispatched"
  | "agent_complete"
  | "swarm_dispatched"
  | "ask_user_waiting";

export interface AIProgressEvent {
  phase: "agent";
  kind: AIProgressKind;
  message: string;
  detail: {
    agent_name?: string;
    tool?: string;
    success?: boolean;
    status?: string;
    iteration?: number;
    swarm_name?: string;
    agents?: string[];
    confidence?: number;
  };
}

export interface AgentQuestion {
  sessionId: string;
  question: string;
  context?: string;
  options?: string[];
}

// ============================================================
// Brain tree state (for thinking indicator)
// ============================================================

export interface AgentState {
  status: "running" | "done" | "fail";
  steps: ToolStep[];
}

export interface ToolStep {
  tool: string;
  status: "running" | "ok" | "fail";
  summary?: string;
}

export interface BrainTree {
  thinking: string;
  agents: Record<string, AgentState>;
  phase: "idle" | "dispatching" | "swarm";
  currentAgent: string;
}

// ============================================================
// Session & Conductor state
// ============================================================

export type ConductorState =
  | "Idle"
  | "ReadyToHost"
  | "Hosting"
  | "Joined"
  | "BackendDisconnected";

export interface Session {
  roomId: string;
  hostId: string;
  userId: string;
  displayName?: string;
  createdAt: number;
  backendUrl: string;
  isLocal?: boolean;
}

export interface Permissions {
  sessionRole: "host" | "guest" | "none";
  canCreateSummary?: boolean;
  canGenerateChanges?: boolean;
  canApplyChanges?: boolean;
  canAutoApply?: boolean;
  canConfigureAI?: boolean;
  canShareCode?: boolean;
  canUploadFiles?: boolean;
}

export interface SSOIdentity {
  email: string;
  name?: string;
  provider: string;
  avatarUrl?: string;
}

export interface UserInfo {
  displayName: string;
  role: string;
  avatarColor: number;
  identitySource?: string;
  online?: boolean;
}

// ============================================================
// Room & Online rooms
// ============================================================

export interface Room {
  roomId: string;
  hostName?: string;
  hostEmail?: string;
  createdAt: string;
  userCount?: number;
  status?: "active" | "idle";
}

// ============================================================
// Todo / Task Board
// ============================================================

export interface Todo {
  id: string;
  title: string;
  description?: string;
  status: "pending" | "in_progress" | "done";
  roomId?: string;
  jiraKey?: string;
  epicKey?: string;
  epicName?: string;
  epicColor?: string;
  dependencies?: {
    jira?: string;
    after?: string;
    blocked?: string;
  };
  source: "linked" | "code" | "jira";
  filePath?: string;
  lineNumber?: number;
  // Workspace scan fields (from todoScanner WorkspaceTodo)
  relativePath?: string;
  commentPrefix?: string;
  descriptionLine?: number;
  rawTag?: string;
  blockEndLine?: number;
  changeNumber?: number;
  afterDeps?: number[];
  blockedBy?: string[];
  parentTicket?: string;
  // Jira enrichment
  browseUrl?: string;
  ticketStatus?: string;
  priority?: string;
  assignee?: string;
  isDone?: boolean;
}

/** Item in the AI Working Space (dragged from backlog or room TODO). */
export interface WorkspaceItem {
  id: string;
  source: "code" | "ticket";
  title: string;
  description?: string;
  ticketKey?: string;
  ticketStatus?: { key: string; summary: string; status: string; isDone: boolean; browseUrl?: string };
  filePath?: string;
  relativePath?: string;
  lineNumber?: number;
  commentPrefix?: string;
  descriptionLine?: number;
  rawTag?: string;
  blockEndLine?: number;
  changeNumber?: number;
  afterDeps?: number[];
  blockedBy?: string[];
  parentTicket?: string;
  priority?: string;
  browseUrl?: string;
  isDone?: boolean;
}

// ============================================================
// Change management
// ============================================================

export interface CodeChange {
  filePath: string;
  diff?: string;
  newContent?: string;
  operation: "modify" | "create" | "delete";
}

export interface ChangeSet {
  changes: CodeChange[];
}

export interface PolicyResult {
  approved: boolean;
  reason?: string;
}
