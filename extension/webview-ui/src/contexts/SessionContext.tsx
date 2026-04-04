import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useReducer,
  type ReactNode,
} from "react";
import type {
  ConductorState,
  Permissions,
  Session,
  SSOIdentity,
  UserInfo,
} from "../types/messages";
import { useCommand, useVSCode } from "./VSCodeContext";

// SSO UI state for pending/done display
export type SSOUIState = "idle" | "pending" | "done";
export interface SSOPendingInfo {
  userCode: string;
  provider: string;
}

// ============================================================
// Global session state — conductor FSM, permissions, users
// ============================================================

interface SessionState {
  conductorState: ConductorState;
  session: Session | null;
  permissions: Permissions;
  ssoIdentity: SSOIdentity | null;
  ssoProvider: string | null;
  enabledSSOProviders: string[];
  autoApplyEnabled: boolean;
  users: Map<string, UserInfo>;
  isAIBusy: boolean;
  ssoUIState: SSOUIState;
  ssoPending: SSOPendingInfo | null;
}

type SessionAction =
  | { type: "SET_CONDUCTOR_STATE"; state: ConductorState; session?: Session; ssoIdentity?: SSOIdentity; ssoProvider?: string }
  | { type: "SET_PERMISSIONS"; permissions: Permissions }
  | { type: "SET_AUTO_APPLY"; enabled: boolean }
  | { type: "SET_SSO_IDENTITY"; identity: SSOIdentity | null }
  | { type: "SET_USERS"; users: Map<string, UserInfo> }
  | { type: "UPDATE_USER"; userId: string; info: UserInfo }
  | { type: "REMOVE_USER"; userId: string }
  | { type: "SET_AI_BUSY"; busy: boolean }
  | { type: "SET_SSO_PROVIDERS"; providers: string[] }
  | { type: "SSO_PENDING"; userCode: string; provider: string }
  | { type: "SSO_DONE"; identity: SSOIdentity; provider: string }
  | { type: "SSO_CLEARED" }
  | { type: "RESET_SESSION" };

function sessionReducer(state: SessionState, action: SessionAction): SessionState {
  switch (action.type) {
    case "SET_CONDUCTOR_STATE":
      return {
        ...state,
        conductorState: action.state,
        session: action.session ?? state.session,
        ssoIdentity: action.ssoIdentity ?? state.ssoIdentity,
        ssoProvider: action.ssoProvider ?? state.ssoProvider,
      };
    case "SET_PERMISSIONS":
      return { ...state, permissions: action.permissions };
    case "SET_AUTO_APPLY":
      return { ...state, autoApplyEnabled: action.enabled };
    case "SET_SSO_IDENTITY":
      return { ...state, ssoIdentity: action.identity };
    case "SET_USERS": {
      return { ...state, users: action.users };
    }
    case "UPDATE_USER": {
      const users = new Map(state.users);
      users.set(action.userId, action.info);
      return { ...state, users };
    }
    case "REMOVE_USER": {
      const users = new Map(state.users);
      users.delete(action.userId);
      return { ...state, users };
    }
    case "SET_AI_BUSY":
      return { ...state, isAIBusy: action.busy };
    case "SET_SSO_PROVIDERS":
      return { ...state, enabledSSOProviders: action.providers };
    case "SSO_PENDING":
      return {
        ...state,
        ssoUIState: "pending",
        ssoPending: { userCode: action.userCode, provider: action.provider },
      };
    case "SSO_DONE": {
      // Derive display name from email (part before @)
      const displayName = action.identity.name || action.identity.email?.split("@")[0] || "User";
      return {
        ...state,
        ssoUIState: "done",
        ssoPending: null,
        ssoIdentity: action.identity,
        ssoProvider: action.provider,
        session: state.session ? { ...state.session, displayName } : state.session,
      };
    }
    case "SSO_CLEARED":
      return {
        ...state,
        ssoUIState: "idle",
        ssoPending: null,
        ssoIdentity: null,
        ssoProvider: null,
      };
    case "RESET_SESSION":
      return {
        ...state,
        session: null,
        users: new Map(),
        isAIBusy: false,
      };
    default:
      return state;
  }
}

/** Convert { aws: true, google: false } or ["aws","google"] to string[] */
function parseSSOProviders(raw: unknown): string[] {
  if (Array.isArray(raw)) return raw as string[];
  if (raw && typeof raw === "object") {
    return Object.entries(raw as Record<string, boolean>)
      .filter(([, enabled]) => enabled)
      .map(([name]) => name);
  }
  return [];
}

