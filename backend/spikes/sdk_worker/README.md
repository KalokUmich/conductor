# Step 05 — SDK worker spike (GATE)

Throwaway probe for the agent-SDK migration. Proves design §7's 4 seams: can a
Claude worker run on the Claude Agent SDK, using ONLY our tools (proxied through
the same `CachedToolExecutor` the in-house worker uses), hit the Fact Vault, and
return a result `condense_result()` accepts unchanged?

**Not imported by `app/`** — `make test` is unaffected. Run each seam directly:

```bash
cd backend
../.venv/bin/python -m spikes.sdk_worker.seam1_proxy        # no Bedrock
../.venv/bin/python -m spikes.sdk_worker.seam2_vault        # no Bedrock
../.venv/bin/python -m spikes.sdk_worker.seam3_modelswitch  # REAL Bedrock (Haiku+Sonnet)
../.venv/bin/python -m spikes.sdk_worker.seam4_allmcp       # REAL Bedrock (4 runs)
```

Pre-flight Bedrock runs with `bash scripts/refactor/check_creds.sh`.

## Components
- `runner.py` — shared harness: wraps a representative 5-tool subset
  (read_file/grep/list_files/file_outline/find_symbol) as SDK `@tool`s delegating
  to `CachedToolExecutor`; builds `ClaudeAgentOptions` with Bedrock env; drives
  `query()`; maps the result to an `AgentResult`-shaped shim (`SdkAgentResult`).
- `seam{1..4}_*.py` — one runnable seam each.

## Results & findings
See the "Step 05 spike" entry in `docs/REFACTOR_EXECUTION_LOG.md` for the
seam-by-seam verdict and the go/no-go decision.

Key Step-06 carry-over finding: the spike used a generic `{"params": dict}` input
schema, which makes the model occasionally mis-shape tool args (observed once on
`find_symbol`; the model self-corrected). Step 06 should give each `@tool` a
**typed input schema derived from our existing Pydantic param models** so tool
calls validate first-try. Prompt caching is active for free on the SDK path
(cache_read ~66–70K tokens/run observed).
