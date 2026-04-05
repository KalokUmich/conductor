// ============================================================
// Command parsing — slash commands, @mentions, #context
//
// Three prefix types:
//   / — Actions (/ask, /pr, /jira, /summary, /diff, /help)
//   @ — Agent scope (@brain, @review, @workspace)
//   # — Context injection (#file:path, #symbol:name, #ticket:KEY)
// ============================================================

export interface SlashCommand {
  name: string;
  description: string;
  hint: string;
  transform: (args: string) => string;
  isAI: boolean;
  category: "action" | "agent" | "context";
}

// ── Slash commands (/) — actions ────────────────────────

export const SLASH_COMMANDS: SlashCommand[] = [
  { name: "/ask", description: "Ask AI a question", hint: "Type your question...", transform: (args) => args, isAI: true, category: "action" },
  { name: "/pr", description: "Request a code review", hint: "Describe the PR or paste a link...", transform: (args) => `[query_type:code_review] ${args}`, isAI: true, category: "action" },
  { name: "/jira", description: "Create or search Jira issues", hint: "Describe the task or search query...", transform: (args) => `[query_type:issue_tracking] ${args}`, isAI: true, category: "action" },
  { name: "/summary", description: "Summarize chat decisions", hint: "Summarize recent discussion...", transform: (args) => `[query_type:summary] ${args || "Summarize the key decisions from this conversation"}`, isAI: true, category: "action" },
  { name: "/diff", description: "Show workspace changes", hint: "Show recent code changes...", transform: (args) => `[query_type:diff] ${args || "Show the current workspace diff"}`, isAI: true, category: "action" },
  { name: "/help", description: "Show available commands", hint: "", transform: () => "", isAI: false, category: "action" },
];

// ── @mentions — agent scopes ────────────────────────────

export const MENTION_COMMANDS: SlashCommand[] = [
  { name: "@AI", description: "Ask AI a question", hint: "Type your question...", transform: (args) => args, isAI: true, category: "agent" },
  { name: "@review", description: "Code review specialist", hint: "Review this code...", transform: (args) => `[query_type:code_review] ${args}`, isAI: true, category: "agent" },
  { name: "@workspace", description: "Include workspace context", hint: "Search the codebase...", transform: (args) => `[context:workspace] ${args}`, isAI: true, category: "agent" },
];

// ── #context — context injection ────────────────────────

export const CONTEXT_COMMANDS: SlashCommand[] = [
  { name: "#file:", description: "Attach a file as context", hint: "path/to/file.ts", transform: (args) => `[context:file:${args}]`, isAI: false, category: "context" },
  { name: "#symbol:", description: "Attach a symbol definition", hint: "symbolName", transform: (args) => `[context:symbol:${args}]`, isAI: false, category: "context" },
  { name: "#ticket:", description: "Attach a Jira ticket", hint: "PROJ-123", transform: (args) => `[context:ticket:${args}]`, isAI: false, category: "context" },
];

/** All commands combined for unified search */
export const ALL_COMMANDS: SlashCommand[] = [...SLASH_COMMANDS, ...MENTION_COMMANDS, ...CONTEXT_COMMANDS];

/** Match slash commands based on current input. Returns matching commands. */
export function matchSlashCommands(input: string): SlashCommand[] {
  if (!input) return [];

  // / prefix — action commands
  if (input.startsWith("/")) {
    const prefix = input.toLowerCase().split(" ")[0];
    if (input.includes(" ")) return [];
    return SLASH_COMMANDS.filter((c) => c.name.startsWith(prefix));
  }

  // @ prefix — agent mention commands
  if (input.startsWith("@")) {
    const prefix = input.toLowerCase().split(" ")[0];
    if (input.includes(" ")) return [];
    return MENTION_COMMANDS.filter((c) => c.name.startsWith(prefix));
  }

  // # prefix — context commands
  if (input.startsWith("#")) {
    const prefix = input.toLowerCase().split(" ")[0];
    return CONTEXT_COMMANDS.filter((c) => c.name.startsWith(prefix));
  }

  return [];
}

/** Compute ghost hint text (the autocomplete preview). */
export function computeGhostHint(input: string, matches: SlashCommand[]): string {
  if (matches.length === 0) return "";
  const prefix = input.toLowerCase().split(" ")[0];
  if (matches[0].name.startsWith(prefix)) {
    return matches[0].name.slice(prefix.length);
  }
  return "";
}

/** Parse a message and determine if it's an AI query, and transform it. */
export function parseMessageForAI(text: string): { query: string; isAI: boolean } {
  // Check slash commands
  for (const cmd of SLASH_COMMANDS) {
    if (text.startsWith(cmd.name + " ") || text === cmd.name) {
      const args = text.slice(cmd.name.length).trim();
      return { query: cmd.transform(args), isAI: !!cmd.isAI };
    }
  }

  // Check @mention commands
  for (const cmd of MENTION_COMMANDS) {
    if (text.startsWith(cmd.name + " ") || text === cmd.name) {
      const args = text.slice(cmd.name.length).trim();
      return { query: cmd.transform(args), isAI: !!cmd.isAI };
    }
  }

  // Legacy @AI support
  if (text.startsWith("@AI ") || text.startsWith("@ai ")) {
    return { query: text.slice(4), isAI: true };
  }
  return { query: text, isAI: false };
}
