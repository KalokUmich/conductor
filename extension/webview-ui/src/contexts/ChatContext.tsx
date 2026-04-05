import {
  createContext,
  useCallback,
  useContext,
  useReducer,
  useTransition,
  type ReactNode,
} from "react";
import type {
  AgentQuestion,
  AIProgressEvent,
  BrainTree,
  ChatMessage,
  ThinkingStep,
} from "../types/messages";
import { useCommand, useVSCode } from "./VSCodeContext";
import { useSession } from "./SessionContext";

// ============================================================
// Chat state — messages, AI thinking, WebSocket
// ============================================================

export interface ChatState {
  messages: ChatMessage[];
  seenIds: Set<string>;
  isAIThinking: boolean;
  brainTree: BrainTree;
  currentAction: string;
  thinkingSteps: ThinkingStep[];
  agentQuestion: AgentQuestion | null;
  typingUsers: Map<string, number>; // userId → timeout id
  hasMoreHistory: boolean;
  isLoadingHistory: boolean;
  /** Original query when planMode was used — enables plan-apply bar on AI response */
  pendingPlanQuery: string | null;
}

export type ChatAction =
  | { type: "ADD_MESSAGE"; message: ChatMessage }
  | { type: "ADD_MESSAGES_BATCH"; messages: ChatMessage[] }
  | { type: "AI_PROGRESS"; event: AIProgressEvent }
  | { type: "AI_DONE"; error?: string; stopped?: boolean; thinkingSteps?: ThinkingStep[] }
  | { type: "AGENT_QUESTION"; question: AgentQuestion }
  | { type: "CLEAR_AGENT_QUESTION" }
  | { type: "SET_TYPING"; userId: string; isTyping: boolean }
  | { type: "SET_HISTORY_LOADING"; loading: boolean }
  | { type: "SET_HAS_MORE_HISTORY"; hasMore: boolean }
  | { type: "SET_PLAN_QUERY"; query: string | null }
  | { type: "CLEAR_MESSAGES" };

function updateBrainTree(tree: BrainTree, event: AIProgressEvent): BrainTree {
  const next = {
    ...tree,
    agents: { ...tree.agents },
  };
  const detail = event.detail || {};
  const agentName = detail.agent_name || "";

  switch (event.kind) {
    case "thinking":
      if (agentName) {
        if (!next.agents[agentName]) {
          next.agents[agentName] = { status: "running", steps: [] };
        }
        next.currentAgent = agentName;
      }
      break;
    case "agent_dispatched":
      next.agents[agentName] = { status: "running", steps: [] };
      next.currentAgent = agentName;
      next.phase = "dispatching";
      break;
    case "tool_call":
      if (agentName) {
        if (!next.agents[agentName]) {
          next.agents[agentName] = { status: "running", steps: [] };
        }
        next.agents[agentName] = {
          ...next.agents[agentName],
          steps: [...next.agents[agentName].steps, { tool: detail.tool || "", status: "running" }],
        };
      }
      break;
    case "tool_result":
      if (agentName && next.agents[agentName]) {
        const steps = [...next.agents[agentName].steps];
        if (steps.length > 0) {
          steps[steps.length - 1] = {
            ...steps[steps.length - 1],
            status: detail.success !== false ? "ok" : "fail",
            summary: event.message || "",
          };
        }
        next.agents[agentName] = { ...next.agents[agentName], steps };
      }
      break;
    case "agent_complete":
      if (agentName && next.agents[agentName]) {
        next.agents[agentName] = {
          ...next.agents[agentName],
          status: detail.status === "done" ? "done" : "fail",
        };
      }
      break;
    case "swarm_dispatched":
      (detail.agents || []).forEach((n) => {
        next.agents[n] = { status: "running", steps: [] };
      });
      next.phase = "swarm";
      break;
  }

  return next;
}

