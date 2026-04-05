# Extension CLAUDE.md

## Structure

```
extension/src/
├── extension.ts             # Entry point, command registration, _handleLocalToolRequest,
│                            # _handleAskAI (unified @AI + code explanation via codeContext),
│                            # getOnlineRooms, removeQuitRoom, auto-workspace registration
├── panels/                  # collabPanel.ts, workspacePanel.ts
├── services/
│   ├── conductorStateMachine.ts        # FSM: Idle → ReadyToHost → Hosting → Joined
│   ├── conductorController.ts          # FSM driver
│   ├── workflowPanel.ts                # Workflow visualization WebView (singleton)
│   ├── workspaceClient.ts              # /workspace/ HTTP client
│   ├── conductorFileSystemProvider.ts  # conductor:// URI scheme
│   ├── lspResolver.ts                  # VS Code LSP definition + references
│   ├── relevanceRanker.ts              # Hybrid structural + semantic relevance scoring
│   ├── contextPlanGenerator.ts         # Deduplicated read-file operation planner
│   ├── xmlPromptAssembler.ts           # Structured XML prompt builder for LLM
│   ├── localToolDispatcher.ts          # Three-tier tool dispatch (all native TS)
│   ├── astToolRunner.ts                # 6 AST tools via web-tree-sitter
│   ├── treeSitterService.ts            # web-tree-sitter WASM wrapper (8 languages)
│   ├── complexToolRunner.ts            # 6 complex tools (compressed_view, trace_variable, etc.)
│   ├── fileEditRunner.ts               # file_edit + file_write tools (read-before-write enforcement)
│   ├── ticketProvider.ts               # ITicketProvider interface + JiraTicketProvider (batch status, my tickets)
│   ├── todoScanner.ts                  # Workspace TODO scanner ({jira:TICKET#N|after:M|blocked:OTHER} deps, //+ continuations, 43+ file types)
│   ├── jiraAuthService.ts              # Jira OAuth URI handler + connection state management
│   ├── jiraTokenStore.ts               # Local Jira token persistence (SecretStorage + .conductor/jira.json)
│   └── chatLocalStore.ts               # Local message cache (IndexedDB via VS Code globalState)
└── commands/index.ts

extension/webview-ui/
├── src/
│   ├── components/          # React 18 components (chat, modals, panels, tasks, shared)
│   ├── contexts/            # ChatContext, SessionContext, VSCodeContext
│   ├── hooks/               # useWebSocket, useReadReceipts, useHistoryPagination, useMermaid
│   ├── types/               # commands.ts (postMessage contract), messages.ts (data types)
│   ├── styles/              # design-tokens.css, components.css
│   └── utils/               # format.ts helpers
├── esbuild.mjs              # Bundler config (IIFE, browser target, JSX automatic)
└── tsconfig.json

extension/media/
├── webview.js       # React WebView bundle (esbuild output)
├── webview.css      # React WebView styles (esbuild output)
├── workflow.html    # Workflow visualization — SVG graph + agent detail panel
├── highlight.min.js    # Bundled Highlight.js 11.9.0 (no CDN dependency)
└── github-dark.min.css # Highlight.js GitHub Dark theme

extension/grammars/          # tree-sitter .wasm grammar files (committed)
├── tree-sitter.wasm         # web-tree-sitter runtime
└── tree-sitter-{lang}.wasm  # Python, JS, TS, Java, Go, Rust, C, C++ (8 files)
```

## Local Mode Tool Dispatch

When the agent runs in local workspace mode, tools are proxied via WebSocket to the extension. The extension runs ALL tools natively — zero Python dependency. All tool output schemas are aligned with Python (same field names, same structure) so the LLM sees consistent data regardless of execution path. The TS grep uses `rg --no-ignore --no-messages` with `-E` fallback on system grep to match Python's behavior:

```
RemoteToolExecutor → WebSocket → extension._handleLocalToolRequest
  → localToolDispatcher.ts
    ├── SUBPROCESS (13): grep, read_file, list_files, glob, git_log, git_diff, git_diff_files,
    │                    git_blame, git_show, find_tests, run_test, ast_search, get_repo_graph
    ├── AST (6):         file_outline, find_symbol, find_references, get_callees, get_callers, expand_symbol
    │                    → web-tree-sitter WASM (treeSitterService + astToolRunner)
    └── COMPLEX (6):     compressed_view, trace_variable, detect_patterns, get_dependencies, get_dependents, test_outline
                         → native TypeScript (complexToolRunner)
```

Grammar WASM files in `extension/grammars/` are committed to the repo. **Do not** re-download
grammars independently — the grammar ABI version must match `web-tree-sitter` (pinned at 0.26.7).
Mismatched versions cause silent fallback to regex extraction with degraded accuracy.

## Chat WebView (React)

The WebView is a React 18 SPA built with esbuild (`npm run compile:webview`). Key patterns:

