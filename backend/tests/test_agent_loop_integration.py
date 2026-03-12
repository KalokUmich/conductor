"""Integration tests for the agent loop with a REAL LLM provider.

These tests create a realistic mini-repository (with 40+ noise files) and run
the full agent loop against real Bedrock-hosted models to validate that:
  1. High-level architectural questions are answered correctly despite noise.
  2. The agent finds the right orchestration files and doesn't get lost.
  3. The answer includes ALL steps/stages defined in the code.
  4. Quality parity with Claude Opus 4.6 (multi-model comparison).

Requires:
  - Valid AWS Bedrock credentials in config/conductor.secrets.yaml
  - Network access to AWS Bedrock

Run with:
    make integration-test                          # via Makefile
    pytest tests/test_agent_loop_integration.py -v -s -m integration --timeout=180
"""
from __future__ import annotations

import os
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import pytest
import yaml

from app.ai_provider.claude_bedrock import ClaudeBedrockProvider
from app.agent_loop.service import AgentLoopService, AgentResult, _is_high_level_query
from app.code_tools.tools import invalidate_graph_cache


# ---------------------------------------------------------------------------
# Helpers & multi-model support
# ---------------------------------------------------------------------------

# Known Bedrock cross-region inference profile IDs (eu-west-2)
MODEL_SONNET = "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"
MODEL_OPUS = "eu.anthropic.claude-opus-4-6-v1"

# Which models to compare in multi-model tests (override via env)
_COMPARE_MODELS: Dict[str, str] = {
    "sonnet": MODEL_SONNET,
    "opus": MODEL_OPUS,
}


def _load_bedrock_credentials() -> Optional[dict]:
    """Load Bedrock credentials from conductor.secrets.yaml."""
    for base in [
        Path(__file__).resolve().parent.parent.parent / "config",
        Path.cwd() / "config",
        Path.cwd().parent / "config",
    ]:
        secrets_path = base / "conductor.secrets.yaml"
        if secrets_path.exists():
            with open(secrets_path) as f:
                data = yaml.safe_load(f) or {}
            bedrock = data.get("ai_providers", {}).get("aws_bedrock", {})
            if bedrock.get("access_key_id") and bedrock.get("secret_access_key"):
                return bedrock
    return None


def _make_provider(model_id: str = MODEL_SONNET) -> Optional[ClaudeBedrockProvider]:
    """Create a real Bedrock provider from secrets, or None if unavailable."""
    creds = _load_bedrock_credentials()
    if not creds:
        return None
    return ClaudeBedrockProvider(
        aws_access_key_id=creds["access_key_id"],
        aws_secret_access_key=creds["secret_access_key"],
        aws_session_token=creds.get("session_token"),
        region_name=creds.get("region", "us-east-1"),
        model_id=model_id,
    )


# Check credentials availability (used by integration tests below)
_provider = _make_provider()
_skip_no_creds = pytest.mark.skipif(
    _provider is None,
    reason="No Bedrock credentials in conductor.secrets.yaml",
)


@dataclass
class ModelRunResult:
    """Stores the result of a single model run for comparison."""
    model_name: str
    model_id: str
    answer: str = ""
    iterations: int = 0
    tool_calls: int = 0
    duration_ms: float = 0.0
    error: Optional[str] = None
    passed_checks: List[str] = field(default_factory=list)
    failed_checks: List[str] = field(default_factory=list)


def _print_comparison(results: List[ModelRunResult]) -> None:
    """Print a side-by-side comparison table of model results."""
    print(f"\n{'=' * 80}")
    print("MULTI-MODEL COMPARISON")
    print(f"{'=' * 80}")
    for r in results:
        status = "✅ PASS" if not r.failed_checks else "❌ FAIL"
        print(f"\n--- {r.model_name} ({r.model_id}) --- {status}")
        print(f"    Iterations: {r.iterations}  |  Tool calls: {r.tool_calls}  |  "
              f"Duration: {r.duration_ms:.0f}ms")
        if r.failed_checks:
            print(f"    ❌ Failed: {', '.join(r.failed_checks)}")
        if r.passed_checks:
            print(f"    ✅ Passed: {', '.join(r.passed_checks)}")
        if r.error:
            print(f"    ⚠ Error: {r.error}")
        print(f"    Answer (first 500 chars):\n      {r.answer[:500]}")
    print(f"\n{'=' * 80}\n")


# ---------------------------------------------------------------------------
# Mini-repo builder: Loan Application Journey (with 40+ noise files)
# ---------------------------------------------------------------------------


def _write_file(base: Path, rel_path: str, content: str) -> None:
    """Helper to write a file, creating parent directories as needed."""
    p = base / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content))


