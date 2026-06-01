# Microsoft Teams Bot — Deployment Guide

> **Purpose**: turn Conductor's AI summarization capability into an internal Teams bot that employees can invoke with `@Conductor summarize`.
>
> **Two audiences — two sets of tasks**:
> - **DevOps / IT** own everything in Azure Portal + Teams Admin Center (§A sections).
> - **Backend Developer** owns everything in the Conductor backend code and local/remote hosting (§B sections).
>
> **Two phases**:
> - **Phase 1 — Connectivity Proof**: no AI yet. Stand up a local backend, expose it via a tunnel, confirm Teams can reach it and the bot can reply "hello". Small audience (1–3 test users). Goal: prove the pipe works end-to-end.
> - **Phase 2 — Production AI Rollout**: wire the real summarizer, deploy backend to production hosting, distribute to the whole company through the Teams Admin Center.
>
> Do **not** skip Phase 1. Most integration bugs (JWT validation, firewall rules, manifest typos) are easier to find without AI noise.

---

## What we already have

- ✅ **Azure AD App Registration** with `Application (client) ID`, `Directory (tenant) ID`, `Client Secret`, and Microsoft Graph `ChatMessage.Read.All` permission.
- ✅ **Conductor backend (FastAPI)** running locally, with existing AI summarization endpoints.

## What is still missing

1. An **Azure Bot Service** resource (wraps the App Registration as a bot identity; enables the Microsoft Teams channel).
2. A **public HTTPS messaging endpoint** that Teams can POST Activities to.
3. A **Teams App package** (`manifest.json` + two icons, zipped) that tells Teams the bot exists and what commands it supports.
4. **Distribution** — sideloading in Phase 1, org-wide Admin Center upload in Phase 2.

---

## Decisions to make before starting

| Item | Options | Recommendation |
|---|---|---|
| Tenant mode | Single-tenant / Multi-tenant | **Single-tenant** (internal tool) |
| Bot display name | e.g. `Conductor`, `Fintern Conductor` | Align with Product |
| Phase 1 public URL | ngrok free / ngrok paid / Cloudflare Tunnel | **ngrok paid** or **Cloudflare Tunnel** (stable subdomain) |
| Phase 2 production hosting | Azure App Service / AWS ECS / existing internal K8s | **Azure App Service** (same ecosystem as Bot Framework) |
| Phase 2 production domain | e.g. `conductor-bot.fintern.internal` | Needs DNS + trusted TLS cert |
| Azure subscription & resource group | Reuse / new | New RG: `rg-conductor-teams` |

---

---

# Phase 1 — Connectivity Proof

**Goal**: A developer `@mentions` the bot in a private Teams channel; the request arrives at a local backend through a tunnel; the backend replies with a plain text echo; the message shows up in Teams.

**Scope**: 1–3 test users, the developer's own Teams account, no org-wide distribution, no AI.

**Expected duration**: half a day once credentials are in hand.

## §A.1 DevOps / IT tasks for Phase 1

### A.1.1 Create the Azure Bot Service resource

The Bot Service is a lightweight wrapper — it does not host code. Its job is to route messages between Teams and our backend, and to sign Bearer JWTs using the existing App Registration identity.