// Read initial state injected by extension host
function getInitialState(): SessionState {
  const w = window as unknown as Record<string, unknown>;
  return {
    conductorState: (w.initialConductorState as ConductorState) || "Idle",
    session: (w.initialSession as Session) || null,
    permissions: (w.initialPermissions as Permissions) || { sessionRole: "none" },
    ssoIdentity: (w.initialSSOIdentity as SSOIdentity) || null,
    ssoProvider: (w.initialSSOProvider as string) || null,
    enabledSSOProviders: parseSSOProviders(w.initialEnabledSSOProviders),
    autoApplyEnabled: false,
    users: new Map(),
    isAIBusy: false,
    ssoUIState: (w.initialSSOIdentity as SSOIdentity)?.email ? "done" : "idle",
    ssoPending: null,
  };
}

interface SessionContextValue {
  state: SessionState;
  dispatch: React.Dispatch<SessionAction>;
}

const SessionContext = createContext<SessionContextValue | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(sessionReducer, undefined, getInitialState);
  const { send } = useVSCode();

  // Request initial state from extension
  useEffect(() => {
    send({ command: "getConductorState" });
    send({ command: "getPermissions" });
    send({ command: "getAutoApplyState" });
  }, [send]);

  // Listen to conductor state changes
  useCommand("conductorStateChanged", (msg) => {
    if (msg.command !== "conductorStateChanged") return;
    dispatch({
      type: "SET_CONDUCTOR_STATE",
      state: msg.state,
      session: msg.session,
      ssoIdentity: msg.ssoIdentity,
      ssoProvider: msg.ssoProvider,
    });
  });

  useCommand("updatePermissions", (msg) => {
    if (msg.command !== "updatePermissions") return;
    dispatch({ type: "SET_PERMISSIONS", permissions: msg.permissions });
  });

  useCommand("autoApplyState", (msg) => {
    if (msg.command !== "autoApplyState") return;
    dispatch({ type: "SET_AUTO_APPLY", enabled: msg.enabled });
  });

  // SSO command handlers (not in typed IncomingCommand, use onAny)
  const { onAny } = useVSCode();
  useEffect(() => {
    return onAny((msg) => {
      const cmd = (msg as unknown as { command: string }).command;
      const data = msg as unknown as Record<string, unknown>;
      if (cmd === "ssoLoginPending") {
        dispatch({
          type: "SSO_PENDING",
          userCode: (data.userCode as string) || "",
          provider: (data.provider as string) || "",
        });
      } else if (cmd === "ssoLoginResult") {
        if (data.identity) {
          const identity = data.identity as SSOIdentity;
          // Attach userUuid from backend user profile
          if (data.userUuid) {
            (identity as Record<string, unknown>).userUuid = data.userUuid;
          }
          dispatch({
            type: "SSO_DONE",
            identity,
            provider: (data.provider as string) || "",
          });
        } else {
          // Login failed or cancelled — back to idle
          dispatch({ type: "SSO_CLEARED" });
        }
      } else if (cmd === "ssoCacheCleared") {
        dispatch({ type: "SSO_CLEARED" });
      } else if (cmd === "ssoProvidersUpdate") {
        const providers = parseSSOProviders((data as Record<string, unknown>).providers);
        dispatch({ type: "SET_SSO_PROVIDERS", providers });
      }
    });
  }, [onAny, dispatch]);

  useCommand("endChatConfirmed", () => {
    // Tell extension to transition FSM back (mirrors old chat.html sessionEnded flow)
    send({ command: "sessionEnded" } as never);
    dispatch({ type: "RESET_SESSION" });
  });

  return (
    <SessionContext.Provider value={{ state, dispatch }}>
      {children}
    </SessionContext.Provider>
  );
}

export function useSession(): SessionContextValue {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSession must be used within SessionProvider");
  return ctx;
}

export function useSessionActions() {
  const { send } = useVSCode();
  const { dispatch } = useSession();

  const startSession = useCallback(() => send({ command: "startSession" }), [send]);
  const stopSession = useCallback(() => send({ command: "stopSession" }), [send]);
  const joinSession = useCallback((inviteUrl: string) => send({ command: "joinSession", inviteUrl }), [send]);
  const leaveSession = useCallback(() => send({ command: "leaveSession" }), [send]);
  const confirmEndChat = useCallback(() => send({ command: "confirmEndChat" }), [send]);
  const quitChat = useCallback(() => send({ command: "quitChat" }), [send]);
  const retryConnection = useCallback(() => send({ command: "retryConnection" }), [send]);
  const setAutoApply = useCallback((enabled: boolean) => {
    send({ command: "setAutoApply", enabled });
    dispatch({ type: "SET_AUTO_APPLY", enabled });
  }, [send, dispatch]);

  return {
    startSession,
    stopSession,
    joinSession,
    leaveSession,
    confirmEndChat,
    quitChat,
    retryConnection,
    setAutoApply,
  };
}