def _build_noise_files(root: Path) -> None:
    """Add 40+ noise/decoy files to make the repo realistic and hard to navigate."""

    # ---- Config & CI noise ----
    _write_file(root, "Makefile", """\
        .PHONY: build test lint deploy
        build:
        \t@echo "Building..."
        test:
        \tpytest tests/ -v
        lint:
        \truff check .
        deploy:
        \t./scripts/deploy.sh
    """)
    _write_file(root, "Dockerfile", """\
        FROM python:3.12-slim
        WORKDIR /app
        COPY requirements.txt .
        RUN pip install -r requirements.txt
        COPY . .
        CMD ["uvicorn", "main:app", "--host", "0.0.0.0"]
    """)
    _write_file(root, "docker-compose.yml", """\
        version: "3.8"
        services:
          app:
            build: .
            ports: ["8000:8000"]
            depends_on: [db, redis]
          db:
            image: postgres:15
            environment:
              POSTGRES_DB: loanapp
          redis:
            image: redis:7
    """)
    _write_file(root, "requirements.txt", """\
        fastapi==0.104.1
        uvicorn==0.24.0
        sqlalchemy==2.0.23
        pydantic==2.5.2
        redis==5.0.1
        celery==5.3.6
        boto3==1.29.4
        httpx==0.25.2
    """)
    _write_file(root, ".github/workflows/ci.yml", """\
        name: CI
        on: [push, pull_request]
        jobs:
          test:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
              - run: pip install -r requirements.txt
              - run: pytest tests/ -v
    """)
    _write_file(root, "config/__init__.py", "")
    _write_file(root, "config/settings.py", """\
        \"\"\"Application settings loaded from environment.\"\"\"
        import os

        DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://localhost/loanapp")
        REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
        SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key")
        KYC_API_URL = os.getenv("KYC_API_URL", "https://kyc.example.com/v1")
        SCORING_API_URL = os.getenv("SCORING_API_URL", "https://scoring.example.com/v2")
        MAX_LOAN_AMOUNT = 500000
        MIN_CREDIT_SCORE = 500
        REVIEW_TIMEOUT_HOURS = 48
    """)
    _write_file(root, "config/database.py", """\
        \"\"\"Database connection and session management.\"\"\"
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from config.settings import DATABASE_URL

        engine = create_engine(DATABASE_URL, pool_size=20)
        SessionLocal = sessionmaker(bind=engine)

        def get_db():
            db = SessionLocal()
            try:
                yield db
            finally:
                db.close()
    """)

    # ---- DB models (decoys — mention "steps" and "status" a lot) ----
    _write_file(root, "models/__init__.py", "")
    _write_file(root, "models/application.py", """\
        \"\"\"Loan application database model.\"\"\"
        from sqlalchemy import Column, Integer, String, DateTime, Enum
        from sqlalchemy.orm import declarative_base

        Base = declarative_base()

        class Application(Base):
            __tablename__ = "applications"
            id = Column(Integer, primary_key=True)
            applicant_id = Column(String, nullable=False)
            amount = Column(Integer, nullable=False)
            status = Column(String, default="pending")  # pending, in_progress, approved, rejected
            current_step = Column(String, default="submitted")  # DECOY: not the journey steps
            created_at = Column(DateTime)
            updated_at = Column(DateTime)
    """)
    _write_file(root, "models/user.py", """\
        \"\"\"User and applicant models.\"\"\"
        from sqlalchemy import Column, Integer, String, Boolean
        from models.application import Base

        class User(Base):
            __tablename__ = "users"
            id = Column(Integer, primary_key=True)
            email = Column(String, unique=True)
            hashed_password = Column(String)
            is_active = Column(Boolean, default=True)

        class Applicant(Base):
            __tablename__ = "applicants"
            id = Column(Integer, primary_key=True)
            user_id = Column(Integer)
            first_name = Column(String)
            last_name = Column(String)
            ssn_hash = Column(String)
            credit_score = Column(Integer)
    """)
    _write_file(root, "models/document.py", """\
        \"\"\"Document metadata model.\"\"\"
        from sqlalchemy import Column, Integer, String, DateTime
        from models.application import Base

        class Document(Base):
            __tablename__ = "documents"
            id = Column(Integer, primary_key=True)
            application_id = Column(Integer)
            doc_type = Column(String)  # id_proof, income_proof, address_proof
            file_path = Column(String)
            status = Column(String, default="pending")  # pending, verified, rejected
            uploaded_at = Column(DateTime)
    """)

    # ---- Auth service (unrelated) ----
    _write_file(root, "services/auth/__init__.py", "")
    _write_file(root, "services/auth/service.py", """\
        \"\"\"Authentication and authorization service.\"\"\"
        import hashlib
        from models.user import User

        class AuthService:
            def authenticate(self, email: str, password: str) -> User:
                \"\"\"Authenticate user by email and password.\"\"\"
                pass

            def create_token(self, user: User) -> str:
                \"\"\"Create a JWT token for the user session.\"\"\"
                pass

            def verify_token(self, token: str) -> dict:
                \"\"\"Verify and decode a JWT token.\"\"\"
                pass
    """)
    _write_file(root, "services/auth/middleware.py", """\
        \"\"\"Authentication middleware for FastAPI.\"\"\"

        class AuthMiddleware:
            async def __call__(self, request, call_next):
                token = request.headers.get("Authorization", "").replace("Bearer ", "")
                if not token:
                    return {"error": "Unauthorized"}
                return await call_next(request)
    """)

    # ---- Cache service (unrelated) ----
    _write_file(root, "services/cache/__init__.py", "")
    _write_file(root, "services/cache/redis_client.py", """\
        \"\"\"Redis cache client for application data.\"\"\"
        import redis
        from config.settings import REDIS_URL

        class CacheService:
            def __init__(self):
                self.client = redis.from_url(REDIS_URL)

            def get(self, key: str) -> str:
                return self.client.get(key)

            def set(self, key: str, value: str, ttl: int = 3600):
                self.client.setex(key, ttl, value)

            def invalidate_application(self, app_id: str):
                \"\"\"Invalidate all cached data for an application.\"\"\"
                self.client.delete(f"app:{app_id}")
                self.client.delete(f"app:{app_id}:status")
                self.client.delete(f"app:{app_id}:score")
    """)

    # ---- Notifications service (uses word "flow" — decoy!) ----
    _write_file(root, "services/notifications/__init__.py", "")
    _write_file(root, "services/notifications/flow.py", """\
        \"\"\"Notification flow engine.

        This manages the NOTIFICATION flow — which notifications to send and when.
        NOT related to the loan application journey.
        \"\"\"

        NOTIFICATION_FLOW_STEPS = [
            "welcome_email",
            "application_received",
            "status_update",
            "approval_notification",
            "rejection_notification",
            "disbursement_confirmation",
        ]

        class NotificationFlowEngine:
            \"\"\"Manages the sequence of notifications for each application event.\"\"\"

            def trigger(self, event_type: str, application_id: str):
                \"\"\"Trigger the appropriate notification flow for an event.\"\"\"
                if event_type == "submitted":
                    self._send_email("welcome_email", application_id)
                    self._send_email("application_received", application_id)
                elif event_type == "approved":
                    self._send_email("approval_notification", application_id)

            def _send_email(self, template: str, app_id: str):
                pass
    """)
    _write_file(root, "services/notifications/templates.py", """\
        \"\"\"Email templates for notifications.\"\"\"

        TEMPLATES = {
            "welcome_email": "Welcome to LoanApp! Your application has been received.",
            "application_received": "We have received your loan application #{app_id}.",
            "status_update": "Your application status has been updated to: {status}.",
            "approval_notification": "Congratulations! Your loan has been approved.",
            "rejection_notification": "We regret to inform you that your application was not approved.",
            "disbursement_confirmation": "Funds have been transferred. Transaction ID: {tx_id}.",
        }
    """)

    # ---- Reporting service (unrelated) ----
    _write_file(root, "services/reporting/__init__.py", "")
    _write_file(root, "services/reporting/dashboard.py", """\
        \"\"\"Dashboard reporting service for management.\"\"\"

        class DashboardService:
            def get_application_stats(self) -> dict:
                \"\"\"Get aggregate stats: total applications, approval rate, avg processing time.\"\"\"
                return {"total": 0, "approved": 0, "rejected": 0, "avg_days": 0}

            def get_step_performance(self) -> dict:
                \"\"\"Performance metrics per journey step — avg time, failure rate.\"\"\"
                # NOTE: This queries step metrics, but does NOT define the steps themselves.
                return {}
    """)

    # ---- Audit service (unrelated) ----
    _write_file(root, "services/audit/__init__.py", "")
    _write_file(root, "services/audit/logger.py", """\
        \"\"\"Audit logging for compliance.\"\"\"
        import json
        from datetime import datetime

        class AuditLogger:
            def log_event(self, event_type: str, application_id: str, details: dict):
                \"\"\"Log an audit event for regulatory compliance.\"\"\"
                entry = {
                    "timestamp": datetime.utcnow().isoformat(),
                    "event": event_type,
                    "application_id": application_id,
                    "details": details,
                }
                # In production: write to audit DB / S3
                print(json.dumps(entry))
    """)

    # ---- LEGACY: deprecated old journey (BIG DECOY) ----
    _write_file(root, "services/legacy/__init__.py", "")
    _write_file(root, "services/legacy/journey_v1.py", """\
        \"\"\"DEPRECATED: Old 3-step journey from v1.0.

        This was the original loan journey before the v2.0 rewrite.
        DO NOT USE — kept for reference only. See services/journey/ for current.
        \"\"\"

        # DEPRECATED STEPS (v1.0 — only 3 steps):
        LEGACY_JOURNEY_STEPS = [
            "basic_check",       # Combined KYC + doc check (replaced by steps 1+2)
            "auto_scoring",      # Automated scoring only (no manual review)
            "disbursement",      # Same as current step 5
        ]

        class OldJourneyRunner:
            \"\"\"DEPRECATED: v1.0 journey runner.\"\"\"
            def run(self, application_id: str):
                raise NotImplementedError("Use LoanJourneyOrchestrator instead")
    """)
    _write_file(root, "services/legacy/old_workflow.py", """\
        \"\"\"DEPRECATED: Old workflow utilities from the initial prototype.\"\"\"

        def execute_workflow(steps: list, context: dict) -> dict:
            \"\"\"Generic workflow executor — DEPRECATED, do not use.\"\"\"
            results = {}
            for step in steps:
                results[step] = {"status": "completed"}
            return results
    """)

    # ---- DECOY: utils/steps.py (mentions "steps" but is about config steps) ----
    _write_file(root, "utils/__init__.py", "")
    _write_file(root, "utils/steps.py", """\
        \"\"\"Configuration deployment steps utility.

        This module handles the DEPLOYMENT steps for configuration updates,
        NOT the loan application journey steps.
        \"\"\"

        DEPLOYMENT_STEPS = [
            "validate_config",
            "backup_current",
            "apply_changes",
            "run_smoke_tests",
            "rollback_if_failed",
        ]

        def run_deployment_steps(config: dict) -> bool:
            \"\"\"Execute configuration deployment steps in order.\"\"\"
            for step in DEPLOYMENT_STEPS:
                print(f"Running deployment step: {step}")
            return True
    """)
    _write_file(root, "utils/helpers.py", """\
        \"\"\"Miscellaneous utility functions.\"\"\"
        import hashlib
        import uuid

        def generate_id() -> str:
            return str(uuid.uuid4())

        def hash_ssn(ssn: str) -> str:
            return hashlib.sha256(ssn.encode()).hexdigest()

        def format_currency(amount: int) -> str:
            return f"${amount:,.2f}"

        def validate_email(email: str) -> bool:
            return "@" in email and "." in email.split("@")[1]
    """)
    _write_file(root, "utils/validators.py", """\
        \"\"\"Input validation utilities.\"\"\"

        def validate_loan_amount(amount: int) -> bool:
            return 1000 <= amount <= 500000

        def validate_application_data(data: dict) -> list:
            errors = []
            if not data.get("applicant_name"):
                errors.append("applicant_name is required")
            if not data.get("amount"):
                errors.append("amount is required")
            if data.get("amount") and not validate_loan_amount(data["amount"]):
                errors.append("amount must be between 1,000 and 500,000")
            return errors
    """)

    # ---- Deep nesting: infrastructure ----
    _write_file(root, "infrastructure/__init__.py", "")
    _write_file(root, "infrastructure/db/__init__.py", "")
    _write_file(root, "infrastructure/db/migrations/001_create_tables.py", """\
        \"\"\"Migration 001: Create initial tables.\"\"\"

        def upgrade(conn):
            conn.execute(\"\"\"
                CREATE TABLE applications (
                    id SERIAL PRIMARY KEY,
                    applicant_id VARCHAR(255) NOT NULL,
                    amount INTEGER NOT NULL,
                    status VARCHAR(50) DEFAULT 'pending',
                    current_step VARCHAR(100) DEFAULT 'submitted',
                    created_at TIMESTAMP DEFAULT NOW()
                );
            \"\"\")

        def downgrade(conn):
            conn.execute("DROP TABLE IF EXISTS applications;")
    """)
    _write_file(root, "infrastructure/db/migrations/002_add_documents.py", """\
        \"\"\"Migration 002: Add documents table.\"\"\"

        def upgrade(conn):
            conn.execute(\"\"\"
                CREATE TABLE documents (
                    id SERIAL PRIMARY KEY,
                    application_id INTEGER REFERENCES applications(id),
                    doc_type VARCHAR(50),
                    file_path VARCHAR(500),
                    status VARCHAR(50) DEFAULT 'pending'
                );
            \"\"\")

        def downgrade(conn):
            conn.execute("DROP TABLE IF EXISTS documents;")
    """)
    _write_file(root, "infrastructure/messaging/__init__.py", "")
    _write_file(root, "infrastructure/messaging/events.py", """\
        \"\"\"Event definitions for the message bus.\"\"\"

        class ApplicationEvent:
            def __init__(self, event_type: str, application_id: str, data: dict = None):
                self.event_type = event_type
                self.application_id = application_id
                self.data = data or {}

        # Event types
        APPLICATION_SUBMITTED = "application.submitted"
        KYC_COMPLETED = "kyc.completed"
        DOCUMENTS_VERIFIED = "documents.verified"
        SCORING_COMPLETED = "scoring.completed"
        REVIEW_COMPLETED = "review.completed"
        LOAN_DISBURSED = "loan.disbursed"
    """)
    _write_file(root, "infrastructure/messaging/broker.py", """\
        \"\"\"Message broker for async event processing.\"\"\"

        class MessageBroker:
            def __init__(self):
                self._handlers = {}

            def subscribe(self, event_type: str, handler):
                self._handlers.setdefault(event_type, []).append(handler)

            def publish(self, event):
                for handler in self._handlers.get(event.event_type, []):
                    handler(event)
    """)

    # ---- API routes (decoy entry points) ----
    _write_file(root, "api/__init__.py", "")
    _write_file(root, "api/routes.py", """\
        \"\"\"FastAPI route definitions.\"\"\"

        from fastapi import APIRouter, Depends

        router = APIRouter()

        @router.post("/applications")
        async def create_application(data: dict):
            \"\"\"Create a new loan application.\"\"\"
            pass

        @router.get("/applications/{app_id}")
        async def get_application(app_id: str):
            \"\"\"Get application details and current status.\"\"\"
            pass

        @router.post("/applications/{app_id}/start")
        async def start_journey(app_id: str):
            \"\"\"Start the loan application journey processing.\"\"\"
            # Delegates to the journey orchestrator
            pass

        @router.get("/applications/{app_id}/status")
        async def get_journey_status(app_id: str):
            \"\"\"Get current journey progress.\"\"\"
            pass
    """)
    _write_file(root, "api/admin_routes.py", """\
        \"\"\"Admin API routes.\"\"\"
        from fastapi import APIRouter

        admin_router = APIRouter(prefix="/admin")

        @admin_router.get("/dashboard")
        async def dashboard():
            return {"status": "ok"}

        @admin_router.get("/applications/pending-review")
        async def pending_reviews():
            return []
    """)
    _write_file(root, "main.py", """\
        \"\"\"Application entry point.\"\"\"
        from fastapi import FastAPI
        from api.routes import router

        app = FastAPI(title="Loan Application System")
        app.include_router(router, prefix="/api")
    """)

    # ---- Tests (realistic test files) ----
    _write_file(root, "tests/__init__.py", "")
    _write_file(root, "tests/test_auth.py", """\
        \"\"\"Tests for authentication service.\"\"\"
        import pytest
        from services.auth.service import AuthService

        class TestAuthService:
            def test_authenticate_valid(self):
                pass
            def test_authenticate_invalid_password(self):
                pass
            def test_create_token(self):
                pass
            def test_verify_token(self):
                pass
    """)
    _write_file(root, "tests/test_scoring.py", """\
        \"\"\"Tests for credit scoring engine.\"\"\"
        import pytest

        class TestCreditScoring:
            def test_score_high_income(self):
                pass
            def test_score_low_income(self):
                pass
            def test_score_no_history(self):
                pass
    """)
    _write_file(root, "tests/test_journey.py", """\
        \"\"\"Tests for the loan journey orchestrator.\"\"\"
        import pytest

        class TestLoanJourney:
            \"\"\"Integration tests for the complete loan journey.\"\"\"
            def test_happy_path(self):
                \"\"\"Test that all 5 steps execute in order.\"\"\"
                pass
            def test_kyc_rejection(self):
                pass
            def test_low_credit_score_rejection(self):
                pass
            def test_manual_review_rejection(self):
                pass
    """)
    _write_file(root, "tests/conftest.py", """\
        \"\"\"Shared test fixtures.\"\"\"
        import pytest

        @pytest.fixture
        def sample_application():
            return {
                "applicant_name": "John Doe",
                "amount": 50000,
                "email": "john@example.com",
            }
    """)

    # ---- Marketing doc (mentions "customer journey" but is not code!) ----
    _write_file(root, "docs/customer_journey.md", """\
        # Customer Journey Map

        This document describes the CUSTOMER EXPERIENCE journey — the steps
        from the user's perspective (NOT the technical implementation).

        1. **Landing Page** — user visits the website
        2. **Sign Up** — user creates an account
        3. **Application Form** — user fills out the loan application
        4. **Waiting Period** — user waits for processing
        5. **Result Notification** — user receives approval/rejection
        6. **Fund Receipt** — user receives funds (if approved)

        > Note: For the technical implementation steps, see the codebase
        > under `services/journey/`.
    """)
    _write_file(root, "docs/api_reference.md", """\
        # API Reference

        ## POST /api/applications
        Create a new loan application.

        ## GET /api/applications/{id}
        Get application details.

        ## POST /api/applications/{id}/start
        Start processing the loan application through the journey pipeline.
    """)

    # ---- Scripts (noise) ----
    _write_file(root, "scripts/deploy.sh", """\
        #!/bin/bash
        echo "Deploying application..."
        docker-compose up -d
    """)
    _write_file(root, "scripts/seed_data.py", """\
        \"\"\"Seed the database with test data.\"\"\"

        def seed():
            applications = [
                {"applicant_id": "user-1", "amount": 50000},
                {"applicant_id": "user-2", "amount": 100000},
            ]
            for app in applications:
                print(f"Seeding application: {app}")
    """)