- **Message rendering**: `MessageBubble.tsx` dispatches by `msg.type` (`text`, `code_snippet`, `ai_answer`, `file`, `stack_trace`, `test_failures`, `system`, etc.)
- **Syntax highlighting**: `CodeBlock.tsx` uses bundled Highlight.js (`highlight.min.js` + `github-dark.min.css`)
- **Mermaid diagrams**: `AIContent` renders `.mermaid-source` elements; click opens `DiagramLightbox` (fullscreen zoom). Falls back to raw source on parse error.
- **Markdown rendering**: `renderMarkdown()` in `MessageBubble.tsx` — headers, bold/italic, lists, blockquotes, inline code, horizontal rules, file path auto-linking (`src/file.ts:42` → clickable)
- **State management**: `ChatContext` (messages + AI state), `SessionContext` (FSM + permissions + SSO), `VSCodeContext` (postMessage bridge)
- **WebSocket**: `useWebSocket.ts` — full lifecycle (connect → auth → history → join → messages → reconnect)
- **Typed commands**: `commands.ts` defines `IncomingCommand` / `OutgoingCommand` union types for the postMessage contract
- **Responsive layout**: `useContainerWidth` hook (ResizeObserver → `app-narrow/default/wide` CSS class)
- **Command palette**: `CommandPalette.tsx` — Cmd+K fuzzy search across all commands
- **Connection status**: `ConnectionStatus.tsx` — thin strip (connected/reconnecting/disconnected)
- **Command system**: `slashCommands.ts` — `/` actions, `@` agent scopes, `#` context injection

## 美学 2.0 Design Principles

Identity: **"Comfortable Intelligence"** — regardless of mode, eyes are relaxed, mind is active but unstressed, breathing is natural, flow is maintained.

### Four Modes
The WebView auto-adapts to VS Code's active theme via body classes:
- `.vscode-dark` — warm dark base (Claude.ai-inspired `#141210`)
- `.vscode-light` — Apple-cool off-white (`#F2F2F7`)
- `.vscode-high-contrast` — dark + stronger borders, no glass, AAA contrast
- `.vscode-high-contrast-light` — light + stronger borders, no glass, AAA contrast

Token architecture: theme-invariant tokens (typography, spacing, motion) in `:root`, theme-variant tokens (colors, materials, shadows) scoped to body class selectors. Highlight.js theme auto-switches via MutationObserver on body class.

### Pillar 1: Material Quality (视觉质感)
- Glass materials with `backdrop-filter: blur()` on header, modals, slash menu, FABs
- 5 material layers: `--material-ultra-thin` through `--material-chrome`
- Dark: warm glass `rgba(30,28,26,alpha)`. Light: cool glass `rgba(242,242,247,alpha)`
- High contrast: glass replaced with solid opaque backgrounds
- 0.5px Retina-ready borders (not 1px)
- Shadows: Apple three-layer recipe (dark), much softer (light), none (high contrast)

### Pillar 2: Kinetic Harmony (动态和谐)
- Spring physics for all motion: `--spring-snappy`, `--spring-gentle`, `--spring-bouncy`
- Duration: enter (350-500ms) > exit (200ms) — new content needs registration time
- Message animation: `translateY` only (no scale — avoids "popping")
- All animations interruptible, respect `prefers-reduced-motion`
- Animations preserved in high contrast (only glass/blur removed)

### Pillar 3: Flow State Protection (心流保护)
- Notifications follow severity hierarchy (status bar → inline → toast → modal)
- Keyboard shortcuts for every action; `Cmd+K` command palette
- AI thinking uses `useDeferredValue` — input never blocks during streaming
- Progressive disclosure: investigation steps collapsed by default, expandable

### Three-Channel Aesthetics (三通道美学)
- **Human → AI**: Intuitive input (slash commands, @mentions, #context injection)
- **AI → Human**: Zero cognitive burden (formatted responses, scannable tool logs)
- **AI ↔ AI**: Max signal per token (labeled text > JSON for inter-agent communication)

### AI Response Color Hierarchy (暖色層次)
Warm hierarchy in both modes — warm-on-dark (natural) and warm-on-cool (deliberate departure marking AI content as distinct).

**Dark mode** (warm analogous):
- h1: `#e8be82` warm gold | h2: `#d4a080` dusty copper | h3: `#a8b8a0` muted sage
- bold: `#f0e6d8` warm ivory | italic: `#b8a8c8` soft lavender
- inline-code: `#d4b898` chai | table: `#dcc0a0` desert sand

**Light mode** (deep warm on cool):
- h1: `#7A5C10` deep amber | h2: `#6B4D30` burnt sienna | h3: `#4A6040` forest sage
- bold: `#33302B` dark umber | italic: `#5E4E70` deep purple
- inline-code: `#6B5535` dark tea | table: `#5E4A25` dark sand

### Key Design Decisions
- **One accent color**: Violet (dark `#8b5cf6`, light `#7c3aed`) for all interactive elements
- **iMessage bubble DNA**: Flat own (violet), glass other (no border), warm AI (parchment)
- **Robot avatar**: Cute robot face for AI messages (antenna pulse + eye blink animation)
- **Apple sheet modals**: Blurred overlay, scale+translate enter, fast exit
- **Linear-style Kanban**: Flat rows with 2px status border (no bordered cards)
- **Responsive**: `useContainerWidth` hook (narrow <350px, default, wide >500px)
- **Markdown tables**: Pipe-delimited table parsing with warm desert sand headers

## Tool Parity Testing

Python and TypeScript tools must produce equivalent output. `make test-parity` validates this:

1. Checks `contracts/tool_contracts.json` matches Python Pydantic schemas
2. Validates TS tool output shapes against the contract
3. **Validates 11 subprocess tools** by calling the Python CLI (`python -m app.code_tools`) and checking `{success, data}` shape — done inside `extension/tests/validate_contract.js`
4. Runs cross-language parity tests (60+ tests across 13 dual-implementation tools)

```bash
make test-parity          # full validation (contract + shape + output comparison)
make update-contracts     # regenerate contracts after changing Python schemas
```

Contract output: `contracts/tool_contracts.json` (JSON Schema) + `extension/src/services/toolContracts.d.ts` (TypeScript interfaces). Regenerate after any schema change with `make update-contracts`.
