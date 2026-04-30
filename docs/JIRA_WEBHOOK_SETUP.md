# Jira Webhook Auto-Investigate — Setup Guide

Phase 7.7.11 MVP. When a new Jira ticket is created, Conductor runs a
short LLM triage pass and posts the result back as a comment. This doc
covers how to register the webhook on the Atlassian side and how to
configure the matching secret on the Conductor side.

## What gets posted to the ticket

Sample comment posted by the bot:

```
🤖 Conductor auto-triage (initial pass — confirm before acting)

**Triage**: bug — webhook reliability regression on the payment
provider integration.

**Likely components**:
- abound-server / Payment
- abound-server / JBE (default)

**First investigation steps**:
1. grep `WebhookRetryConfig` under `src/payment/`
2. Check recent commits to `PaymentWebhookHandler.java`
3. Look for retry / backoff config in `application.yml` and
   the deployment overrides

**Risks / unknowns**:
- Ticket doesn't say which provider — confirm before touching code.
```

This is a single LLM call (no tool dispatch, no workspace mount). Cost
is bounded — typically <500 input tokens, <500 output tokens per
ticket. **It is not a full investigation** — it's a first read meant to
save the assignee 2-3 minutes of triage.

## What you need

- An Atlassian Cloud site admin account.
- The Conductor backend reachable from the public internet at a stable
  URL (e.g. via load balancer or ngrok). Atlassian Cloud will not call
  internal-only addresses.
- The Atlassian readonly service-account token already configured (see
  `docs/PR_REVIEW_INFRA_PLAN.md` or `config/conductor.secrets.yaml`).
  The same client is used to fetch the ticket body and post the
  comment.

## Step 1 — Conductor side

Generate a webhook token (any random string, 32+ chars recommended):

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Add it to `config/conductor.secrets.local.yaml` (gitignored) under
the existing `atlassian_readonly` block:

```yaml
atlassian_readonly:
  site_url: "https://yourcompany.atlassian.net"
  email: "bot@yourcompany.com"
  api_token: "<existing-classic-token>"
  webhook_token: "<the-string-you-just-generated>"
```

Or via env var on the deployed backend:

```bash
CONDUCTOR_JIRA_WEBHOOK_TOKEN=<the-string>
```

Restart the backend. The webhook receiver becomes active at:

```
POST {your_backend_url}/api/webhooks/jira?token=<the-string>
```

Until the env var / secret is set, the receiver returns **503** (so
testing hits without configuring the token will fail loudly rather
than silently accepting all traffic).

## Step 2 — Atlassian side

In Atlassian Cloud:

1. Go to **Settings → System → Webhooks** (requires site admin).
2. Click **Create a Webhook**.
3. Fill in:
   - **Name**: `Conductor auto-investigate`
   - **Status**: Enabled
   - **URL**: `https://your-backend.example.com/api/webhooks/jira?token=<the-string>`
   - **Description**: `Posts initial triage notes to new tickets`
   - **Events**: check **Issue → created** (`jira:issue_created`).
     Leave `issue_updated` UNchecked — it's noisy and Conductor
     filters it out anyway in MVP.
   - **JQL filter** *(strongly recommended)*: scope to the projects /
     assignees you actually want auto-triaged. Examples:
     - `project IN (DEV, PAY)` — only your team's projects
     - `assignee in (currentUser())` — only tickets assigned at
       creation time
     - `priority IN (Highest, High)` — skip low-priority noise

4. Save.

Atlassian's UI also lets you **send a test request** — it'll POST a
sample payload to the URL. Conductor logs each inbound webhook with
`[Jira webhook] received event=... issue=...` — verify the test request
shows up in your backend logs. The first real ticket creation should
appear there within a few seconds.

## How it gets verified

The receiver is hardened against the three obvious problems:

| Failure mode | Conductor response |
|---|---|
| No `?token=` query param | **401** invalid or missing token |
| Wrong token | **401** invalid or missing token (constant-time compare) |
| `webhook_token` not set in Conductor secrets | **503** webhook receiver not configured |
| Atlassian readonly client not configured | **503** jira_readonly_client not configured |
| AI provider not initialized | **503** AI provider not initialised |
| Subscribed event but missing `issue.key` | **200** with `skipped: missing_issue_key` |
| Unsubscribed event (e.g. `issue_updated`) | **200** with `skipped: event_not_subscribed` |
| Happy path | **200** with `scheduled: <issue-key>` (background task) |

The investigation runs **after** the 200 response — Jira Cloud requires
webhook handlers to respond within 10s and we won't make them wait for
LLM calls. If the LLM call or comment-write fails, the failure is
logged on the Conductor side; Jira is not retried-or-notified.

## Disabling

Either:
- Disable the webhook in Atlassian's admin UI (preserves config), or
- Clear `webhook_token` in Conductor secrets (causes any inbound webhook
  request to 503).

## Troubleshooting

**Webhook fires but no comment appears**:
- Check backend logs for `[Jira webhook]` entries
- Verify `jira_readonly_client.add_comment` works manually:
  `curl https://backend/api/integrations/jira/readonly/whoami` should
  return the bot account profile.
- Service account needs `Add comments` permission on the project.

**"unauthorized" responses on the Atlassian side**:
- Site admin lost permission, or token is stale. Regenerate the token
  on `https://id.atlassian.com/manage-profile/security/api-tokens` and
  update Conductor secrets.

**Empty or low-quality triage notes**:
- Ticket description was thin. The bot is told to refuse to speculate
  when there's no detail; that's working as intended. The fix is to
  ask the reporter for more context, not to "improve the bot".