def _build_core_journey_files(root: Path) -> None:
    """Build the actual journey orchestration files (the signal in the noise)."""

    # README — intentionally brief, does NOT list all steps explicitly
    _write_file(root, "README.md", """\
        # Loan Application System

        A microservices-based loan origination platform.

        ## Quick Start
        ```
        docker-compose up -d
        curl http://localhost:8000/api/applications -X POST -d '{"amount": 50000}'
        ```

        ## Architecture
        The system is organized into several service modules under `services/`.
        Each service handles a specific domain concern. The main application
        entry point is `main.py` which registers API routes.

        For deployment, see `scripts/deploy.sh` and the CI pipeline in `.github/`.
    """)

    # Journey orchestrator — THE key file (buried among 8+ service dirs)
    journey_dir = root / "services" / "journey"
    journey_dir.mkdir(parents=True, exist_ok=True)
    _write_file(root, "services/journey/__init__.py", "")
    _write_file(root, "services/journey/orchestrator.py", """\
        \"\"\"Loan application journey orchestrator.

        Defines the complete sequence of steps a loan application goes through.
        \"\"\"
        from services.kyc.verifier import verify_identity
        from services.scoring.engine import calculate_credit_score
        from services.documents.collector import collect_documents
        from services.journey.review import manual_review
        from services.disbursement.processor import disburse_funds


        JOURNEY_STEPS = [
            "identity_verification",
            "document_collection",
            "credit_scoring",
            "manual_review",
            "fund_disbursement",
        ]


        class LoanJourneyOrchestrator:
            \"\"\"Orchestrates the 5-step loan application journey.\"\"\"

            def __init__(self, application_id: str):
                self.application_id = application_id
                self.current_step = 0
                self.completed_steps = []

            async def run_journey(self) -> dict:
                \"\"\"Execute all journey steps in sequence.\"\"\"
                result = {"application_id": self.application_id, "steps": []}

                # Step 1: Identity Verification (KYC)
                kyc_result = await self.step_identity_verification()
                result["steps"].append(kyc_result)
                if not kyc_result["passed"]:
                    result["status"] = "rejected_kyc"
                    return result

                # Step 2: Document Collection
                doc_result = await self.step_document_collection()
                result["steps"].append(doc_result)

                # Step 3: Credit Scoring
                score_result = await self.step_credit_scoring()
                result["steps"].append(score_result)
                if score_result["score"] < 500:
                    result["status"] = "rejected_score"
                    return result

                # Step 4: Manual Review
                review_result = await self.step_manual_review()
                result["steps"].append(review_result)
                if not review_result["approved"]:
                    result["status"] = "rejected_review"
                    return result

                # Step 5: Fund Disbursement
                disbursement_result = await self.step_fund_disbursement()
                result["steps"].append(disbursement_result)
                result["status"] = "completed"
                return result

            async def step_identity_verification(self) -> dict:
                \"\"\"Step 1: Verify applicant identity via KYC provider.\"\"\"
                r = verify_identity(self.application_id)
                self.completed_steps.append("identity_verification")
                return {"step": "identity_verification", "passed": r}

            async def step_document_collection(self) -> dict:
                \"\"\"Step 2: Collect and validate required documents.\"\"\"
                docs = collect_documents(self.application_id)
                self.completed_steps.append("document_collection")
                return {"step": "document_collection", "documents": docs}

            async def step_credit_scoring(self) -> dict:
                \"\"\"Step 3: Calculate credit score.\"\"\"
                score = calculate_credit_score(self.application_id)
                self.completed_steps.append("credit_scoring")
                return {"step": "credit_scoring", "score": score}

            async def step_manual_review(self) -> dict:
                \"\"\"Step 4: Human reviewer evaluates the application.\"\"\"
                approved = manual_review(self.application_id)
                self.completed_steps.append("manual_review")
                return {"step": "manual_review", "approved": approved}

            async def step_fund_disbursement(self) -> dict:
                \"\"\"Step 5: Disburse approved loan funds.\"\"\"
                tx = disburse_funds(self.application_id)
                self.completed_steps.append("fund_disbursement")
                return {"step": "fund_disbursement", "transaction": tx}
    """)

    # Review sub-module (in journey/)
    _write_file(root, "services/journey/review.py", """\
        \"\"\"Manual review step for loan applications.\"\"\"

        def manual_review(application_id: str) -> bool:
            \"\"\"Send application to human reviewer queue. Returns approval decision.\"\"\"
            return True
    """)

    # KYC service
    _write_file(root, "services/kyc/__init__.py", "")
    _write_file(root, "services/kyc/verifier.py", """\
        \"\"\"KYC identity verification service.\"\"\"

        def verify_identity(application_id: str) -> bool:
            \"\"\"Verify applicant identity against government databases.\"\"\"
            return True
    """)

    # Scoring service
    _write_file(root, "services/scoring/__init__.py", "")
    _write_file(root, "services/scoring/engine.py", """\
        \"\"\"Credit scoring engine.\"\"\"

        def calculate_credit_score(application_id: str) -> int:
            \"\"\"Calculate credit score from financial history. Range: 300-850.\"\"\"
            return 720
    """)

    # Documents service
    _write_file(root, "services/documents/__init__.py", "")
    _write_file(root, "services/documents/collector.py", """\
        \"\"\"Document collection and validation.\"\"\"

        def collect_documents(application_id: str) -> list:
            \"\"\"Collect required documents: ID, income proof, address proof.\"\"\"
            return ["id_document", "income_proof", "address_proof"]
    """)

    # Disbursement service
    _write_file(root, "services/disbursement/__init__.py", "")
    _write_file(root, "services/disbursement/processor.py", """\
        \"\"\"Fund disbursement processor.\"\"\"

        def disburse_funds(application_id: str) -> dict:
            \"\"\"Transfer approved loan amount to applicant's bank account.\"\"\"
            return {"transaction_id": "TX-12345", "status": "completed"}
    """)


