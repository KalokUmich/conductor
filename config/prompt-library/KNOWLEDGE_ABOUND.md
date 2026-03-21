# Abound Server — Knowledge Base

Source: `/home/kalok/abound-server` | Last updated: 2026-03-21

## Identity

Consumer lending platform (Java / Spring Boot / Maven). Multi-module monorepo: loan origination, admin CMS, shared common services.

## Architecture

| Module | Purpose | Key Packages |
|--------|---------|-------------|
| **loan/** | User-facing loan application | controller/ (26), service/impl/ (22) |
| **cms/** | Admin operations, callbacks, scheduled tasks | controller/ (11), service/ (16) |
| **common/** | Shared domain, services, integrations | domain/model/, service/impl/, repository/ |
| **abound-lambda/** | PDF generation via AWS Lambda | Handler.java |

## Naming Conventions — Golden Rules

### 1. `*DataRequest` classes = step completion checklists
Each multi-step flow has a `*DataRequest` class with boolean + timestamp pairs and a composite `isFinished` gate:

| Class | Fields | isFinished Logic |
|-------|--------|-----------------|
| `PostApprovalDataRequest` | setPassword, setPhone, commissionConsent, confirmationPayee, setCpa, signature, idv | ALL 7 = true |
| `AboutYouDataRequest` | loanPurposeConfirm, rentConfirm, addressUpdate, futureIncomeChange, nationalityConfirm, debtArrangementConfirm + emailVerification(auto-true) | ALL 7 = true |
| `YourLoanDataRequest` | loanValueConfirm | loanValueConfirm = true |
| `SubmissionDataRequest` | submitConfirm | submitConfirm = true |

**Pattern**: `boolean field` + `LocalDateTime fieldTime` + `synchronized updateIsFinished()`

### 2. Class name = search keyword
- `PostApprovalDataRequest` → search "PostApproval"
- `RenderCallBackServiceImpl` → search "RenderCallBack"
- `CreditDecisionOutcome` → search "CreditDecision"

### 3. `*ServiceImpl` = actual business logic
- `RenderCallBackServiceImpl` (1689 lines) — all Render callback handling
- `ApproveServiceImpl` — IDV approval → disbursement
- `PaymentServiceImpl` — payment callback handling
- `OnePageApplicationServiceImpl` — single-page flow
- `LedgerCommonServiceImpl` — ledger integration

### 4. `*CallBackService` = async flow entry points
Callbacks from external systems (Render, NatWest, Acquired) arrive at `*CallBackService`, not at Controllers.

### 5. Enums define state machines (42 total)
Key enums in `domain/model/enums/`:
- `CreditDecisionOutcome`: PENDING, ACCEPT, DECLINE, REFERAL, AWAITING, WITHDRAWN, APPEAL
- `LoanActionStatus`: ALLOW, DENY, HIDE
- `AboundJourneyVariant`: ORIGINAL, ONE_PAGE, NO_REG_JOURNEY
- `EmailType`, `TopicType`, `DocumentTypeEnum`

## Loan Application Lifecycle

```
welcome → email_conf → rent_info → open_banking → pre_approval
→ income_verification → upload_photos/videos → contract_submitted
→ [Render credit decision]
→ ACCEPT: approval email + doc generation + voice message
  → PostApproval: setPassword → setPhone → commissionConsent
    → confirmationPayee → setCpa → signature → idv
  → [All 7 complete: isFinished = true]
  → Admin IDV review → createNewLoan (ledger) → disburse
→ DECLINE: decline email with reason
→ WITHDRAWN: anonymize + withdrawal email
```

## Post-Approval → Disbursement Flow

```
PostApprovalDataRequest.isFinished = true
→ Admin approves IDV (ApproveServiceImpl.approveIdv)
  → ledgerCommonService.createNewLoan()
  → handleApproveAndPayout()
    → Finance Portal: notify broker, no auto-payout
    → Debt Consolidation: Clearer/Bettercents API
    → Standard: direct payout to user account
  → renderWebClientService.disburseApplication()
```

## Document Generation (3 types via AWS Lambda)

| Type | ID | Document |
|------|-----|----------|
| PCCI | 6 | Pre-Contract Credit Information / Key Facts |
| Credit Agreement | 7 | Unsigned loan agreement |
| Adequate Explanations | 8 | AE document |

Polling: 100s timeout, 10 attempts, 10s interval. Sent as email attachments.

## Callback Architecture

| Callback Type | Handler Method | Trigger |
|--------------|----------------|---------|
| TYPE_DECISION | registrationObCallback / borrowMoreRenderDecision | Render credit decision |
| TYPE_APPEALED_DECISION | processAppeal / processBMAppeal | Customer appeals decline |
| TYPE_UPDATE_ADDRESS_HISTORY | setAddress | Render address update |
| TYPE_UPDATE_MAX_PAYMENT_AMOUNT | changeLoan / changeLoanBM | Counter-offer |

## Email System

70+ email types. Selection: `EmailTemplate.find(declineReason, owner, aggregator)`
Brands: ABOUND, CREDISPHERE, PHOENIX, VENDIGO.
Key types: CONDITION_APPROVAL, DISBURSAL_APPROVE, BM_APPROVED, PORTAL_FINANCE_IDV_APPROVAL.

## Database (MySQL / MyBatis-Plus)

| Table | Entity | Purpose |
|-------|--------|---------|
| b_user | User | User personal data |
| b_user_apply | UserApplicationMapping | Application tracking (approvalStatus, preDispersalStatus) |
| b_reg_stage_query | RegStageQuery | Journey stages (22 boolean+timestamp fields) |
| b_reg_one_page_flow | RegOnePageFlow | One-page journey state (JSON blobs) |
| b_loan | Loan | Loan records + PDF links |
| b_admin_audit | AdminAudit | Render decision logs |
| b_idv_review | IdvReview | Admin IDV approvals |

## External Integrations

| System | Service | Purpose |
|--------|---------|---------|
| **Render** | RenderCallBackServiceImpl, RenderServiceImpl | Credit decisions, IDV, callbacks |
| **Ledger** | LedgerCommonServiceImpl | Loan creation, drawdown, APR calc |
| **NatWest** | NatWestService, ClearerRestService | Open banking, COP, payments |
| **Acquired** | PaymentServiceImpl | Payment processing callbacks |
| **AWS Lambda** | FileMarkerService → ConvertAPI | PDF generation |

## Package Structure

```
com.abound.common/
├── domain/model/request/    → 108 Request classes (*DataRequest = step checklists)
├── domain/model/response/   → 74 Response classes
├── domain/model/internal/   → Record classes (StageRecord, etc.)
├── domain/model/enums/      → 42 enum classes
├── service/                 → 23 interfaces
├── service/impl/            → implementations
├── repository/entity/       → 70 entity classes
├── repository/dao/          → 60 DAO interfaces (MyBatis-Plus)
├── external/                → integration services
└── constants/               → EmailTemplate, etc.
```
