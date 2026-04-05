import { describe, it, expect } from "vitest";
import {
  buildDependencyGraph,
  todoToWorkspaceItem,
  jiraTicketToWorkspaceItem,
} from "../components/tasks/TasksTab";
import type { Todo } from "../types/messages";

// ============================================================
// TasksTab pure logic tests
// ============================================================

describe("buildDependencyGraph", () => {
  it("returns empty graph for empty todos", () => {
    expect(buildDependencyGraph([])).toEqual({});
  });

  it("returns empty graph when no dependencies exist", () => {
    const todos: Todo[] = [
      { id: "1", title: "Task A", status: "pending", source: "code" },
      { id: "2", title: "Task B", status: "pending", source: "code" },
    ];
    expect(buildDependencyGraph(todos)).toEqual({});
  });

  it("detects intra-ticket afterDeps", () => {
    const todos: Todo[] = [
      { id: "1", title: "Step 1", status: "pending", source: "linked", jiraKey: "DEV-10", changeNumber: 1 },
      { id: "2", title: "Step 2", status: "pending", source: "linked", jiraKey: "DEV-10", changeNumber: 2, afterDeps: [1] },
    ];
    const graph = buildDependencyGraph(todos);
    expect(graph["todo:2"]).toBeDefined();
    expect(graph["todo:2"].unresolvedAfter).toHaveLength(1);
    expect(graph["todo:2"].unresolvedAfter[0]).toEqual({ changeNumber: 1, ticketKey: "DEV-10" });
    expect(graph["todo:1"]).toBeUndefined(); // Step 1 has no deps
  });

  it("detects cross-ticket blockedBy", () => {
    const todos: Todo[] = [
      { id: "a", title: "From DEV-1", status: "pending", source: "linked", jiraKey: "DEV-1" },
      { id: "b", title: "From DEV-2", status: "pending", source: "linked", jiraKey: "DEV-2", blockedBy: ["DEV-1"] },
    ];
    const graph = buildDependencyGraph(todos);
    expect(graph["todo:b"]).toBeDefined();
    expect(graph["todo:b"].unresolvedBlocked).toHaveLength(1);
    expect(graph["todo:b"].unresolvedBlocked[0]).toEqual({ ticketKey: "DEV-1" });
  });

  it("does not mark blockedBy if blocking ticket has no todos", () => {
    const todos: Todo[] = [
      { id: "x", title: "Blocked by phantom", status: "pending", source: "linked", jiraKey: "DEV-5", blockedBy: ["DEV-999"] },
    ];
    const graph = buildDependencyGraph(todos);
    // DEV-999 doesn't exist in todos, so no unresolved blocker
    expect(graph["todo:x"]).toBeUndefined();
  });

  it("handles mixed afterDeps and blockedBy", () => {
    const todos: Todo[] = [
      { id: "1", title: "A#1", status: "pending", source: "linked", jiraKey: "A", changeNumber: 1 },
      { id: "2", title: "A#2", status: "pending", source: "linked", jiraKey: "A", changeNumber: 2, afterDeps: [1], blockedBy: ["B"] },
      { id: "3", title: "B#1", status: "pending", source: "linked", jiraKey: "B", changeNumber: 1 },
    ];
    const graph = buildDependencyGraph(todos);
    expect(graph["todo:2"].unresolvedAfter).toHaveLength(1);
    expect(graph["todo:2"].unresolvedBlocked).toHaveLength(1);
  });
});

describe("todoToWorkspaceItem", () => {
  it("converts a basic todo", () => {
    const todo: Todo = {
      id: "abc",
      title: "Fix bug",
      status: "pending",
      source: "code",
      filePath: "/src/foo.ts",
      relativePath: "src/foo.ts",
      lineNumber: 42,
      commentPrefix: "//",
    };
    const item = todoToWorkspaceItem(todo, {});
    expect(item.id).toBe("todo:abc");
    expect(item.source).toBe("code");
    expect(item.title).toBe("Fix bug");
    expect(item.relativePath).toBe("src/foo.ts");
    expect(item.lineNumber).toBe(42);
  });

  it("enriches with ticket status", () => {
    const todo: Todo = {
      id: "t1",
      title: "Linked task",
      status: "pending",
      source: "linked",
      jiraKey: "DEV-100",
    };
    const statuses = {
      "DEV-100": { key: "DEV-100", summary: "Do thing", status: "In Progress", isDone: false, browseUrl: "https://jira/DEV-100" },
    };
    const item = todoToWorkspaceItem(todo, statuses);
    expect(item.ticketStatus).toBeDefined();
    expect(item.ticketStatus!.status).toBe("In Progress");
    expect(item.browseUrl).toBe("https://jira/DEV-100");
    expect(item.isDone).toBe(false);
  });

  it("handles todo without jiraKey", () => {
    const todo: Todo = { id: "x", title: "Plain", status: "pending", source: "code" };
    const item = todoToWorkspaceItem(todo, {});
    expect(item.ticketKey).toBeUndefined();
    expect(item.ticketStatus).toBeUndefined();
  });
});

describe("jiraTicketToWorkspaceItem", () => {
  it("converts a jira ticket todo", () => {
    const ticket: Todo = {
      id: "DEV-50",
      title: "Implement feature",
      status: "pending",
      source: "jira",
      jiraKey: "DEV-50",
      browseUrl: "https://jira/DEV-50",
      priority: "High",
    };
    const item = jiraTicketToWorkspaceItem(ticket);
    expect(item.id).toBe("ticket:DEV-50");
    expect(item.source).toBe("ticket");
    expect(item.title).toBe("Implement feature");
    expect(item.ticketKey).toBe("DEV-50");
    expect(item.ticketStatus!.key).toBe("DEV-50");
    expect(item.priority).toBe("High");
  });

  it("handles done ticket", () => {
    const ticket: Todo = {
      id: "DEV-1",
      title: "Done",
      status: "done",
      source: "jira",
      jiraKey: "DEV-1",
      isDone: true,
    };
    const item = jiraTicketToWorkspaceItem(ticket);
    expect(item.isDone).toBe(true);
    expect(item.ticketStatus!.isDone).toBe(true);
  });
});