@pytest.fixture()
def loan_journey_repo(tmp_path: Path) -> Path:
    """Create a realistic mini-repo with 40+ files (noise + signal)."""
    _build_noise_files(tmp_path)
    _build_core_journey_files(tmp_path)
    invalidate_graph_cache()
    return tmp_path


# ---------------------------------------------------------------------------
# Unit tests for query classification helper
# ---------------------------------------------------------------------------


class TestQueryClassification:
    """Test the _is_high_level_query heuristic."""

    def test_high_level_journey(self):
        assert _is_high_level_query("What are the steps in our journey?")

    def test_high_level_architecture(self):
        assert _is_high_level_query("Explain the architecture of this system")

    def test_high_level_flow(self):
        assert _is_high_level_query("Describe the flow of a loan application")

    def test_high_level_workflow(self):
        assert _is_high_level_query("What is the workflow for onboarding?")

    def test_code_level_function(self):
        assert not _is_high_level_query("What does the authenticate() function do?")

    def test_code_level_bug(self):
        assert not _is_high_level_query("Why is line 42 in auth.py throwing an error?")

    def test_code_level_specific(self):
        assert not _is_high_level_query("Show me the implementation of verify_identity")


# ---------------------------------------------------------------------------
# Shared assertion helpers
# ---------------------------------------------------------------------------