function getCurrentAction(event: AIProgressEvent): string {
  const detail = event.detail || {};
  const agentName = detail.agent_name || "";

  switch (event.kind) {
    case "start": return "Connecting to AI...";
    case "classify": return event.message || "Analyzing query...";
    case "thinking":
      return agentName
        ? `${agentName}: ${event.message || "Thinking..."}`
        : event.message || "Thinking...";
    case "tool_call":
      return agentName
        ? `${agentName} → ${detail.tool || ""}`
        : event.message || "Working...";
    case "agent_dispatched": return `→ ${agentName}`;
    case "agent_complete":
      return `${detail.status === "done" ? "✓" : "✗"} ${agentName}`;
    case "swarm_dispatched":
      return `→ Swarm: ${detail.swarm_name || "parallel"}`;
    default: return event.message || "";
  }
}

const INITIAL_BRAIN_TREE: BrainTree = {
  thinking: "",
  agents: {},
  phase: "idle",
  currentAgent: "",
};

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "ADD_MESSAGE": {
      const msg = action.message;
      if (msg.id && state.seenIds.has(msg.id)) return state;
      const seenIds = new Set(state.seenIds);
      if (msg.id) seenIds.add(msg.id);
      // Cap seen IDs at 10000
      if (seenIds.size > 10000) {
        const arr = Array.from(seenIds);
        seenIds.clear();
        arr.slice(-5000).forEach((id) => seenIds.add(id));
      }
      return { ...state, messages: [...state.messages, msg], seenIds };
    }
    case "ADD_MESSAGES_BATCH": {
      const seenIds = new Set(state.seenIds);
      const newMsgs = action.messages.filter((m) => {
        if (m.id && seenIds.has(m.id)) return false;
        if (m.id) seenIds.add(m.id);
        return true;
      });
      if (newMsgs.length === 0) return state;
      return {
        ...state,
        messages: [...newMsgs, ...state.messages],
        seenIds,
        isLoadingHistory: false,
      };
    }
    case "AI_PROGRESS": {
      const brainTree = updateBrainTree(state.brainTree, action.event);
      const currentAction = getCurrentAction(action.event);
      const thinkingSteps = [...state.thinkingSteps];
      const detail = action.event.detail || {};

      if (action.event.kind === "tool_result") {
        thinkingSteps.push({
          tool: detail.tool || "",
          summary: action.event.message || "done",
          success: detail.success !== false,
        });
      } else if (action.event.kind === "agent_dispatched") {
        thinkingSteps.push({
          tool: "dispatch",
          summary: `→ ${detail.agent_name || ""}`,
          success: true,
        });
      } else if (action.event.kind === "agent_complete") {
        thinkingSteps.push({
          tool: detail.agent_name || "",
          summary: action.event.message || detail.status || "",
          success: detail.status === "done",
        });
      }

      return {
        ...state,
        isAIThinking: true,
        brainTree,
        currentAction,
        thinkingSteps,
      };
    }
    case "AI_DONE": {
      // Attach thinking steps + plan query to the last AI message
      const steps = action.thinkingSteps || state.thinkingSteps;
      let messages = state.messages;
      const lastIdx = [...messages].reverse().findIndex(m =>
        m.type === "ai_answer" || m.type === "ai_explanation" || m.type === "ai_summary"
      );
      if (lastIdx >= 0) {
        const idx = messages.length - 1 - lastIdx;
        const updated = {
          ...messages[idx],
          ...(steps.length > 0 ? { thinkingSteps: steps } : {}),
          ...(state.pendingPlanQuery ? { planQuery: state.pendingPlanQuery } : {}),
        };
        messages = [...messages];
        messages[idx] = updated;
      }
      return {
        ...state,
        messages,
        isAIThinking: false,
        brainTree: INITIAL_BRAIN_TREE,
        currentAction: "",
        thinkingSteps: [],
        agentQuestion: null,
        pendingPlanQuery: null,
      };
    }
    case "AGENT_QUESTION":
      return { ...state, agentQuestion: action.question };
    case "CLEAR_AGENT_QUESTION":
      return { ...state, agentQuestion: null };
    case "SET_TYPING": {
      const typingUsers = new Map(state.typingUsers);
      if (action.isTyping) {
        typingUsers.set(action.userId, Date.now());
      } else {
        typingUsers.delete(action.userId);
      }
      return { ...state, typingUsers };
    }
    case "SET_HISTORY_LOADING":
      return { ...state, isLoadingHistory: action.loading };
    case "SET_HAS_MORE_HISTORY":
      return { ...state, hasMoreHistory: action.hasMore };
    case "SET_PLAN_QUERY":
      return { ...state, pendingPlanQuery: action.query };
    case "CLEAR_MESSAGES":
      return {
        ...state,
        messages: [],
        seenIds: new Set(),
        thinkingSteps: [],
      };
    default:
      return state;
  }
}

