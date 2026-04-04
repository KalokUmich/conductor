import { useCallback, useEffect, useRef } from "react";
import { useSession } from "../contexts/SessionContext";
import { useChat } from "../contexts/ChatContext";
import { useVSCode } from "../contexts/VSCodeContext";
import type { ChatMessage, UserInfo } from "../types/messages";

// ============================================================
// useWebSocket — FULL WebSocket lifecycle matching old chat.html
//
// Flow: connect → recv 'connected' (store userId/role) →
//       recv 'history' (render users + messages, send 'join') →
//       recv messages/typing/etc
// ============================================================

let _ws: WebSocket | null = null;
let _wsSendFn: ((data: Record<string, unknown>) => void) | null = null;

export function useWebSocketSend(): (data: Record<string, unknown>) => void {
  return useCallback((data: Record<string, unknown>) => {
    if (_wsSendFn) _wsSendFn(data);
  }, []);
}

export function useWebSocket() {
  const { state: sessionState, dispatch: sessionDispatch } = useSession();
  const { addMessage, dispatch } = useChat();
  const { send } = useVSCode();
  const reconnectTimer = useRef<ReturnType<typeof setTimeout>>();
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectAttemptsRef = useRef(0);
  const intentionalCloseRef = useRef(false);
  const assignedUserIdRef = useRef<string>("");
  const assignedRoleRef = useRef<string>("");

  const session = sessionState.session;
  const ssoIdentity = sessionState.ssoIdentity;

  // Use refs for callbacks so the effect closure always has latest versions
  const addMessageRef = useRef(addMessage);
  addMessageRef.current = addMessage;
  const dispatchRef = useRef(dispatch);
  dispatchRef.current = dispatch;
  const sendRef = useRef(send);
  sendRef.current = send;
  const sessionDispatchRef = useRef(sessionDispatch);
  sessionDispatchRef.current = sessionDispatch;
  const sessionRef = useRef(session);
  sessionRef.current = session;
  const ssoIdentityRef = useRef(ssoIdentity);
  ssoIdentityRef.current = ssoIdentity;

  useEffect(() => {
    const backendUrl = session?.backendUrl || "";
    const roomId = session?.roomId || "";
    if (!backendUrl || !roomId) return;

    const wsBaseUrl = backendUrl.replace(/^http/, "ws");
    const fullWsUrl = `${wsBaseUrl}/ws/chat/${roomId}`;

    // Load local cache BEFORE WS connects (instant display, like old code)
    sendRef.current({ command: "loadLocalMessages", roomId });

    function connect() {
      console.log("[WS] Connecting to:", fullWsUrl);
      const ws = new WebSocket(fullWsUrl);
      wsRef.current = ws;
      _ws = ws;

      _wsSendFn = (data: Record<string, unknown>) => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify(data));
        }
      };

      ws.onopen = () => {
        console.log("[WS] Connected");
        reconnectAttemptsRef.current = 0;
        // Don't send join yet — wait for 'connected' then 'history' from backend
      };

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          handleMessage(data, ws);
        } catch (e) {
          console.error("[WS] Parse error:", e);
        }
      };

      ws.onclose = (ev) => {
        console.log("[WS] Disconnected:", ev.code, ev.reason);
        _ws = null;
        _wsSendFn = null;
        if (!intentionalCloseRef.current && reconnectAttemptsRef.current < 10) {
          reconnectAttemptsRef.current++;
          const delay = Math.min(1000 * Math.pow(1.5, reconnectAttemptsRef.current), 15000);
          reconnectTimer.current = setTimeout(connect, delay);
        }
      };

      ws.onerror = (err) => {
        console.error("[WS] Error:", err);
      };
    }

    function handleMessage(data: Record<string, unknown>, ws: WebSocket) {
      const type = data.type as string;

      // ── 1. Backend-assigned credentials (FIRST message) ──
      if (type === "connected") {
        console.log("[WS] Backend assigned credentials:", data);
        assignedUserIdRef.current = (data.userId as string) || "";
        assignedRoleRef.current = (data.role as string) || "";
        // Store in session context so UI can use it
        if (sessionRef.current) {
          sessionDispatchRef.current({
            type: "SET_CONDUCTOR_STATE",
            state: "Hosting", // keep current state
            session: { ...sessionRef.current, userId: assignedUserIdRef.current },
          });
        }
        return;
      }

      // ── 2. History (second message after connected) ──
      if (type === "history") {
        // Update users from history
        const users = data.users as Array<Record<string, unknown>> | undefined;
        if (users && Array.isArray(users)) {
          const map = new Map<string, UserInfo>();
          users.forEach((u) => {
            const uid = (u.userId as string) || (u.id as string) || "";
            map.set(uid, {
              displayName: (u.displayName as string) || "User",
              role: (u.role as string) || "engineer",
              avatarColor: (u.avatarColor as number) || 0,
              online: u.online !== false,
              identitySource: u.identitySource as string,
            });
          });
          sessionDispatchRef.current({ type: "SET_USERS", users: map });
        }

        // Render messages from backend history
        // Local mode: skip backend messages — local cache is source of truth
        // Online mode: merge backend history for sync with Redis
        const messages = data.messages as Array<Record<string, unknown>> | undefined;
        const isLocalMode = !!(sessionRef.current as Record<string, unknown> | null)?.isLocal;
        if (messages && Array.isArray(messages) && messages.length > 0 && !isLocalMode) {
          const chatMsgs: ChatMessage[] = messages.map(parseMessageData);
          dispatchRef.current({ type: "ADD_MESSAGES_BATCH", messages: chatMsgs });
          // Also save backend history to local cache for future offline access
          sendRef.current({ command: "saveLocalMessages", roomId, messages: messages as unknown[] });
        }

        // Send join (only if not recovery/reconnect)
        if (!data.isRecovery) {
          const sso = ssoIdentityRef.current;
          ws.send(JSON.stringify({
            type: "join",
            displayName: sessionRef.current?.displayName || "User",
            identitySource: sso ? "sso" : undefined,
            ssoEmail: sso?.email || undefined,
            ssoProvider: sso?.provider || undefined,
          }));
        }

        // Fetch AI status
        sendRef.current({ command: "getAiStatus" });
        return;
      }

      // ── 3. Typing indicator ──
      if (type === "typing") {
        const userId = data.userId as string;
        const displayName = data.displayName as string;
        if (data.isTyping) {
          dispatchRef.current({ type: "SET_TYPING", userId, isTyping: true });
          // Store display name for showing in UI
          _typingNames.set(userId, displayName || "Someone");
          setTimeout(() => {
            dispatchRef.current({ type: "SET_TYPING", userId, isTyping: false });
            _typingNames.delete(userId);
          }, 3000);
        } else {
          dispatchRef.current({ type: "SET_TYPING", userId, isTyping: false });
          _typingNames.delete(userId);
        }
        return;
      }

      // ── 4. User joined/left (full user list update) ──
      if (type === "user_joined" || type === "user_left") {
        // Backend sends full user list in data.users
        const users = data.users as Array<Record<string, unknown>> | undefined;
        if (users && Array.isArray(users)) {
          const map = new Map<string, UserInfo>();
          users.forEach((u) => {
            const uid = (u.userId as string) || (u.id as string) || "";
            map.set(uid, {
              displayName: (u.displayName as string) || "User",
              role: (u.role as string) || "engineer",
              avatarColor: (u.avatarColor as number) || 0,
              online: u.online !== false,
            });
          });
          sessionDispatchRef.current({ type: "SET_USERS", users: map });
        }

        const user = data.user as Record<string, unknown> | undefined;
        const name = user?.displayName || data.displayName || "User";
        addMessageRef.current({
          id: `system-${Date.now()}-${Math.random()}`,
          userId: "system", displayName: "System", role: "system",
          content: type === "user_joined" ? `${name} joined the chat` : `${name} left the chat`,
          type: "system",
          ts: Date.now() / 1000,
        });
        return;
      }

      // ── 5. Session ended ──
      if (type === "session_ended") {
        addMessageRef.current({
          id: `system-end-${Date.now()}`,
          userId: "system", displayName: "System", role: "system",
          content: `🔴 ${(data.message as string) || "Chat session has ended"}`,
          type: "system",
          ts: Date.now() / 1000,
        });
        sendRef.current({ command: "sessionEnded" });
        return;
      }

      // ── 6. Lead changed ──
      if (type === "lead_changed") {
        addMessageRef.current({
          id: `system-lead-${Date.now()}`,
          userId: "system", displayName: "System", role: "system",
          content: `Lead transferred`,
          type: "system",
          ts: Date.now() / 1000,
        });
        return;
      }

      // ── 7. Read receipts ──
      if (type === "read_receipt") return;

      // ── 8. Tool request → extension ──
      if (type === "tool_request") {
        console.log("[WS] Tool request:", data.tool, "reqId:", data.requestId);
        sendRef.current({
          command: "tool_request",
          requestId: data.requestId as string,
          tool: data.tool as string,
          params: (data.params as Record<string, unknown>) || {},
          workspace: data.workspace as string | undefined,
        });
        return;
      }

      // ── 9. Tool response relay ──
      if (data.command === "tool_response") {
        if (_ws?.readyState === WebSocket.OPEN) {
          _ws.send(JSON.stringify(data));
        }
        return;
      }

      // ── 10. Regular chat message (all types) ──
      const msg = parseMessageData(data);
      addMessageRef.current(msg);

      // Cache every incoming message locally for offline recovery
      sendRef.current({ command: "saveLocalMessages", roomId, messages: [data] } as never);
    }

    function parseMessageData(data: Record<string, unknown>): ChatMessage {
      return {
        id: (data.id as string) || `msg-${Date.now()}-${Math.random()}`,
        userId: (data.userId as string) || "",
        displayName: (data.displayName as string) || "Unknown",
        role: (data.role as ChatMessage["role"]) || "engineer",
        content: (data.content as string) || "",
        type: ((data.type as string) || "text") as ChatMessage["type"],
        ts: (data.ts as number) || Date.now() / 1000,
        identitySource: data.identitySource as string,
        metadata: data.metadata as Record<string, unknown>,
        codeSnippet: data.codeSnippet as ChatMessage["codeSnippet"],
        fileId: data.fileId as string,
        originalFilename: data.originalFilename as string,
        fileType: data.fileType as string,
        mimeType: data.mimeType as string,
        sizeBytes: data.sizeBytes as number,
        downloadUrl: data.downloadUrl as string,
        caption: data.caption as string,
        answer: data.answer as string,
        summary: data.summary as string,
        codePrompt: (data.codePrompt as string) || (data.code_prompt as string),
        thinkingSteps: data.thinkingSteps as ChatMessage["thinkingSteps"],
        stackTrace: data.stackTrace as ChatMessage["stackTrace"],
        testFailures: data.testFailures as ChatMessage["testFailures"],
      };
    }

    intentionalCloseRef.current = false;
    connect();

    return () => {
      intentionalCloseRef.current = true;
      clearTimeout(reconnectTimer.current);
      if (wsRef.current) {
        wsRef.current.close();
      }
      _ws = null;
      _wsSendFn = null;
    };
  // CRITICAL: Only depend on stable identifiers (backendUrl, roomId).
  // Do NOT depend on session object, ssoIdentity, or conductorState —
  // those change during the WS lifecycle and would cause reconnect loops.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session?.backendUrl, session?.roomId]);

  // Forward tool responses from extension back via WebSocket
  useEffect(() => {
    const handler = (event: MessageEvent) => {
      const data = event.data;
      if (data?.command === "tool_response") {
        console.log("[WS] Tool response relay:", data.tool, "reqId:", data.requestId, "wsOpen:", _ws?.readyState === WebSocket.OPEN);
        if (_ws?.readyState === WebSocket.OPEN) {
          _ws.send(JSON.stringify(data));
        }
      }
    };
    window.addEventListener("message", handler);
    return () => window.removeEventListener("message", handler);
  }, []);
}

// ── Typing display names (module-level for access from ChatInput) ──

const _typingNames = new Map<string, string>();

/** Get typing user display names (excluding self) */
export function getTypingDisplayNames(selfUserId?: string): string[] {
  const names: string[] = [];
  _typingNames.forEach((name, uid) => {
    if (uid !== selfUserId) names.push(name);
  });
  return names;
}