_STEP_CHECKS = [
    ("Step 1 (Identity Verification)", ["identity verification", "identity_verification", "kyc"]),
    ("Step 2 (Document Collection)", ["document collection", "document_collection", "collect_documents"]),
    ("Step 3 (Credit Scoring)", ["credit scoring", "credit_scoring", "credit score"]),
    ("Step 4 (Manual Review)", ["manual review", "manual_review", "human review"]),
    ("Step 5 (Fund Disbursement)", ["fund disbursement", "fund_disbursement", "disburse"]),
]


def _check_journey_answer(answer_lower: str) -> tuple[list[str], list[str]]:
    """Check that the answer mentions all 5 journey steps.

    Returns (passed, failed) lists of check names.
    """
    passed, failed = [], []
    for name, keywords in _STEP_CHECKS:
        if any(kw in answer_lower for kw in keywords):
            passed.append(name)
        else:
            failed.append(name)
    if "orchestrator" in answer_lower:
        passed.append("cites orchestrator")
    else:
        failed.append("cites orchestrator")
    return passed, failed


async def _run_agent(
    model_id: str,
    query: str,
    workspace_path: str,
    max_iterations: int = 15,
) -> ModelRunResult:
    """Run the agent loop with a specific model and return a ModelRunResult."""
    model_name = {v: k for k, v in _COMPARE_MODELS.items()}.get(model_id, model_id)
    provider = _make_provider(model_id=model_id)
    if provider is None:
        return ModelRunResult(model_name=model_name, model_id=model_id,
                              error="No credentials")

    agent = AgentLoopService(provider=provider, max_iterations=max_iterations)
    t0 = time.monotonic()
    result: AgentResult = await agent.run(query=query, workspace_path=workspace_path)
    elapsed = (time.monotonic() - t0) * 1000

    return ModelRunResult(
        model_name=model_name,
        model_id=model_id,
        answer=result.answer,
        iterations=result.iterations,
        tool_calls=result.tool_calls_made,
        duration_ms=elapsed,
        error=result.error,
    )