const initialChatState: ChatState = {
  messages: [],
  seenIds: new Set(),
  isAIThinking: false,
  brainTree: INITIAL_BRAIN_TREE,
  currentAction: "",
  thinkingSteps: [],
  agentQuestion: null,
  typingUsers: new Map(),
  hasMoreHistory: true,
  isLoadingHistory: false,
  pendingPlanQuery: null,
};

interface ChatContextValue {
  state: ChatState;
  dispatch: React.Dispatch<ChatAction>;
  /** Use useTransition for non-urgent AI thinking updates */
  isPendingTransition: boolean;
  addMessage: (msg: ChatMessage) => void;
  sendChatMessage: (content: string) => void;
  askAI: (query: string, codeContext?: Record<string, unknown>) => void;
  stopAI: () => void;
  answerAgent: (sessionId: string, answer: string) => void;
}

const ChatContext = createContext<ChatContextValue | null>(null);

export function ChatProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(chatReducer, initialChatState);
  const [isPendingTransition, startTransition] = useTransition();
  const { send } = useVSCode();
  const { state: sessionState } = useSession();

  // AI progress — use transition to keep input responsive
  useCommand("askAIProgress", (msg) => {
    if (msg.command !== "askAIProgress") return;
    startTransition(() => {
      dispatch({ type: "AI_PROGRESS", event: msg as AIProgressEvent });
    });
  });

  useCommand("askAIDone", (msg) => {
    if (msg.command !== "askAIDone") return;
    dispatch({
      type: "AI_DONE",
      error: msg.error,
      stopped: msg.stopped,
      thinkingSteps: msg.thinkingSteps,
    });
  });

  useCommand("agentQuestion", (msg) => {
    if (msg.command !== "agentQuestion") return;
    dispatch({ type: "AGENT_QUESTION", question: msg as AgentQuestion });
  });

  const addMessage = useCallback((msg: ChatMessage) => {
    dispatch({ type: "ADD_MESSAGE", message: msg });
  }, []);

  const sendChatMessage = useCallback(
    (_content: string) => {
      // Actual WebSocket send is handled by useWebSocket hook
      // This is for adding the message to local state
    },
    []
  );

  const askAI = useCallback(
    (query: string, codeContext?: Record<string, unknown>) => {
      if (!sessionState.session?.roomId) return;
      const isPlanMode = /^\[query_type:issue_tracking\]\s*(create|investigate)/i.test(query)
        || /^\[jira\]/i.test(query); // TasksTab investigate sends [jira] prefix
      send({
        command: "askAI",
        roomId: sessionState.session.roomId,
        query,
        planMode: isPlanMode,
        codeContext,
      });
      // Track plan query so AI response can show the plan-apply bar
      if (isPlanMode) {
        dispatch({ type: "SET_PLAN_QUERY", query });
      }
      dispatch({
        type: "AI_PROGRESS",
        event: { phase: "agent", kind: "start", message: "Connecting to AI...", detail: {} },
      });
    },
    [send, sessionState.session?.roomId]
  );

  const stopAI = useCallback(() => {
    send({ command: "stopAskAI" });
  }, [send]);

  const answerAgent = useCallback(
    (sessionId: string, answer: string) => {
      send({ command: "agentAnswer", sessionId, answer });
      dispatch({ type: "CLEAR_AGENT_QUESTION" });
    },
    [send]
  );

  return (
    <ChatContext.Provider
      value={{
        state,
        dispatch,
        isPendingTransition,
        addMessage,
        sendChatMessage,
        askAI,
        stopAI,
        answerAgent,
      }}
    >
      {children}
    </ChatContext.Provider>
  );
}

export function useChat(): ChatContextValue {
  const ctx = useContext(ChatContext);
  if (!ctx) throw new Error("useChat must be used within ChatProvider");
  return ctx;
}
