# Config CLAUDE.md

## Structure

```
config/
├── conductor.settings.yaml  # Non-sensitive settings (committed)
├── conductor.secrets.yaml   # Secrets (gitignored)
├── brain.yaml               # Brain orchestrator config (limits, core_tools, model)
├── brains/                  # Specialized Brain configs
│   └── pr_review.yaml       # PR Brain config (agents, budget_weights, post_processing)
├── workflows/               # pr_review.yaml (parallel_all_matching), code_explorer.yaml (first_match)
├── agents/                  # 19 agent .md files (YAML frontmatter + Markdown body)
│   └── pr_arbitrator.md     # Defense attorney for PR review (challenges findings)
├── swarms/                  # Swarm presets (agent group + parallel/sequential)
│   ├── pr_review.yaml       # 5-agent PR review swarm
│   └── business_flow.yaml   # 2-agent business flow tracing
├── prompts/                 # review_base.md, explorer_base.md (shared templates)
└── prompt-library/          # prompts.chat CSV (1500+ role prompts, `make update-prompt-library`)
```

## Agent & Prompt Design Principles

When creating or editing agent definitions (`config/agents/*.md`), system prompts (`prompts.py`), or workflow configs, follow these principles. Sources: [Anthropic Prompt Engineering](https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/claude-4-best-practices), [Context Engineering for Agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents). We also maintain a local copy of [prompts.chat](https://github.com/f/prompts.chat) (1500+ prompts) at `config/prompt-library/` — primarily used as **example references** when designing new agent roles, not as direct templates.

### 4-Layer Prompt Architecture (mandatory)

Every agent prompt — Brain or sub-agent — MUST follow this 4-layer structure. Each layer has a distinct purpose and MUST NOT bleed into another.

| Layer | Purpose | Where it lives | What goes here |
|-------|---------|----------------|----------------|
| **1. System Prompt** | Who the agent is, what it cares about, how it behaves | `system` parameter in LLM call | Agent identity, perspective, behavioral rules, answer format. **Each sub-agent gets its own system prompt** — no shared "generic agent" identity. Built from agent `.md` description + instructions. |
| **2. Tools** | What the agent can do and when to use each tool | `tools` parameter in LLM call | Tool definitions with clear descriptions. Treat tool descriptions as prompts — they guide behavior. `brain.yaml` core_tools + agent-specific tools from `.md` frontmatter. |
| **3. Skills & Guidelines** | Project-specific knowledge and reusable patterns | Appended to system prompt, clearly separated | Workspace layout, project docs (README/CLAUDE.md), investigation patterns (domain models first, scope searches, etc.), risk signals, budget. Shared across agents — same project context for all. |
| **4. User Messages** | The actual task plus focused context | `messages` parameter in LLM call | The query from Brain, plus any code_context snippet. Keep it specific and scoped to what the agent needs right now. **Never inject agent identity or role into user messages.** |

**Key rules:**
- Agent identity (Layer 1) MUST be in the system prompt, never in the user message. The old pattern of appending `## Your Role` to the query violates this — the agent's role defines how it processes ALL messages, not just one.
- Layer 3 (Skills & Guidelines) is shared context, not identity. Two agents in the same workspace see the same project docs and investigation patterns, but have different system prompts.
- Layer 2 (Tools) is curated per agent. An implementation tracer gets `get_callers` + `trace_variable`; a usage tracer gets `find_tests` + `test_outline`. The tool set IS part of the agent's capabilities.

### Anthropic Core Principles

1. **Right Altitude** — Not too vague ("investigate the code"), not too prescriptive ("call get_callers on the gate method"). Target: "Trace the complete lifecycle from trigger to final outcome."
2. **Examples over rule lists** — 3-5 diverse examples teach behavior better than a laundry list of edge-case bullets. Wrap in `<example>` tags.
3. **Explain why, not just what** — Claude generalizes from motivation. "Output will be read by TTS, so avoid ellipses" beats "never use ellipses."
4. **Positive framing** — Say what to do, not what not to do.
5. **Context over instructions** — Provide workspace layout, project docs, detected project roots (Layer 3). Let the model decide the investigation path.
6. **Three-layer language rule** — Not all forceful language is bad. Apply different tones depending on the context:
   - **Layer 1 (tool frequency/eagerness)**: Dial back. Newer models overtrigger on `CRITICAL: You MUST use this tool`. Use `Use this tool when...` instead.
   - **Layer 2 (efficiency/style preferences)**: Give reasons, not bare commands. `Avoid reading 300+ line files — use start_line/end_line to save tokens` beats `Do NOT read large files`. The word "avoid"/"prefer" is fine; bare `NEVER` without context is less effective.
   - **Layer 3 (safety/irreversible constraints)**: Forceful language is still appropriate. `ALWAYS use file= to limit diffs` (prevents truncation) and `NEVER skip hooks` (prevents data loss) are valid hard constraints. Anthropic's own docs use MUST/NEVER for safety guardrails.
7. **Minimal tool guidance** — If a human can't definitively say which tool to use, don't prescribe it. Let tool descriptions (Layer 2) guide the model.

### Multi-Agent Workflow Rules

8. **Role specialization** — Each agent has a distinct identity (Layer 1 system prompt). Shared investigation patterns belong in Layer 3, not Layer 1. Never add shared strategies to individual agent identities — this destroys role separation (proven by eval: 60% → 25% regression).
9. **Structured output via strategy** — Output format templates (e.g. code_review) are injected as a Layer 3 skill when the agent's frontmatter sets `strategy: code_review`. Don't inject investigation procedures for open-ended queries.
10. **Adversarial arbitration for PR reviews** — Sub-agents provide evidence FOR findings (prosecution). The arbitrator provides evidence AGAINST (defense). The synthesis LLM acts as judge, seeing both sides. The arbitrator does NOT adjust severity — it provides counter-evidence and a rebuttal confidence score.
11. **DO NOT FLAG list** — PR review agents have an explicit exclusion list: style/formatting, pre-existing issues, speculative concerns, secondary effects of the same root cause, design disagreements, generated/vendored code.
12. **Per-agent model selection** — Critical review dimensions (correctness) use the strong model; others use the explorer model. Set `model: strong` in the agent `.md` frontmatter.

### Agent `.md` File Design (informed by prompts.chat patterns)

13. **One clear role sentence** — Open with what the agent IS and what it traces. prompts.chat's "I want you to act as..." pattern works because it's unambiguous. Our equivalent: "You are investigating from the [perspective] side. Your goal is to trace [scope]." This becomes the core of the agent's Layer 1 system prompt.
14. **Goal, not procedure** — Define WHAT to find (domain models, service implementations, completion effects), not HOW to find it (don't say "first grep, then read_file, then get_callers").
15. **Short** — Agent instructions should be 50-150 words. prompts.chat averages 80 words. If you need more, you're probably being too prescriptive.
16. **Consult the prompt library** — Before writing a new agent role, search `config/prompt-library/prompts.csv` for similar roles. Study how they define constraints and scope. Use `for_devs=TRUE` filter for developer-focused prompts.

### Validation

17. **Test with eval** — Any prompt change must be validated with eval. For PR review: `eval/code_review/run.py --brain --verbose`. For exploration: `eval/agent_quality/run_bedrock.py --brain`. Check multiple modes — changes that help one can break another.
