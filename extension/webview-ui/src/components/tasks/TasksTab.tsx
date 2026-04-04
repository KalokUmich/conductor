import { memo, useCallback, useEffect, useState } from "react";
import { useVSCode, useCommand } from "../../contexts/VSCodeContext";
import { useSession } from "../../contexts/SessionContext";
import type { Todo } from "../../types/messages";

// ============================================================
// TasksTab — Task Board with 3 sections + scan/load buttons
// ============================================================

type TodoSection = "linked" | "code" | "jira";

export function TasksTab() {
  const [todos, setTodos] = useState<Todo[]>([]);
  const [jiraTickets, setJiraTickets] = useState<Todo[]>([]);
  const [expandedSections, setExpandedSections] = useState<Set<TodoSection>>(
    new Set(["linked", "code", "jira"])
  );
  const [scanning, setScanning] = useState(false);
  const { send, onAny } = useVSCode();
  const { state: sessionState } = useSession();

  const roomId = sessionState.session?.roomId;

  // Load todos on mount
  useEffect(() => {
    if (roomId) {
      send({ command: "loadTodos", roomId });
    }
  }, [send, roomId]);

  // Listen for todo updates
  useCommand("todosLoaded", (msg) => {
    if (msg.command !== "todosLoaded") return;
    setTodos(msg.todos);
  });

  useCommand("todoCreated", (msg) => {
    if (msg.command !== "todoCreated") return;
    setTodos((prev) => [...prev, msg.todo]);
  });

  useCommand("todoUpdated", (msg) => {
    if (msg.command !== "todoUpdated") return;
    setTodos((prev) => prev.map((t) => (t.id === msg.todo.id ? msg.todo : t)));
  });

  useCommand("todoDeleted", (msg) => {
    if (msg.command !== "todoDeleted") return;
    setTodos((prev) => prev.filter((t) => t.id !== msg.todoId));
  });

  useCommand("workspaceTodosScanned", (msg) => {
    if (msg.command !== "workspaceTodosScanned") return;
    setScanning(false);
    const codeTodos = msg.todos.map((t) => ({ ...t, source: "code" as const }));
    setTodos((prev) => {
      const existing = prev.filter((t) => t.source !== "code");
      return [...existing, ...codeTodos];
    });
  });

  // Listen for Jira tickets (may come as 'jiraTicketsLoaded')
  useEffect(() => {
    return onAny((msg) => {
      const cmd = (msg as unknown as { command: string }).command;
      if (cmd === "jiraTicketsLoaded") {
        const data = msg as unknown as { tickets?: Array<Record<string, unknown>> };
        if (data.tickets) {
          const mapped: Todo[] = data.tickets.map((t) => ({
            id: (t.key as string) || `jira-${Date.now()}`,
            title: (t.summary as string) || (t.key as string) || "Jira ticket",
            status: (t.status as string) === "Done" ? "done" as const : "pending" as const,
            jiraKey: t.key as string,
            epicKey: t.epicKey as string,
            epicName: t.epicName as string,
            epicColor: t.epicColor as string,
            source: "jira" as const,
          }));
          setJiraTickets(mapped);
        }
      }
    });
  }, [onAny]);

  const handleScan = useCallback(() => {
    setScanning(true);
    send({ command: "scanWorkspaceTodos" });
  }, [send]);

  const handleLoadJira = useCallback(() => {
    send({ command: "loadJiraTickets" });
  }, [send]);

  const toggleSection = useCallback((section: TodoSection) => {
    setExpandedSections((prev) => {
      const next = new Set(prev);
      if (next.has(section)) next.delete(section); else next.add(section);
      return next;
    });
  }, []);

  const linkedTodos = todos.filter((t) => t.source === "linked");
  const codeTodos = todos.filter((t) => t.source === "code");
  const allJira = jiraTickets;

  return (
    <div className="tasks-tab">
      {/* Header with action buttons */}
      <div className="tasks-header">
        <div className="tasks-title-row">
          <span className="tasks-icon">📋</span>
          <span className="tasks-title">Task Board</span>
          <span className="todo-count">{linkedTodos.length + codeTodos.length + allJira.length}</span>
        </div>
        <div className="tasks-actions">
          <button className="action-btn action-brand" onClick={handleLoadJira}>
            Load Jira
          </button>
          <button className="action-btn action-brand" onClick={handleScan} disabled={scanning}>
            {scanning ? "Scanning..." : "Scan TODOs"}
          </button>
        </div>
      </div>

      {/* Quick add */}
      <QuickAddTodo />

      {/* Sections */}
      <TodoSectionComp
        title="Linked TODOs"
        count={linkedTodos.length}
        expanded={expandedSections.has("linked")}
        onToggle={() => toggleSection("linked")}
        todos={linkedTodos}
      />

      <TodoSectionComp
        title="Code TODOs"
        count={codeTodos.length}
        expanded={expandedSections.has("code")}
        onToggle={() => toggleSection("code")}
        todos={codeTodos}
      />

      <JiraSectionComp
        count={allJira.length}
        expanded={expandedSections.has("jira")}
        onToggle={() => toggleSection("jira")}
        tickets={allJira}
      />
    </div>
  );
}

// ── Todo Section ──────────────────────────────────────────

