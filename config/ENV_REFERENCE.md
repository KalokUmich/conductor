# Environment Variable Reference

When deploying Conductor to ECS (or any container platform), set these environment variables to override the YAML config. All are optional for local dev but required for production.

## Database & Cache

| Variable | Example | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://conductor:SECRET@my-rds.abc.eu-west-2.rds.amazonaws.com:5432/conductor` | Full async Postgres URL. Overrides `postgres.*` + `secrets.postgres.*` from YAML entirely. |
| `REDIS_URL` | `redis://:SECRET@my-elasticache.abc.euw2.cache.amazonaws.com:6379/0` | Full Redis URL. Overrides `redis.*` + `secrets.redis.*` from YAML entirely. |

## Server

| Variable | Example | Description |
|---|---|---|
| `BACKEND_HOST` | `0.0.0.0` | Bind address. |
| `BACKEND_PORT` | `8000` | Bind port. |

## AI Providers (Claude only — if not using YAML secrets)

Bedrock auth priority is **bearer > static keys > SSO profile > IAM role**. In deployed/ECS, set none of the credential vars below and let the task role resolve via the default credential chain.

| Variable | Example | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | `sk-ant-...` | Anthropic Direct API key. |
| `CONDUCTOR_AWS_BEARER_TOKEN` | `bedrock-api-key-...` | Bedrock API key (bearer → `AWS_BEARER_TOKEN_BEDROCK`); highest priority. Long-lived secret — scope its IAM policy tightly. |
| `CONDUCTOR_AWS_PROFILE` | `bedrock-sso` | Local SSO profile (boto3 + CLI auto-refresh). Leave unset in deployed/role mode. |
| `AWS_ACCESS_KEY_ID` | `AKIA...` | Static Bedrock credentials (or use the ECS task role instead). |
| `AWS_SECRET_ACCESS_KEY` | `...` | Static Bedrock credentials. |
| `AWS_SESSION_TOKEN` | `...` | Optional, for temporary STS credentials. |
| `AWS_DEFAULT_REGION` | `eu-west-2` | Bedrock region. |

## Jira Integration

| Variable | Example | Description |
|---|---|---|
| `JIRA_CLIENT_ID` | `ezAx...` | Atlassian OAuth 2.0 client ID. |
| `JIRA_CLIENT_SECRET` | `ATOA...` | Atlassian OAuth 2.0 client secret. |

## ECS Task Definition Snippet

```json
{
  "containerDefinitions": [
    {
      "name": "conductor-backend",
      "image": "conductor/backend:latest",
      "portMappings": [{ "containerPort": 8000 }],
      "environment": [
        { "name": "BACKEND_HOST", "value": "0.0.0.0" },
        { "name": "BACKEND_PORT", "value": "8000" }
      ],
      "secrets": [
        { "name": "DATABASE_URL", "valueFrom": "arn:aws:secretsmanager:eu-west-2:ACCOUNT:secret:conductor/database-url" },
        { "name": "REDIS_URL", "valueFrom": "arn:aws:secretsmanager:eu-west-2:ACCOUNT:secret:conductor/redis-url" }
      ]
    }
  ]
}
```

## AWS Secrets Manager Keys

Create these secrets in Secrets Manager (plain text, not JSON):

| Secret Name | Value |
|---|---|
| `conductor/database-url` | `postgresql+asyncpg://conductor:PASSWORD@my-rds:5432/conductor` |
| `conductor/redis-url` | `redis://:PASSWORD@my-elasticache:6379/0` |

## Notes

- ECS task role should have Bedrock access (`bedrock:InvokeModel`) — no need for AWS key env vars (deployed mode resolves the role via the default credential chain).
- `DATABASE_URL` and `REDIS_URL` always take highest priority, overriding anything in YAML config.
- Observability is via task-hierarchy telemetry (the `task` table in the same Postgres).