# ---------------------------------------------------------------------------
# Integration tests (require real LLM)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@_skip_no_creds
class TestHighLevelQueryIntegration:
    """Integration tests for high-level queries using a real LLM (Sonnet).

    These use the noisy 40+ file repo to validate the agent can find the
    correct orchestration layer despite many decoy files.
    """

    @pytest.mark.asyncio
    async def test_loan_journey_steps(self, loan_journey_repo: Path):
        """The agent should correctly identify ALL 5 steps despite 40+ noise files."""
        r = await _run_agent(
            MODEL_SONNET,
            "What are the steps in the loan application journey? List all steps in order.",
            str(loan_journey_repo),
        )
        answer = r.answer.lower()
        print(f"\n{'='*60}")
        print(f"[{r.model_name}] {r.iterations} iters, {r.tool_calls} calls, {r.duration_ms:.0f}ms")
        print(f"{'='*60}\n{r.answer}\n{'='*60}\n")

        passed, failed = _check_journey_answer(answer)
        assert not failed, f"Failed checks: {failed}"
        assert r.iterations <= 10, f"Too many iterations: {r.iterations}"
        assert r.error is None, f"Agent error: {r.error}"

    @pytest.mark.asyncio
    async def test_architecture_overview(self, loan_journey_repo: Path):
        """The agent should describe the architecture from a brief README."""
        r = await _run_agent(
            MODEL_SONNET,
            "Describe the architecture of this system. What are the main modules?",
            str(loan_journey_repo),
        )
        answer = r.answer.lower()
        print(f"\n{'='*60}")
        print(f"[{r.model_name}] {r.iterations} iters, {r.tool_calls} calls, {r.duration_ms:.0f}ms")
        print(f"{'='*60}\n{r.answer}\n{'='*60}\n")

        assert "journey" in answer, "Missing journey module"
        assert "kyc" in answer or "identity" in answer, "Missing KYC module"
        assert "scoring" in answer or "credit" in answer, "Missing scoring module"
        assert r.iterations <= 10, f"Too many iterations: {r.iterations}"

    @pytest.mark.asyncio
    async def test_vague_query_no_keyword(self, loan_journey_repo: Path):
        """Vague query without explicit 'journey'/'flow' keyword.

        The agent must still discover the orchestration layer from context.
        """
        r = await _run_agent(
            MODEL_SONNET,
            "How does a loan application get processed from start to finish in this system?",
            str(loan_journey_repo),
        )
        answer = r.answer.lower()
        print(f"\n{'='*60}")
        print(f"[VAGUE QUERY] {r.iterations} iters, {r.tool_calls} calls")
        print(f"{'='*60}\n{r.answer}\n{'='*60}\n")

        passed, failed = _check_journey_answer(answer)
        # Allow at most 1 missing step for vague queries
        assert len(failed) <= 1, f"Too many failed checks for vague query: {failed}"
        assert r.iterations <= 12, f"Too many iterations: {r.iterations}"

    @pytest.mark.asyncio
    async def test_decoy_resistance(self, loan_journey_repo: Path):
        """Agent should NOT confuse the notification flow or legacy journey with the real one."""
        r = await _run_agent(
            MODEL_SONNET,
            "What are the steps in the loan journey? Don't include deprecated or notification steps.",
            str(loan_journey_repo),
        )
        answer = r.answer.lower()
        print(f"\n{'='*60}")
        print(f"[DECOY RESISTANCE] {r.iterations} iters, {r.tool_calls} calls")
        print(f"{'='*60}\n{r.answer}\n{'='*60}\n")

        # Should NOT mention legacy 3-step journey
        assert "basic_check" not in answer, "Answer includes deprecated legacy step"
        assert "auto_scoring" not in answer, "Answer includes deprecated legacy step"
        # Should NOT include notification flow steps
        assert "welcome_email" not in answer, "Answer includes notification flow step"
        # Should mention the real 5 steps
        passed, failed = _check_journey_answer(answer)
        assert not failed, f"Failed checks: {failed}"