function TodoSectionComp({
  title, count, expanded, onToggle, todos,
}: {
  title: string; count: number; expanded: boolean; onToggle: () => void; todos: Todo[];
}) {
  return (
    <div className="todo-section">
      <button className="todo-section-header" onClick={onToggle}>
        <span className={`todo-chevron ${expanded ? "chevron-open" : ""}`}>›</span>
        <span className="todo-section-title">{title}</span>
        <span className="todo-count">{count}</span>
      </button>
      {expanded && (
        <div className="todo-list stagger-children">
          {todos.length === 0 ? (
            <div className="todo-empty">No items</div>
          ) : (
            todos.map((todo) => <TodoItem key={todo.id} todo={todo} />)
          )}
        </div>
      )}
    </div>
  );
}

// ── Todo Item ─────────────────────────────────────────────

const TodoItem = memo(function TodoItem({ todo }: { todo: Todo }) {
  const { send } = useVSCode();
  const isBlocked = todo.dependencies?.blocked || todo.dependencies?.after;

  const handleToggle = useCallback(() => {
    const newStatus = todo.status === "done" ? "pending" : "done";
    send({ command: "updateTodo", todoId: todo.id, status: newStatus });
  }, [send, todo.id, todo.status]);

  const handleNavigate = useCallback(() => {
    if (todo.filePath) {
      send({ command: "navigateToCode", relativePath: todo.filePath, startLine: todo.lineNumber || 1 });
    }
  }, [send, todo.filePath, todo.lineNumber]);

  return (
    <div className={`todo-item ${isBlocked ? "todo-blocked" : ""} ${todo.status === "done" ? "todo-done" : ""}`}>
      <button
        className={`todo-checkbox ${todo.status === "done" ? "checked" : ""}`}
        onClick={handleToggle}
      >
        {todo.status === "done" && "✓"}
      </button>
      <div className="todo-info" onClick={handleNavigate}>
        <span className="todo-title">{todo.title}</span>
        {todo.jiraKey && <span className="todo-jira-key">{todo.jiraKey}</span>}
        {todo.epicName && (
          <span className="todo-epic" style={{ borderColor: todo.epicColor || "var(--c-border-default)" }}>
            {todo.epicName}
          </span>
        )}
        {isBlocked && <span className="todo-blocked-badge">blocked</span>}
      </div>
    </div>
  );
});

// ── Jira Section with Epic Grouping ───────────────────────

function JiraSectionComp({
  count, expanded, onToggle, tickets,
}: {
  count: number; expanded: boolean; onToggle: () => void; tickets: Todo[];
}) {
  // Group by epic
  const epicGroups = new Map<string, Todo[]>();
  const noEpic: Todo[] = [];
  tickets.forEach((t) => {
    if (t.epicKey) {
      const key = t.epicKey;
      if (!epicGroups.has(key)) epicGroups.set(key, []);
      epicGroups.get(key)!.push(t);
    } else {
      noEpic.push(t);
    }
  });

  return (
    <div className="todo-section">
      <button className="todo-section-header" onClick={onToggle}>
        <span className={`todo-chevron ${expanded ? "chevron-open" : ""}`}>›</span>
        <span className="todo-section-title">Jira Tickets</span>
        <span className="todo-count">{count}</span>
      </button>
      {expanded && (
        <div className="jira-ticket-list">
          {tickets.length === 0 ? (
            <div className="todo-empty">No tickets loaded</div>
          ) : (
            <>
              {/* Epic groups */}
              {Array.from(epicGroups.entries()).map(([epicKey, items]) => {
                const epicName = items[0]?.epicName || epicKey;
                const epicColor = items[0]?.epicColor || "var(--c-accent-400)";
                return (
                  <div key={epicKey} className="epic-group">
                    <div className="epic-header" style={{ borderLeftColor: epicColor }}>
                      <span className="epic-name">{epicName}</span>
                      <span className="epic-count">{items.length}</span>
                    </div>
                    {items.map((t) => <TodoItem key={t.id} todo={t} />)}
                  </div>
                );
              })}
              {/* Tickets without epic */}
              {noEpic.length > 0 && (
                <div className="epic-group">
                  {epicGroups.size > 0 && (
                    <div className="epic-header" style={{ borderLeftColor: "var(--c-text-muted)" }}>
                      <span className="epic-name" style={{ color: "var(--c-text-muted)" }}>No Epic</span>
                      <span className="epic-count">{noEpic.length}</span>
                    </div>
                  )}
                  {noEpic.map((t) => <TodoItem key={t.id} todo={t} />)}
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

// ── Quick Add Todo ────────────────────────────────────────

function QuickAddTodo() {
  const [value, setValue] = useState("");
  const { send } = useVSCode();
  const { state: sessionState } = useSession();

  const handleAdd = useCallback(() => {
    const title = value.trim();
    if (!title || !sessionState.session?.roomId) return;
    send({ command: "createTodo", title, roomId: sessionState.session.roomId });
    setValue("");
  }, [value, send, sessionState.session?.roomId]);

  return (
    <div className="quick-add">
      <input
        type="text"
        className="text-input"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && handleAdd()}
        placeholder="Add a task..."
      />
      <button className="btn-primary btn-sm" onClick={handleAdd} disabled={!value.trim()}>+</button>
    </div>
  );
}
