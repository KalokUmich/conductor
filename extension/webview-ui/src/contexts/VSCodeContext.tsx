import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  type ReactNode,
} from "react";
import type { IncomingCommand, OutgoingCommand } from "../types/commands";

// ============================================================
// VS Code WebView API bridge
// ============================================================

// VS Code injects this global in WebView contexts
declare function acquireVsCodeApi(): {
  postMessage(message: unknown): void;
  getState(): unknown;
  setState(state: unknown): void;
};

interface VSCodeAPI {
  postMessage(msg: OutgoingCommand): void;
  getState<T>(): T | undefined;
  setState<T>(state: T): void;
}

type CommandHandler = (message: IncomingCommand) => void;
type CommandName = IncomingCommand["command"];

interface VSCodeContextValue {
  api: VSCodeAPI;
  /** Send a typed command to the extension host */
  send: (msg: OutgoingCommand) => void;
  /** Subscribe to a specific command from extension host. Returns unsubscribe fn. */
  on: (command: CommandName, handler: CommandHandler) => () => void;
  /** Subscribe to ALL commands from extension host. Returns unsubscribe fn. */
  onAny: (handler: CommandHandler) => () => void;
}

const VSCodeContext = createContext<VSCodeContextValue | null>(null);

// Singleton — VS Code only allows one acquireVsCodeApi() call
let _vscodeApi: VSCodeAPI | null = null;

function getVSCodeAPI(): VSCodeAPI {
  if (!_vscodeApi) {
    _vscodeApi = acquireVsCodeApi() as unknown as VSCodeAPI;
  }
  return _vscodeApi!;
}

export function VSCodeProvider({ children }: { children: ReactNode }) {
  const api = getVSCodeAPI();
  const listenersRef = useRef<Map<string, Set<CommandHandler>>>(new Map());
  const anyListenersRef = useRef<Set<CommandHandler>>(new Set());

  // Global message listener — routes to per-command subscribers
  useEffect(() => {
    function handleMessage(event: MessageEvent) {
      const message = event.data as IncomingCommand;
      if (!message?.command) return;

      // Notify specific command listeners
      const handlers = listenersRef.current.get(message.command);
      if (handlers) {
        handlers.forEach((h) => h(message));
      }

      // Notify wildcard listeners
      anyListenersRef.current.forEach((h) => h(message));
    }

    window.addEventListener("message", handleMessage);
    return () => window.removeEventListener("message", handleMessage);
  }, []);

  const send = useCallback(
    (msg: OutgoingCommand) => {
      api.postMessage(msg);
    },
    [api]
  );

  const on = useCallback(
    (command: CommandName, handler: CommandHandler): (() => void) => {
      if (!listenersRef.current.has(command)) {
        listenersRef.current.set(command, new Set());
      }
      listenersRef.current.get(command)!.add(handler);
      return () => {
        listenersRef.current.get(command)?.delete(handler);
      };
    },
    []
  );

  const onAny = useCallback((handler: CommandHandler): (() => void) => {
    anyListenersRef.current.add(handler);
    return () => {
      anyListenersRef.current.delete(handler);
    };
  }, []);

  return (
    <VSCodeContext.Provider value={{ api, send, on, onAny }}>
      {children}
    </VSCodeContext.Provider>
  );
}

/** Hook to access the VS Code postMessage bridge */
export function useVSCode(): VSCodeContextValue {
  const ctx = useContext(VSCodeContext);
  if (!ctx) {
    throw new Error("useVSCode must be used within VSCodeProvider");
  }
  return ctx;
}

/** Hook to subscribe to a specific command. Auto-cleans up. */
export function useCommand(
  command: CommandName,
  handler: CommandHandler
): void {
  const { on } = useVSCode();
  const handlerRef = useRef(handler);
  handlerRef.current = handler;

  useEffect(() => {
    return on(command, (msg) => handlerRef.current(msg));
  }, [on, command]);
}