@pytest.mark.integration
@_skip_no_creds
class TestMultiModelComparison:
    """Compare Sonnet vs Opus on the same queries.

    These tests run the same query on both models and print a comparison.
    The test PASSes if BOTH models pass the quality checks. If only Sonnet
    fails but Opus passes, it's a signal we need to tune steering further.
    """

    @pytest.mark.asyncio
    async def test_compare_journey_steps(self, loan_journey_repo: Path):
        """Compare both models on the journey steps question."""
        query = "What are the steps in the loan application journey? List all steps in order."
        ws = str(loan_journey_repo)

        results: List[ModelRunResult] = []
        for _, model_id in _COMPARE_MODELS.items():
            r = await _run_agent(model_id, query, ws)
            p, f = _check_journey_answer(r.answer.lower())
            r.passed_checks = p
            r.failed_checks = f
            results.append(r)

        _print_comparison(results)

        # Both models should pass all checks
        for r in results:
            assert not r.failed_checks, (
                f"{r.model_name} failed: {r.failed_checks}"
            )

    @pytest.mark.asyncio
    async def test_compare_vague_query(self, loan_journey_repo: Path):
        """Compare both models on a vague query without explicit keywords."""
        query = "How does a loan application get processed from start to finish in this system?"
        ws = str(loan_journey_repo)

        results: List[ModelRunResult] = []
        for _, model_id in _COMPARE_MODELS.items():
            r = await _run_agent(model_id, query, ws)
            p, f = _check_journey_answer(r.answer.lower())
            r.passed_checks = p
            r.failed_checks = f
            results.append(r)

        _print_comparison(results)

        # Both models: allow at most 1 missed check
        for r in results:
            assert len(r.failed_checks) <= 1, (
                f"{r.model_name} failed too many checks: {r.failed_checks}"
            )
