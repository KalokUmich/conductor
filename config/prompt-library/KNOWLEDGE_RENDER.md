# Render Platform — Knowledge Base

Source: `/home/kalok/render` | Last updated: 2026-03-21

## Identity

Enterprise lending platform (Python 3.12+ / FastAPI). Provides credit decisioning, underwriting, open banking, and multi-tenant customer journeys for 40+ clients.

## Architecture

| Service | Purpose | Key Files |
|---------|---------|-----------|
| **Dashboard API** | Underwriting staff UI backend | `dashboard_api/api.py` |
| **External API** | Public APIs: `/render/v1`, `/fintern/v1`, `/ob/v1`, portal, BNPL | `external_api/api.py` |
| **Background Worker** | Async job processor (Procrastinate queue) | `background_worker/` |
| **Common Services** | ~55 shared domain services | `common/services/` |

## Naming Conventions

- Classes: PascalCase (`ApplicationDb`, `IApplicationService`)
- Enums: `*Enum` suffix (`ApplicationStatusEnum`, `CreditDecisionOutcome`)
- Interfaces: `I*Service` prefix (`IApplicationService`)
- Database: snake_case with temporal versioning (`valid_from`, `valid_to`)
- Modules: `services/` for implementations, `models.py` for data classes, `routes.py` for endpoints

## Core Domain Models

- **Applicant**: personal, address, financial, credit, OB consent data
- **Application**: amount, terms, decision, quotes, facilities, documents
- **ApplicationDecision**: type, reasons, timestamp, override tracking
- **BankAccount**: transactions, classifications, balances
- **LedgerFacility**: Abound ledger integration, repayment tracking
- **FeatureResult**: ML feature outputs and scoring

## Key Business Flow

```
Create Applicant → Submit Application → OB Consent & Bank Data
→ Feature Evaluation (100+ ML features) → Affordability Check
→ Credit File Lookup (Equifax) → Decision Rules → Quote Generation
→ Quote Acceptance → Disbursal (via Abound) → Repayment Tracking
```

## Decision Outcomes

`CreditDecisionOutcome`: PENDING, ACCEPT, DECLINE, REFERAL, AWAITING, WITHDRAWN, APPEAL

## External Integrations

| System | Purpose |
|--------|---------|
| Equifax, Experian, TransUnion | Credit bureaus |
| Bud, Plaid, TrueLayer | Open Banking |
| Abound, Fintern | Ledger systems |
| Acquired, Citi | Payment processing |
| IDVerse, Equifax IDV | Identity/fraud |
| Zendesk, AWS SES | Communication |
| AWS Bedrock | AI/ML |

## Configuration

Hierarchical JSON: `defaults.json` → `{CLIENT_ID}.json` → `{CLIENT_ID}-{ENVIRONMENT}.json` → env vars.
40+ client configs in `/backend/config/*.json`.

## Database

PostgreSQL with temporal versioning, 100+ tables, Alembic migrations.

## Testing

pytest 8.4+ with async, polyfactory, pytest-xdist parallel, Cypress E2E.