1. [Azure Portal](https://portal.azure.com) → search **Azure Bot** → **Create**.
2. Fill in:
   - **Bot handle**: globally unique, e.g. `fintern-conductor-bot` (internal identifier, not shown to users)
   - **Subscription / Resource group**: per decisions table
   - **Pricing tier**: **F0 (Free)** — the Teams channel has no rate limit on F0; **do not pick S1**
   - **Microsoft App ID**: select **"Use existing app registration"**
     - **Type**: Single Tenant
     - **App ID**: the existing Client ID
     - **App tenant ID**: the existing Tenant ID
3. **Review + create** → **Create**. Wait ~30 seconds for deployment.

### A.1.2 Set a placeholder messaging endpoint

The developer does not yet have a public URL. Put a placeholder in so the resource saves cleanly — you will replace it in A.1.4.

1. Bot resource → left menu **Settings → Configuration**.
2. **Messaging endpoint**: `https://example.com/placeholder`
3. Leave **Enable Streaming Endpoint** off.
4. **Save**.

### A.1.3 Enable the Microsoft Teams channel

1. Bot resource → left menu **Settings → Channels**.
2. Click **Microsoft Teams** → select **Microsoft Teams Commercial** → **Agree & Apply**.
3. Confirm Teams appears in the Channels list with status `Running`.

### A.1.4 Wait for the developer's tunnel URL, then update the endpoint

The developer (see §B.1) will stand up a local backend and a public tunnel (ngrok or Cloudflare Tunnel), and will send you a URL shaped like:

```
https://<random>.ngrok-free.app/api/integrations/teams/bot/messages
```

1. Go back to **Settings → Configuration** on the Bot resource.
2. Replace the placeholder with the developer's URL.
3. **Save**.

### A.1.5 Create a minimal Teams App manifest and sideload it

For Phase 1 we do **not** use the Admin Center. We sideload directly into the developer's personal Teams app list. This requires "Upload custom apps" to be allowed by the tenant's Teams app setup policy (Teams Admin Center → **Teams apps → Setup policies → Global** → enable **Upload custom apps**). If this toggle is off company-wide, enable it just for the test users via a custom policy.

Create a folder with three files. Name it `conductor-teams-app-phase1/`.

**`manifest.json`**:

```json
{
  "$schema": "https://developer.microsoft.com/en-us/json-schemas/teams/v1.17/MicrosoftTeams.schema.json",
  "manifestVersion": "1.17",
  "version": "0.1.0",
  "id": "<NEW GUID — generate a fresh one, do NOT reuse the App ID>",
  "packageName": "ai.fintern.conductor.phase1",
  "developer": {
    "name": "Fintern",
    "websiteUrl": "https://fintern.ai",
    "privacyUrl": "https://fintern.ai/privacy",
    "termsOfUseUrl": "https://fintern.ai/terms"
  },
  "icons": {
    "color": "color.png",
    "outline": "outline.png"
  },
  "name": {
    "short": "Conductor (Dev)",
    "full": "Conductor AI Assistant — Dev"
  },
  "description": {
    "short": "Internal dev build of Conductor bot.",
    "full": "Phase 1 connectivity test build. Not for general use."
  },
  "accentColor": "#4F46E5",
  "bots": [
    {
      "botId": "<the existing App Registration Client ID>",
      "scopes": ["personal", "team", "groupChat"],
      "supportsFiles": false,
      "isNotificationOnly": false
    }
  ],
  "permissions": ["identity", "messageTeamMembers"],
  "validDomains": ["<ngrok host without https://, e.g. abc123.ngrok-free.app>"]
}
```

**Icons**:
- `color.png` — 192×192 PNG, full-bleed colored background (not transparent)
- `outline.png` — 32×32 PNG, white shape on fully transparent background

Any placeholder icons are fine in Phase 1. Teams rejects the upload if dimensions are wrong.

**Package**:

```bash
cd conductor-teams-app-phase1
zip ../conductor-teams-app-phase1.zip manifest.json color.png outline.png
```

Zip **from inside the directory** so the three files sit at the zip root.

**Sideload in the Teams desktop client**:

1. Teams → left rail **Apps** → bottom-left **Manage your apps** → **Upload an app** → **Upload a custom app**.
2. Select `conductor-teams-app-phase1.zip`.
3. After upload, click **Add** → **Add to a team** → pick a private test channel.

If **Upload a custom app** is missing from the menu, the app setup policy (above) hasn't propagated yet — wait 15 minutes and try again, or check the policy assignment.

### A.1.6 Hand off to the developer

Give the developer (via 1Password / Azure Key Vault / similar — **not** Slack/Teams/email):

| Variable | Value |
|---|---|
| `CONDUCTOR_TEAMS_APP_ID` | App Registration Client ID |
| `CONDUCTOR_TEAMS_APP_PASSWORD` | App Registration Client Secret |
| `CONDUCTOR_TEAMS_APP_TENANT_ID` | Tenant ID |
| `CONDUCTOR_TEAMS_APP_TYPE` | `SingleTenant` |

## §B.1 Backend developer tasks for Phase 1

### B.1.1 Stand up a public tunnel

Pick one:

- **ngrok** (simplest): `ngrok http --domain=<your-reserved-subdomain>.ngrok-free.app 8000`. Free tier works for Phase 1 but the subdomain changes on restart unless you reserve one (paid).
- **Cloudflare Tunnel** (free, stable): `cloudflared tunnel --url http://localhost:8000`. Gives a stable `*.trycloudflare.com` URL.

Send DevOps the full URL: `https://<host>/api/integrations/teams/bot/messages`.

### B.1.2 Add a minimal router at `backend/app/integrations/teams/`

Create a new package following the same shape as `backend/app/integrations/jira/`. Phase 1 deliberately **skips JWT validation** — we just want to see requests arrive. Add a `TODO(phase2)` comment so it does not ship to prod.

- `backend/app/integrations/teams/__init__.py`
- `backend/app/integrations/teams/router.py` — single handler:
  - Accept `POST /api/integrations/teams/bot/messages`
  - Log the full Activity JSON (redact `serviceUrl` token if present)
  - If `activity.type == "message"`, call Bot Framework's reply API to post `"Phase 1 OK — received: <text>"` back to the same conversation
  - Return `200 OK` within ~5 seconds (Teams times out otherwise)
- `backend/app/integrations/teams/models.py` — Pydantic models for the Activity fields you actually use (`type`, `id`, `serviceUrl`, `from`, `conversation`, `text`)

Register the router in `backend/app/main.py` alongside the Jira router.

### B.1.3 Bot Framework reply mechanics

To reply, the backend must:

1. Get an OAuth token from `https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token` using the App ID + Secret and scope `https://api.botframework.com/.default`. Cache for ~50 minutes.
2. POST to `<activity.serviceUrl>/v3/conversations/<conversation.id>/activities/<activity.id>` with body:
   ```json
   { "type": "message", "text": "Phase 1 OK — received: hello" }
   ```
   and header `Authorization: Bearer <token>`.

The Microsoft-supplied SDK `botbuilder-core` wraps this, but for Phase 1 two `httpx` calls are simpler and make the token flow obvious in logs. Your choice.

### B.1.4 Config wiring

Add a `TeamsSettings` / `TeamsSecretsConfig` pair in `backend/app/config.py` that mirrors `JiraSettings`. Expose the four env vars from §A.1.6. Do not commit secrets.

### B.1.5 Smoke test

Before asking anyone to @mention the bot, from the developer box:

```bash
# Expected: 401 Unauthorized (no JWT) OR 200 (if JWT validation is deferred to Phase 2 and request body is parseable)
curl -i -X POST https://<tunnel-host>/api/integrations/teams/bot/messages \
  -H "Content-Type: application/json" \
  -d '{"type":"message","text":"test"}'
```

A `404` means the router is not registered. A `502` means the tunnel is down. Both must be fixed before Phase 1 testing.

## Phase 1 verification checklist

- [ ] Bot Service resource created, Teams channel shows `Running`
- [ ] Messaging endpoint points at the developer's live tunnel URL
- [ ] The `.zip` sideloads cleanly into the developer's Teams client
- [ ] Bot appears in the test channel's member list after `Add to a team`
- [ ] `@Conductor (Dev) hello` in the channel produces a backend log entry within 2 seconds
- [ ] Backend reply `"Phase 1 OK — received: hello"` appears in the channel within 5 seconds
- [ ] Azure Bot resource → **Test in Web Chat** also produces the same echo (independent of Teams — useful when Teams caches misbehave)

Once all boxes check, **Phase 1 is done**. Tear nothing down — Phase 2 reuses the same Bot resource, App Registration, and tunnel (temporarily).

---

---

# Phase 2 — Production AI Rollout

**Goal**: replace the echo with real AI summarization; deploy the backend to stable production hosting; distribute the app to the whole company through the Teams Admin Center.

## §A.2 DevOps / IT tasks for Phase 2

### A.2.1 Production backend hosting

Provision one of:

- **Azure App Service** (recommended) — native fit with Bot Framework, easy App Insights integration. Use Linux, Python runtime matching the backend's `pyproject.toml`.
- **AWS ECS + ALB + ACM certificate** — if the rest of Fintern infra lives in AWS.
- **Existing internal Kubernetes** — if there is a shared platform.

Requirements in all cases:

- **HTTPS with a trusted-CA certificate** (Let's Encrypt / Azure-managed / ACM). Self-signed certs are rejected by Teams.
- **TLS 1.2 or newer**.
- **DNS**: a stable hostname, e.g. `conductor-bot.fintern.ai`.
- **Inbound allow-list**: `POST /api/integrations/teams/bot/messages`, plus `/healthz` for load balancer probes. Everything else can stay internal.
- **Optional IP allow-list**: Bot Framework sources are `52.112.0.0/14` and `52.122.0.0/15` ([Microsoft reference](https://learn.microsoft.com/en-us/azure/bot-service/bot-service-resources-bot-framework-faq)).
- **Outbound allow-list** — the backend must reach:
  - `https://login.microsoftonline.com/*` (token acquisition + JWT signing keys)
  - `https://smba.trafficmanager.net/*` (outbound replies to Teams)
  - `https://graph.microsoft.com/*` (reading channel message history)
  - whatever the existing AI provider needs (Bedrock / Anthropic — Claude only)
- **Secrets**: inject the four `CONDUCTOR_TEAMS_*` env vars via App Service Configuration / AWS Secrets Manager / Kubernetes Secret. Never bake them into the image.

### A.2.2 Swap messaging endpoint to production

Azure Bot resource → **Settings → Configuration** → update **Messaging endpoint** to the production URL → **Save**. Phase 1 tunnel can now be retired.

### A.2.3 Bump and repackage the manifest for org-wide distribution

Duplicate the Phase 1 manifest folder as `conductor-teams-app-prod/` and change:

- `"version": "1.0.0"` (was `0.1.0`)
- `"id"`: **new fresh GUID** — this is a different app from the Phase 1 build, so users can have both installed during migration
- `"packageName": "ai.fintern.conductor"` (drop `.phase1`)
- `"name.short": "Conductor"` (drop `(Dev)`)
- `"validDomains": ["conductor-bot.fintern.ai"]` (production host)
- Add `"commandLists"` inside the bot entry:
  ```json
  "commandLists": [
    {
      "scopes": ["team", "groupChat"],
      "commands": [
        { "title": "summarize", "description": "Summarize recent messages in this channel" },
        { "title": "help", "description": "Show available commands" }
      ]
    }
  ]
  ```
- Replace placeholder icons with the final branded assets from Product.

Rezip as `conductor-teams-app-prod.zip`. Validate with [Teams App Validator](https://dev.teams.microsoft.com/appvalidation.html) before uploading.

### A.2.4 Upload to Teams Admin Center

Requires **Teams Service Admin** or **Global Admin** role.

1. [Teams Admin Center](https://admin.teams.microsoft.com) → **Teams apps → Manage apps** → **Actions → Upload new app** → **Upload**.
2. Select `conductor-teams-app-prod.zip`.
3. Open the uploaded app → set **Publishing status** to `Allowed`.

### A.2.5 Permission policy (optional gating)

To gate rollout:
1. **Teams apps → Permission policies → Add** a new policy that explicitly allows Conductor and blocks unknown third-party apps (if that matches corp baseline).
2. Assign the policy to a pilot group via **Users** → batch assign.
3. Expand to everyone after one week of successful pilot.

To skip gating, leave the Global policy as-is — all users get the app.

### A.2.6 User-facing install

Users in the Teams client: **Apps → Built for your org → Conductor → Add**, or **Add to a team** to install in a channel. Rollout may take up to 24 hours to appear in all clients (Teams caches the catalog aggressively).

### A.2.7 Monitoring & alerts

Forward backend logs to App Insights / CloudWatch. Set alerts on:

- JWT validation failure rate > 5% over 10 minutes (misconfiguration or attack)
- 5xx response rate > 1% over 10 minutes
- Outbound call to `smba.trafficmanager.net` failure rate > 1% (Teams reply failures)
- Client Secret expiry at `T-30 days` (calendar reminder — Azure AD does not email by default)

### A.2.8 Secret rotation

App Registration secrets expire after at most 2 years. On rotation:

1. Azure Portal → App Registrations → Conductor → **Certificates & secrets** → **New client secret**.
2. Update the production `CONDUCTOR_TEAMS_APP_PASSWORD` env var in App Service / Secrets Manager.
3. Rolling restart the backend.
4. Verify one test `@Conductor summarize` end-to-end.
5. Only then delete the old secret.

## §B.2 Backend developer tasks for Phase 2

### B.2.1 Proper JWT validation

Replace the Phase 1 pass-through with full Bot Framework JWT validation:

- Fetch signing keys from `https://login.botframework.com/v1/.well-known/openidconfiguration` (cache 24h).
- Validate `iss`, `aud` (must equal `CONDUCTOR_TEAMS_APP_ID`), `exp`, and signature on every incoming request.
- Reject anything else with `401`.

The `botbuilder-core` SDK handles this; wiring it into FastAPI takes ~50 lines. Alternative: hand-rolled validation with `PyJWT` + `cryptography` — fine if the team prefers fewer dependencies, but test carefully.

### B.2.2 Command parser

Parse the message text after stripping the bot `@mention`:

- `summarize` → default channel summary
- `summarize --with-context` → deferred, depends on Phase 12 Knowledge Base
- `help` → post command list
- unknown → friendly "try `summarize` or `help`"

### B.2.3 Read channel history via Graph API

Use the existing `ChatMessage.Read.All` permission:

- For a team channel: `GET /teams/{team-id}/channels/{channel-id}/messages?$top=50`
- For a group chat: `GET /chats/{chat-id}/messages?$top=50`

Team ID and channel ID are both on the incoming Activity (`activity.channelData.team.id`, `activity.channelData.channel.id`). Use the **app-only** token flow (same client credentials as for Bot Framework replies, but with scope `https://graph.microsoft.com/.default`).

### B.2.4 Invoke the existing summarizer

Reuse the summarization prompt/pipeline already serving Azure DevOps. Wrap it behind an internal function (`summarize_conversation(messages: list[str]) -> SummaryResult`) so both Teams and AzDO call the same thing. Do not duplicate prompts.

Token budget: cap at ~50 messages or ~8k input tokens, whichever comes first. Paginate if the channel thread is larger; the initial version can simply summarize "the last 50 messages" and state the scope in the reply.

### B.2.5 Adaptive Card formatter

Plain text replies are fine for Phase 1 but look cheap in Phase 2. Create `backend/app/integrations/teams/formatter.py` producing Adaptive Card JSON (schema 1.5) with sections:

- **Summary** (1-paragraph recap)
- **Key decisions** (bullet list)
- **Action items** (bullet list with assignee if detected)
- **Risk level** (Low / Medium / High badge)

Test cards in the [Adaptive Card Designer](https://adaptivecards.io/designer/) before wiring.

### B.2.6 Observability

Log per-request:
- `conversation.id`, `from.aadObjectId`, command, latency, model used, input/output tokens
- Any Graph API call failures with the upstream error code
- Any Bot Framework reply failures

Record via task-hierarchy telemetry (the `task` table), tagged `integration=teams`.

### B.2.7 Handoff of new env vars

If Phase 2 needs additional config (e.g. `CONDUCTOR_TEAMS_MAX_HISTORY=50`), document them in `docs/GUIDE.md §21` and send DevOps the updated list **before** they cut the production config.

## Phase 2 verification checklist

- [ ] Production backend reachable at `https://conductor-bot.fintern.ai/healthz` from outside the VPN
- [ ] Messaging endpoint on the Bot resource points at production
- [ ] `curl -X POST` to the messaging endpoint without a JWT returns `401` (not `200`, not `404`)
- [ ] Validator accepts the production `.zip`
- [ ] Admin Center shows the app with status `Allowed`
- [ ] A non-developer test user can install the app and `@Conductor summarize` in a real channel
- [ ] Adaptive Card renders correctly on Teams desktop, Teams web, and Teams mobile
- [ ] App Insights / CloudWatch receives logs; alerts fire when tested (e.g. deliberately invalid JWT)
- [ ] Secret rotation runbook (§A.2.8) has been dry-run once

---

## Appendix

### FAQ

**Q. Why not just use Graph API and skip the Bot?**
Graph only reads history; it cannot receive a push when a user `@mentions` us. The `@mention` trigger requires Bot Framework webhooks.

**Q. Can the bot read every message in every channel?**
No. It sees messages in channels/chats **where it has been explicitly added**, and the `@mention` payload gives it permission to read that conversation's recent history (via the Graph permission). It cannot silently monitor the tenant.

**Q. Do we need to publish to Microsoft AppSource?**
No. AppSource is for external SaaS. Internal-only apps use the Admin Center "Custom app" path and skip all Microsoft review.

**Q. How does a Teams user map to a Conductor identity?**
Activities carry `from.aadObjectId` (Azure AD object ID), which matches the ID from existing Fintern SSO. Backend joins on this field.

**Q. What if the Phase 1 tunnel URL changes mid-test?**
Update the messaging endpoint (§A.1.4) and the manifest's `validDomains` (§A.1.5), then re-sideload the new `.zip`. Bot ID and App Registration are unchanged.

**Q. Does Phase 2 require tearing down anything from Phase 1?**
No. Keep the Bot Service resource, App Registration, and Client Secret. The Phase 1 manifest can stay sideloaded in dev accounts during Phase 2 rollout — it points at dev tunnels, Phase 2 points at production. Users are unaffected.

### Credentials inventory

Stored in the shared secrets vault after Phase 1:

| Secret | Owner | Where used |
|---|---|---|
| App Registration Client Secret | DevOps | Backend env `CONDUCTOR_TEAMS_APP_PASSWORD` |
| Bot Service resource ID | DevOps | Azure Portal navigation only |
| Tenant ID | shared | Backend env + manifest |
| Production TLS cert | DevOps | Load balancer / App Service |
| Ngrok auth token (Phase 1) | Developer | Local dev machine only |

---

**Document owner**: Backend team (file issues on the Conductor repo)
**Last updated**: 2026-04-14
