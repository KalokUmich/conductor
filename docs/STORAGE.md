# Conductor Local Storage Architecture

## Overview

Conductor has two local storage systems:

1. **Workspace Index** (`{workspace}/.conductor/`) — code intelligence, project-specific config
2. **User Data** (`~/.conductor/`) — sessions, chat history, credentials, LLM logs

Both are gitignored and local-only. Online mode syncs to backend Postgres for collaboration.

---

## System 1: Workspace Index

Location: `{workspace-root}/.conductor/`

Automatically added to `.gitignore`. Contains only derived or project-specific data.

```
{workspace}/.conductor/
├── config.json              # Workspace scanning config (ignore patterns, limits)
├── repo_graph.json          # AST symbol index (auto-rebuilt, 30min TTL)
├── jira.json                # Jira site metadata only (NO tokens)
├── room_settings.json       # Chat room settings (output mode, code style)
└── uploads/                 # File attachments (local mode only)
    └── {filename}-{ts}.{ext}
```

**No changes needed** — this system is clean and well-scoped.

---

## System 2: User Data (`~/.conductor/`)

### Directory Structure (Target Design)

```
~/.conductor/                                    # Mode 0700 (user-only)
│
├── sessions.json                                # Global session registry
│
├── projects/                                    # Per-project data
│   └── {sanitized-workspace-path}/
│       ├── chat_history/
│       │   └── {session-id}.jsonl               # Chat messages (append-only)
│       └── llm_logs/
│           └── {session-id}.jsonl               # LLM request/response pairs
│
├── credentials/                                 # Sensitive data (mode 0600)
│   ├── sso.json                                 # Cached SSO identity
│   └── tokens.json                              # Integration tokens (encrypted)
│
├── settings.json                                # User preferences
│
└── cache/                                       # Ephemeral, safe to delete
    └── paste/                                   # Large paste content (hash-addressed)
```

### File Formats

#### `sessions.json` — Session Registry
```json
[
  {
    "roomId": "abc-123",
    "workspacePath": "/home/kalok/abound-server",
    "workspaceName": "abound-server",
    "displayName": "Code Review Session",
    "ssoEmail": "kalok.kam@fintern.ai",
    "createdAt": "2026-04-04T10:00:00Z",
    "lastActiveAt": "2026-04-04T15:30:00Z",
    "messageCount": 47,
    "mode": "local"
  }
]
```

#### `chat_history/{session-id}.jsonl` — Chat Messages (JSONL)
Each line is one message. Append-only for performance.
```jsonl
{"id":"msg-1","type":"text","userId":"u1","displayName":"Alice","role":"host","content":"Hello","ts":1712234400}
{"id":"msg-2","type":"ai_answer","userId":"ai","displayName":"Brain","role":"ai","content":"...","ts":1712234405,"aiData":{"model":"claude-sonnet","tokens":{"in":1200,"out":450}}}
{"id":"msg-3","type":"code_snippet","userId":"u1","displayName":"Alice","role":"host","content":"","ts":1712234410,"codeSnippet":{"code":"...","relativePath":"src/auth.ts","startLine":10,"endLine":25,"language":"typescript"}}
{"id":"msg-4","type":"file","userId":"u1","displayName":"Alice","role":"host","content":"","ts":1712234415,"fileId":"local-123","originalFilename":"screenshot.png","mimeType":"image/png","sizeBytes":45000}
```

#### `llm_logs/{session-id}.jsonl` — LLM Request/Response Pairs
For cost tracking, debugging, and compliance.
```jsonl
{"ts":1712234405,"sessionId":"abc","query":"Explain auth flow","model":"claude-sonnet-4-20250514","provider":"bedrock","tokens":{"input":1200,"output":450},"duration_ms":3200,"tools_used":["grep","read_file"],"agent":"dynamic_code_explanation"}
{"ts":1712234450,"sessionId":"abc","query":"Find security issues","model":"claude-haiku-4-20250414","provider":"bedrock","tokens":{"input":800,"output":200},"duration_ms":1500,"tools_used":["grep"],"agent":"security_reviewer"}
```

#### `credentials/sso.json` — SSO Identity Cache
```json
{
  "identity": {
    "email": "kalok.kam@fintern.ai",
    "name": "Kalok Kam",
    "provider": "google"
  },
  "storedAt": 1712234400000,
  "expiresAt": 1712407200000
}
```

#### `credentials/tokens.json` — Integration Tokens (Encrypted)
Encrypted at rest using OS-derived key. Plaintext structure:
```json
{
  "jira": {
    "accessToken": "ey...",
    "refreshToken": "ey...",
    "expiresAt": 1712320800000,
    "cloudId": "xxx",
    "siteUrl": "https://mysite.atlassian.net"
  }
}
```

#### `settings.json` — User Preferences
```json
{
  "theme": "dark",
  "defaultModel": "claude-sonnet-4-20250514",
  "explorerModel": "claude-haiku-4-20250414",
  "autoApply": false,
  "retentionDays": 90,
  "llmLogging": true
}
```

---

## Security

| Requirement | Implementation |
|-------------|---------------|
| Directory permissions | `~/.conductor/` created with mode `0700` |
| Credentials file | `credentials/` mode `0600`, encrypted at rest |
| Chat history | Not encrypted (contains user-generated content) |
| LLM logs | Not encrypted (opt-in, user controls) |
| Jira tokens | Encrypted in `tokens.json` (fallback from OS keychain) |
| .gitignore | `.conductor/` auto-added to workspace gitignore |

---

## Retention Policy

| Data | Default Retention | Configurable |
|------|-------------------|--------------|
| Sessions | 90 days since last active | Yes, `settings.json` |
| Chat history | 90 days since last active | Yes |
| LLM logs | 30 days | Yes |
| Credentials | Until logout/expiry | No |
| Cache (paste) | 7 days | No |

---

## Migration Path

| Phase | From | To |
|-------|------|-----|
| Phase 3 (done) | VS Code globalStorageUri | `~/.conductor/` |
| Phase 4 (future) | JSON chat_history | JSONL (append-only) |
| Phase 4 (future) | No LLM logs | `llm_logs/{session}.jsonl` |
| Phase 4 (future) | VS Code SecretStorage only | `credentials/tokens.json` (encrypted fallback) |
| Phase 5 (future) | No retention | Auto-cleanup stale data |

---

## Platform Support

| Platform | `~/.conductor/` Location | Credentials |
|----------|--------------------------|-------------|
| Linux | `/home/{user}/.conductor/` | `credentials/tokens.json` (encrypted) |
| macOS | `/Users/{user}/.conductor/` | macOS Keychain preferred, file fallback |
| Windows | `C:\Users\{user}\.conductor\` | Windows Credential Manager preferred |
| WSL | `/home/{user}/.conductor/` | Same as Linux |
| Web (local) | N/A — use backend storage | Server-side encrypted |
| CLI | Same as OS | Same as OS |

Override: `CONDUCTOR_DATA_DIR=/custom/path`
