# Reference CLAUDE.md

## Claude Code Source Study

The `reference/claude-code/` directory contains the full Claude Code (Anthropic's official CLI) source code (~205K lines TypeScript, extracted from npm sourcemaps 2026-03-31). This is our primary reference for production-grade AI agent patterns. **Read-only study material — not runnable from source.**

### Key Files to Study

| File | Lines | What to Learn |
|------|-------|---------------|
| `query.ts` | 1729 | Core agent loop: AsyncGenerator, immutable state transitions, 4-layer recovery |
| `QueryEngine.ts` | 1295 | SDK session wrapper: message handling, usage tracking, permission denials |
| `Tool.ts` | 792 | Tool type definition: `isConcurrencySafe()`, `isReadOnly()`, `checkPermissions()` |
| `tools.ts` | 550 | Tool registry: dynamic assembly, filtering, feature gates |
| `main.tsx` | 4683 | Entry point: startup, REPL, session management, mode detection |
| `context.ts` | 190 | System/user context builders (git status, CLAUDE.md injection) |
| `cost-tracker.ts` | 324 | Per-model token tracking, USD cost calculation, session persistence |
| `history.ts` | 465 | Prompt history: dual-buffer (memory + disk JSONL), paste-content references |
| `commands.ts` | 650 | 80+ slash command registry and routing |
| `setup.ts` | 600 | Initialization, project onboarding, config migration |

### Key Directories

| Directory | Files | What to Learn |
|-----------|-------|---------------|
| `tools/` | 40+ tools | AgentTool, BashTool, FileEditTool, WebFetchTool, SkillTool, MCPTool, etc. |
| `services/` | 22 subdirs | API client, MCP integration, analytics, rate limiting, token estimation |
| `hooks/` | 80+ files | React hooks for REPL UI, permission dialogs, keyboard shortcuts |
| `tasks/` | 6 types | local_agent, remote_agent, in_process_teammate, local_bash, dream |
| `skills/` | 3 sources | Bundled, filesystem (.md), MCP-based skill system |
| `coordinator/` | 1 file | Multi-agent orchestration (coordinator + parallel workers) |
| `bridge/` | 30 files | Remote control via claude.ai (JWT auth, WebSocket transport) |
| `buddy/` | 6 files | Tamagotchi companion (feature-gated, unreleased) |
| `state/` | 3 files | Zustand-like reactive state (AppState + bootstrap state) |
| `ink/` | 96 files | Custom React reconciler for terminal UI (Yoga layout, double-buffered) |
| `utils/` | 300+ files | Config, git, shell, diff, cursor, memoization, permissions |

### Patterns to Adopt (Priority Order)

1. **Agent Loop Recovery** — 4-layer: context collapse drain → reactive compact → max output recovery → stop hook blocking. Conductor's agent loop lacks structured recovery.
2. **Streaming Tool Execution** — Tools execute during model streaming. Each tool declares `isConcurrencySafe(input)`. Read-only tools run in parallel (max 10), writes serial.
3. **Prompt Cache Sharing** — Forked sub-agents reuse parent's `CacheSafeParams` to avoid re-tokenizing system prompts. Directly applicable to Brain multi-agent.
4. **Dream System** — Background memory consolidation with 3-gate trigger (time 24h → session 5x → PID lock). 4-phase: orient → gather → consolidate → prune. Maps to Phase 5.5 cross-session learning.
5. **Hook Event System** — 20+ events (PreToolUse, PostToolUse, SessionStart, FileChanged, etc.) with settings-based, plugin, and SDK callback registration. Enables extensible tool pipeline.
6. **Task Type Expansion** — 6 task types (local_agent, remote_agent, in_process_teammate, local_bash, local_workflow, dream) vs Conductor's dispatch_agent/dispatch_swarm.
7. **Permission System** — Multi-layer defense: config rules → hook system → ML classifier → user confirmation. 5 modes: default, plan, acceptEdits, bypassPermissions, auto.
8. **MCP Integration** — Model Context Protocol for tool/resource discovery from external servers (stdio, SSE, WebSocket, HTTP transports). Enables ecosystem tool plugins.
9. **Coordinator Mode** — Coordinator spawns isolated workers with restricted tool sets, shared scratchpad directory, and automatic cost tracking per agent.
10. **Tool Metadata Richness** — Beyond name/description: `shouldDefer` (lazy load), `maxResultSizeChars` (persist large results to disk), `interruptBehavior`, `isOpenWorld` (unsanitized input flag).

### Feature Gates Pattern

Claude Code uses two complementary feature gate systems:
- **Compile-time** (`feature('FLAG')` via Bun bundler): dead-code elimination for unreleased features (KAIROS, COORDINATOR_MODE, BUDDY, BRIDGE_MODE, etc.)
- **Runtime** (`getFeatureValue_CACHED_MAY_BE_STALE('tengu_flag')` via GrowthBook): non-blocking, cached flag checks for A/B testing and gradual rollouts

### Internal Codenames

| Codename | Meaning |
|----------|---------|
| Tengu | Claude Code project |
| Penguin | Fast mode |
| Chicago | Computer Use (vision + click/type) |
| KAIROS | Always-on assistant mode |
| ULTRAPLAN | 30-min remote planning (Opus 4.6) |
| CCR | Cloud Compute Runtime |
