import { describe, it, expect } from "vitest";
import {
  SLASH_COMMANDS,
  matchSlashCommands,
  computeGhostHint,
  parseMessageForAI,
} from "../utils/slashCommands";

// ============================================================
// Slash command parsing tests
// ============================================================

describe("SLASH_COMMANDS", () => {
  it("has 6 commands defined", () => {
    expect(SLASH_COMMANDS).toHaveLength(6);
  });

  it("all commands start with /", () => {
    SLASH_COMMANDS.forEach((cmd) => {
      expect(cmd.name).toMatch(/^\//);
    });
  });

  it("all AI commands have isAI set to true", () => {
    SLASH_COMMANDS.filter(c => c.name !== "/help").forEach((cmd) => {
      expect(cmd.isAI).toBe(true);
    });
  });
});

describe("matchSlashCommands", () => {
  it("returns empty for non-slash input", () => {
    expect(matchSlashCommands("hello")).toEqual([]);
    expect(matchSlashCommands("@AI ask")).toEqual([]);
    expect(matchSlashCommands("")).toEqual([]);
  });

  it('matches /a to /ask', () => {
    const matches = matchSlashCommands("/a");
    expect(matches).toHaveLength(1);
    expect(matches[0].name).toBe("/ask");
  });

  it('matches /j to /jira', () => {
    const matches = matchSlashCommands("/j");
    expect(matches).toHaveLength(1);
    expect(matches[0].name).toBe("/jira");
  });

  it('matches /p to /pr', () => {
    const matches = matchSlashCommands("/p");
    expect(matches).toHaveLength(1);
    expect(matches[0].name).toBe("/pr");
  });

  it("matches / to all slash commands", () => {
    const matches = matchSlashCommands("/");
    expect(matches).toHaveLength(6);
  });

  it("returns empty after space (command already completed)", () => {
    expect(matchSlashCommands("/ask ")).toEqual([]);
    expect(matchSlashCommands("/jira create ticket")).toEqual([]);
  });

  it("returns empty for unknown command", () => {
    expect(matchSlashCommands("/xyz")).toEqual([]);
  });

  it("is case insensitive", () => {
    expect(matchSlashCommands("/ASK")).toHaveLength(1);
    expect(matchSlashCommands("/Jira")).toHaveLength(1);
  });
});

describe("computeGhostHint", () => {
  it("returns completion for partial match", () => {
    const matches = matchSlashCommands("/a");
    expect(computeGhostHint("/a", matches)).toBe("sk");
  });

  it("returns empty for full match", () => {
    const matches = matchSlashCommands("/ask");
    expect(computeGhostHint("/ask", matches)).toBe("");
  });

  it("returns empty for no matches", () => {
    expect(computeGhostHint("/xyz", [])).toBe("");
  });

  it("completes /j to ira", () => {
    const matches = matchSlashCommands("/j");
    expect(computeGhostHint("/j", matches)).toBe("ira");
  });

  it("completes /p to r", () => {
    const matches = matchSlashCommands("/p");
    expect(computeGhostHint("/p", matches)).toBe("r");
  });
});

describe("parseMessageForAI", () => {
  it("detects /ask command", () => {
    const result = parseMessageForAI("/ask what is this?");
    expect(result.isAI).toBe(true);
    expect(result.query).toBe("what is this?");
  });

  it("detects /pr command with transform", () => {
    const result = parseMessageForAI("/pr main...feature/x");
    expect(result.isAI).toBe(true);
    expect(result.query).toBe("[query_type:code_review] main...feature/x");
  });

  it("detects /jira command with transform", () => {
    const result = parseMessageForAI("/jira create login bug");
    expect(result.isAI).toBe(true);
    expect(result.query).toBe("[query_type:issue_tracking] create login bug");
  });

  it("detects @AI prefix", () => {
    const result = parseMessageForAI("@AI explain this code");
    expect(result.isAI).toBe(true);
    expect(result.query).toBe("explain this code");
  });

  it("detects @ai prefix (case insensitive)", () => {
    const result = parseMessageForAI("@ai how does this work");
    expect(result.isAI).toBe(true);
    expect(result.query).toBe("how does this work");
  });

  it("returns plain text for non-AI messages", () => {
    const result = parseMessageForAI("hello everyone");
    expect(result.isAI).toBe(false);
    expect(result.query).toBe("hello everyone");
  });

  it("handles bare /ask with no args", () => {
    const result = parseMessageForAI("/ask");
    expect(result.isAI).toBe(true);
    expect(result.query).toBe("");
  });

  it("does not match partial commands", () => {
    const result = parseMessageForAI("/asker something");
    expect(result.isAI).toBe(false);
  });

  it("does not match slash in middle of text", () => {
    const result = parseMessageForAI("please /ask about this");
    expect(result.isAI).toBe(false);
  });
});
