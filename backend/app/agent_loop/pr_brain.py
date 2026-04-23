"""PR Brain — coordinator-worker orchestrator for PR reviews.

Agent-as-tool design: ONE Brain (Sonnet) acts as the coordinator. It
surveys the diff, plans investigations, dispatches scope-bounded
workers (Haiku, via ``dispatch_subagent``), replans on surprises, and
synthesises a final review. Mechanical safety nets run alongside the
LLM loop — Phase 2 existence check plus P13 / P14 deterministic
verifiers catch compilation-class and stub-call bug classes regardless
of LLM sampling.

Flow:
  Phase 1: Pre-compute (parse diff, classify risk, prefetch diffs, impact graph)
  Phase 2: Existence check (LLM + P13 phantom-symbol scanners + P14 stub detector)
  Phase 3: Coordinator dispatch loop (survey + dispatch_subagent + synthesise)
  Phase 4: Post-process (missing-symbol injection, reflection, diff-scope filter)
  Phase 5: Merge recommendation (deterministic)
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

from app.agent_loop.lifecycle import fire_hook
from app.ai_provider.base import AIProvider
from app.code_review.diff_parser import parse_diff
from app.code_review.models import (
    PRContext,
    ReviewFinding,
    RiskProfile,
)
from app.code_review.risk_classifier import classify_risk
from app.code_review.shared import (
    build_impact_context,
    compute_budget_multiplier,
    prefetch_diffs,
    should_reject_pr,
)
from app.code_tools.executor import ToolExecutor
from app.workflow.models import PRBrainConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunable parameters are loaded from config/brains/pr_review.yaml via
# PRBrainConfig.  Only true constants (regex, enum maps) stay here.
# ---------------------------------------------------------------------------

# Wall-clock cap (seconds) for the Phase 2 existence-check worker. The
# worker does a handful of greps to verify newly-referenced symbols;
# prompt-level budgets are hints that LLM workers often ignore on large
# codebases (~10 min hangs observed on 17K-file repos). This is the
# Hard orchestrator guard — after this deadline, the LLM worker is
# cancelled and the coordinator proceeds with only P13 deterministic
# facts. Lowered in v2u from 120s: Phase 2 now runs P13 (Python / Go /
# Java mechanical scans) BEFORE the LLM worker, so the worker's task
# is already narrowed to signature-level checks that P13 cannot do.
# 60s is enough for the narrow task; 120s was pure waste on cold
# large-repo reviews where the worker never produced facts anyway
# (observed 4/10 sentry cases + 6/10 grafana + 6/9 keycloak timed out
# with zero symbols in v2t regression).
_PHASE2_TIMEOUT_SECONDS = 60


# ---------------------------------------------------------------------------
# Mandatory-dispatch path detector
# ---------------------------------------------------------------------------
# Pattern: coordinator prompt already has "Hard floors" language telling it
# to dispatch `security` on auth/crypto paths and `reliability` on DB
# migrations, but LLM honour-rate is sub-100% — especially on small PRs
# where the coordinator judges that a survey-only pass is enough. That's
# how PR #14227 (1339-line change touching
# .../common/v3/security/ + .../service/v3/V3CmsAuthService.java) shipped
# a plaintext-password cmp bug: coordinator saw the files, decided no
# dispatch was needed, and the hard floor got silently ignored.
#
# This detector runs in Phase 1 (deterministic, pre-coordinator) against
# the diff's file paths. When matched, the coordinator's task text gets a
# "## MANDATORY investigations" section listing the required roles with
# evidence of the trigger. The section uses strong enforcement language
# ("non-skippable", "first dispatches must satisfy this list") and the
# coordinator's non-honour becomes visible in logs + trace.
#
# NOT the same as the coordinator prompt's "Hard floors" text — that was
# advisory; this is path-anchored, evidence-attached, and always appears
# in the user message (not the system prompt), so it's fresher context.

_MANDATORY_DISPATCH_RULES: List[tuple] = [
    # (role, reason, regex matched against diff file paths).
    # Pattern matches any PATH SEGMENT containing the keyword — covers
    # both path segments (``.../security/...``) and camelCase filenames
    # (``V3CmsAuthService.java`` contains "Auth"). Case-insensitive.
    # False-positive risk ("authors/" matches "auth") is acceptable:
    # a mis-dispatched security role costs ~$0.30 and always improves
    # review depth — strictly better than silently skipping on a real
    # auth path (which cost us the PR #14227 plaintext-password miss).
    (
        "security",
        "auth / crypto / session / token / password path touched — "
        "plaintext comparisons, timing attacks, missing gate coverage, "
        "and secret leakage are the common failure modes here",
        re.compile(
            r"(?:^|/)[a-zA-Z0-9_]*"
            r"(?:auth|security|oauth|jwt|session|crypto|token|"
            r"password|credential|secret|signin|signup|login|logout|"
            r"permission|acl|rbac)"
            r"[a-zA-Z0-9_]*"
            r"(?:/|$|\.)",
            re.IGNORECASE,
        ),
    ),
    (
        "reliability",
        "DB migration / schema change detected — NOT NULL without default, "
        "exclusive locks on large tables, and irreversible migrations ship "
        "outages and data loss; dedicated dispatch required",
        re.compile(
            r"(?:^|/)(?:migrations?|changelog|flyway|liquibase)(?:/|$)"
            r"|V\d+__[A-Za-z0-9_]+\.sql$",
            re.IGNORECASE,
        ),
    ),
]


# ---------------------------------------------------------------------------
# Tier 2 — diff content scanner
# ---------------------------------------------------------------------------
# Path-based rules miss any PR whose filename doesn't advertise the
# concern. Real example: PR #14234's IP whitelist endpoint lived under
# ``loan/service/SandBoxServiceImpl.java`` — functionally security-
# relevant (allowlist, production env-gate, Redis trust boundary) but
# path-pattern-invisible. Tier 2 scans the diff's ``+`` lines for
# security / reliability **primitives** — the APIs, annotations,
# imports, and concept words that are load-bearing regardless of
# where the file lives.
#
# Each pattern produces one finding: {role, reason, file, line,
# matched_snippet}. These merge into Tier 1's path-based findings by
# role; a single role gets ONE entry with matching_paths aggregated.

# File extension → language tag used to pick which pattern set to run.
# Missing extension (e.g. Makefile, .yml) → only generic patterns.
_EXT_TO_LANG: Dict[str, str] = {
    ".java": "java",
    ".kt": "kotlin",      # kotlin reuses Java Spring Security
    ".py": "python",
    ".go": "go",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
}


def _compile_content_patterns(raw: List[tuple], *, case_insensitive: bool = False) -> List[tuple]:
    """Pre-compile the (regex, reason) pairs for one language."""
    flags = re.MULTILINE
    if case_insensitive:
        flags |= re.IGNORECASE
    return [(re.compile(p, flags), r) for p, r in raw]


# Java + Kotlin (Spring Security ecosystem): annotations, security
# classes, and crypto / JWT library usage.
_SECURITY_PATTERNS_JAVA: List[tuple] = _compile_content_patterns([
    (r"@(?:PreAuthorize|Secured|RolesAllowed|WithMockUser|EnableWebSecurity|PermitAll|DenyAll|PostAuthorize|PreFilter|PostFilter)\b",
     "Spring Security annotation (access control)"),
    (r"\bnew\s+(?:BCrypt|Argon2|Pbkdf2|SCrypt)PasswordEncoder\s*\(",
     "Password encoder constructor (hashing policy)"),
    (r"\b(?:HttpSecurity|SecurityFilterChain|AuthenticationManager|AuthenticationProvider|UserDetailsService|PasswordEncoder|JwtDecoder|JwtAuthenticationConverter|OAuth2AuthenticationToken|JwtEncoder)\b",
     "Spring Security configuration / token primitive"),
    (r"\bMessageDigest\.isEqual\s*\(", "Constant-time byte comparison"),
    (r"\bSecureRandom\s*\(", "Cryptographic RNG construction"),
    (r"\bJwts\.(?:builder|parser|parserBuilder|SIG)\b",
     "JJWT library call (token sign/verify)"),
    (r'"grant_type"\s*[:,]|"access_token"\s*[:,]|"refresh_token"\s*[:,]',
     "OAuth2 grant / token field string"),
    (r"\bCipher\.getInstance\s*\(", "Cipher construction (crypto primitive)"),
    (r"\b(?:KeyPairGenerator|KeyFactory|KeyGenerator)\.getInstance\s*\(",
     "Crypto key material setup"),
])

# Python: decorators + security library imports + password / crypto funcs.
_SECURITY_PATTERNS_PYTHON: List[tuple] = _compile_content_patterns([
    (r"^\s*@(?:login_required|permission_required|csrf_exempt|staff_member_required|user_passes_test|api_key_required|token_required)\b",
     "Auth / CSRF decorator"),
    (r"^\s*(?:from|import)\s+(?:bcrypt|cryptography|jose|jwt|passlib|authlib|django_otp|oauthlib|pyotp|argon2)\b",
     "Security library import"),
    (r"\b(?:check_password|make_password|compare_digest|pbkdf2_hmac|constant_time_compare)\s*\(",
     "Password / constant-time function call"),
    (r"\bbcrypt\.(?:hashpw|checkpw|gensalt)\s*\(", "bcrypt call"),
    (r"\bhmac\.(?:compare_digest|new)\s*\(", "HMAC operation"),
    (r"\bjwt\.(?:encode|decode|get_unverified_claims)\s*\(", "JWT encode/decode"),
    (r"\b(?:AES|RSA|Fernet|Ed25519|X25519)\.", "Cryptographic primitive class"),
])

# Go: security-critical std + popular libraries.
_SECURITY_PATTERNS_GO: List[tuple] = _compile_content_patterns([
    (r'"(?:crypto/subtle|crypto/rand|crypto/hmac|crypto/rsa|crypto/ecdsa|crypto/tls|crypto/x509)"',
     "Crypto stdlib import"),
    (r'"(?:golang\.org/x/crypto/bcrypt|golang\.org/x/crypto/argon2|golang\.org/x/crypto/scrypt)"',
     "Password hashing library import"),
    (r'"(?:github\.com/golang-jwt/jwt|github\.com/dgrijalva/jwt-go|github\.com/lestrrat-go/jwx)',
     "JWT library import"),
    (r"\bsubtle\.ConstantTimeCompare\s*\(", "Constant-time comparison"),
    (r"\bbcrypt\.(?:CompareHashAndPassword|GenerateFromPassword)\s*\(",
     "bcrypt operation"),
    (r"\bjwt\.(?:Parse|ParseWithClaims|Sign|New|NewWithClaims)\b", "JWT operation"),
    (r"\bmiddleware\.(?:BasicAuth|JWTAuth|RequireAuth)\b", "Auth middleware"),
])

# TypeScript / JavaScript (shared): Node + React + browser auth patterns.
_SECURITY_PATTERNS_TSJS: List[tuple] = _compile_content_patterns([
    # Imports / requires from auth/security packages
    (r"(?:from\s+|require\s*\(\s*)['\"](?:jsonwebtoken|bcrypt(?:js)?|passport(?:-[\w-]+)?|express-session|next-auth|@auth0/[\w-]+|@okta/[\w-]+|firebase/auth|@clerk/[\w-]+|iron-session|cookie-session|csurf|helmet|express-rate-limit|argon2|scrypt-kdf)['\"]",
     "Auth/security npm package import"),
    # JWT / bcrypt function calls
    (r"\b(?:jwt\.(?:sign|verify|decode)|bcrypt\.(?:compare|hash|genSalt))\s*\(",
     "JWT / bcrypt call"),
    # Browser credential storage — strong signal for XSS/exfil risk
    (r"(?:localStorage|sessionStorage)\.(?:setItem|getItem)\s*\(\s*['\"](?:token|auth|session|jwt|accessToken|refreshToken|apiKey)",
     "Browser-storage credential (XSS exfil surface)"),
    (r"document\.cookie\s*[=+]", "Direct cookie write"),
    # React auth components / hooks
    (r"<(?:AuthGuard|ProtectedRoute|RequireAuth|RoleGuard|PrivateRoute|AuthProvider)\b",
     "React auth wrapper component"),
    (r"\b(?:useAuth|useSession|useUser|useClerk|useAuth0)\s*\(", "Auth React hook"),
    # Passport / middleware
    (r"\bpassport\.authenticate\s*\(", "Passport strategy invocation"),
    # CSRF / CORS middleware
    (r"\b(?:csrf|csurf|helmet|cors)\s*\(\s*\{?", "Security middleware invocation"),
])

# Map language tag → pattern list so extension lookup stays O(1).
_SECURITY_PATTERNS_BY_LANG: Dict[str, List[tuple]] = {
    "java": _SECURITY_PATTERNS_JAVA,
    "kotlin": _SECURITY_PATTERNS_JAVA,
    "python": _SECURITY_PATTERNS_PYTHON,
    "go": _SECURITY_PATTERNS_GO,
    "typescript": _SECURITY_PATTERNS_TSJS,
    "javascript": _SECURITY_PATTERNS_TSJS,
}

# Cross-language: concept words that signal security relevance regardless
# of filename / language. Case-insensitive so camelCase (`addCountIpWhitelist`),
# SNAKE_CASE (`COUNT_IP_WHITELIST_KEY`), and plain (`whitelist`) all match
# the same token. Word-boundary dropped on the list-concept patterns
# because tokens commonly appear embedded in identifiers
# (`addCountIpWhitelist` → contains `Whitelist`).
_GENERIC_SECURITY_PATTERNS: List[tuple] = _compile_content_patterns(
    [
        (r"(?:whitelist|allowlist|blocklist|denylist|blacklist)",
         "Allow/deny list concept"),
        (r"(?:firewall|ratelimit|rate_limit|throttl\w*)",
         "Firewall / rate limit concept"),
        (r"\b(?:allowed_ips?|denied_ips?|trusted_ips?|blocked_ips?)\b",
         "IP allow/deny list"),
        (r"\bcsrf[-_]?token\b|\bcsrf_exempt\b|\bSameSite\b|\bHttpOnly\b|\bSecure\s*[;=]",
         "Cookie / CSRF security attribute"),
        (r"\b(?:Bearer |Basic )\s+?\{?[A-Za-z0-9._-]+\}?",
         "HTTP Authorization scheme literal"),
    ],
    case_insensitive=True,
)

# Reliability content patterns — DDL / migration SQL that may ship
# outages regardless of whether the file sits in a /migrations/ dir.
_RELIABILITY_CONTENT_PATTERNS: List[tuple] = _compile_content_patterns([
    (r"\bALTER\s+TABLE\b.*?\b(?:ADD|DROP|ALTER|RENAME)\s+COLUMN\b",
     "DDL column change (lock / rewrite risk)"),
    (r"\bDROP\s+(?:TABLE|INDEX|CONSTRAINT|VIEW)\b",
     "Destructive DDL"),
    (r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\b",
     "Index creation (potentially long lock)"),
])


def _detect_required_dispatches_from_diff_content(
    file_diffs: Dict[str, str],
    *,
    max_matches_per_role: int = 10,
) -> List[Dict[str, Any]]:
    """Tier 2 detector — scan every `+` line across the diff for
    security / reliability primitives, regardless of path.

    Returns ``[{role, reason, matching_evidence: [{file, line, snippet}]}, ...]``
    where ``matching_evidence`` is capped at ``max_matches_per_role`` so
    a huge diff doesn't produce an unreadable block.

    Scan strategy:
    - Per file, pick the language-specific pattern list by extension.
    - Also run the generic / reliability pattern lists on every file.
    - Only `+` (added) lines matter — existing code isn't this PR's
      concern.
    """
    if not file_diffs:
        return []

    import os as _os

    # role → list of {file, line, snippet, reason}
    hits: Dict[str, List[Dict[str, Any]]] = {}

    def _record(role: str, reason: str, file_path: str, line_no: int, snippet: str) -> None:
        bucket = hits.setdefault(role, [])
        if len(bucket) >= max_matches_per_role:
            return
        bucket.append({
            "file": file_path,
            "line": line_no,
            "snippet": snippet[:120],  # truncate for log / prompt safety
            "reason": reason,
        })

    for file_path, diff_text in file_diffs.items():
        ext = _os.path.splitext(file_path)[1].lower()
        lang = _EXT_TO_LANG.get(ext)
        lang_patterns = _SECURITY_PATTERNS_BY_LANG.get(lang or "", [])

        current_new_line = 0
        for raw in diff_text.splitlines():
            if raw.startswith("@@"):
                m = _DIFF_HUNK_HEADER_RE.match(raw)
                if m:
                    current_new_line = int(m.group(1))
                continue
            if raw.startswith(("---", "+++")):
                continue
            if raw.startswith("+") and not raw.startswith("+++"):
                body = raw[1:]
                # Language-specific security patterns
                for pat, reason in lang_patterns:
                    if pat.search(body):
                        _record("security", reason, file_path, current_new_line, body.strip())
                # Cross-language security concepts
                for pat, reason in _GENERIC_SECURITY_PATTERNS:
                    if pat.search(body):
                        _record("security", reason, file_path, current_new_line, body.strip())
                # Reliability (DDL / migration content)
                for pat, reason in _RELIABILITY_CONTENT_PATTERNS:
                    if pat.search(body):
                        _record("reliability", reason, file_path, current_new_line, body.strip())
            if not raw.startswith("-"):
                current_new_line += 1

    results: List[Dict[str, Any]] = []
    # Preserve the same role ordering as Tier 1 (security, then reliability).
    for role in ("security", "reliability"):
        if role not in hits:
            continue
        # Unique reasons summary — one reason string covering all triggered
        # patterns, for use in the coordinator prompt.
        reasons = sorted({h["reason"] for h in hits[role]})
        combined_reason = (
            "Diff content matches security / reliability primitives — "
            "even though the file path doesn't self-declare as security-"
            "critical, the code added here is (triggers: "
            + ", ".join(reasons)
            + ")"
        )
        results.append({
            "role": role,
            "reason": combined_reason,
            "matching_evidence": hits[role],
        })
    return results


def _detect_required_dispatches(
    file_diffs: Dict[str, str],
) -> List[Dict[str, Any]]:
    """Return mandatory-dispatch role requirements keyed off diff paths
    (Tier 1) AND `+` line content primitives (Tier 2).

    Tier 1 output shape per entry: ``{role, reason, matching_paths}``.
    Tier 2 output shape per entry: ``{role, reason, matching_evidence}``
    where evidence is a list of ``{file, line, snippet, reason}`` dicts.

    When both tiers trigger the same role, both entries are returned —
    the coordinator prompt renderer lists path-level triggers first,
    then content-level triggers, so the LLM sees both justifications.

    Empty list when nothing fires.
    """
    if not file_diffs:
        return []
    paths = list(file_diffs.keys())
    requirements: List[Dict[str, Any]] = []

    # Tier 1 — path-anchored
    for role, reason, pattern in _MANDATORY_DISPATCH_RULES:
        matches = sorted({p for p in paths if pattern.search(p)})
        if matches:
            requirements.append({
                "role": role,
                "reason": reason,
                "matching_paths": matches,
                "_tier": 1,
            })

    # Tier 2 — diff content
    for entry in _detect_required_dispatches_from_diff_content(file_diffs):
        entry["_tier"] = 2
        requirements.append(entry)

    return requirements


# P12b — Dimension-worker triggers (Tier 3, opt-in)
# ------------------------------------------------------------------
# The previous detectors (Tier 1 path, Tier 2 content) FORCE a
# role dispatch. Dimension triggers are SUGGESTIONS: a multi-caller
# changed file is a natural candidate for cross-file sweep. The
# coordinator decides whether to actually fire dispatch_dimension_worker
# or stay with scoped dispatches — dimension is expensive (180K-ish),
# so we don't make it mandatory.
_DIMENSION_TRIGGER_MIN_CALLER_FILES = 3
_DIMENSION_TRIGGER_MIN_SYMBOLS = 5


def _detect_dimension_triggers(
    workspace_path: str,
    pr_context,
) -> List[Dict[str, Any]]:
    """Scan changed files for cross-file caller footprints. A file with
    ≥3 distinct caller files (or ≥5 calling symbols across files) is
    a natural dimension-worker target — file-range dispatch would split
    the caller graph into separate unrelated slices.

    Output shape per entry:
        {
            "file": "path/to/changed.py",
            "caller_files": ["a.py", "b.py", "c.py", ...],
            "caller_count": 7,
            "hotspot_symbols": ["Foo.bar", "Foo.baz", ...],
        }

    Fail-soft: any exception during dependency lookup returns the
    triggers we have so far (never crashes Phase 1).
    """
    try:
        from app.code_tools.tools import get_dependents
    except ImportError:
        return []

    biz_files = []
    try:
        biz_files = pr_context.business_logic_files()
    except Exception:
        return []
    if not biz_files:
        return []

    triggers: List[Dict[str, Any]] = []
    for f in biz_files[:15]:
        try:
            result = get_dependents(workspace=workspace_path, file_path=f.path)
        except Exception:
            continue
        if not (result.success and result.data):
            continue

        caller_files: List[str] = []
        hotspot_symbols_set: set = set()
        for d in result.data[:20]:
            cf = d.get("file_path") or ""
            if cf and cf != f.path:
                caller_files.append(cf)
            for sym in (d.get("symbols") or [])[:5]:
                if sym:
                    hotspot_symbols_set.add(sym)

        caller_files_distinct = sorted(set(caller_files))
        fires = (
            len(caller_files_distinct) >= _DIMENSION_TRIGGER_MIN_CALLER_FILES
            or len(hotspot_symbols_set) >= _DIMENSION_TRIGGER_MIN_SYMBOLS
        )
        if fires:
            triggers.append({
                "file": f.path,
                "caller_files": caller_files_distinct[:10],
                "caller_count": len(caller_files_distinct),
                "hotspot_symbols": sorted(hotspot_symbols_set)[:10],
            })

    return triggers


def _dimension_dispatch_cap(n_files: int) -> int:
    """Return the max number of dimension workers allowed for a PR of
    this size.

    <5 files → 0 (not worth the budget)
    5-14    → 1
    ≥15     → 2
    """
    if n_files < 5:
        return 0
    if n_files < 15:
        return 1
    return 2


# ---------------------------------------------------------------------------


class WorkflowEvent:
    """Lightweight event container compatible with WorkflowEngine's event queue."""

    def __init__(self, kind: str, data: Dict[str, Any]):
        self.kind = kind
        self.data = data


# Synthesis system prompt — shared with synthesis-style callers in review flow.
_SYNTHESIS_SYSTEM_PROMPT = """\
You are the **final judge** in a multi-agent code review. You receive:
- Findings from specialized review agents (the **prosecution** — evidence FOR each issue)
- Challenge results from an arbitration agent (the **defense** — counter-evidence AGAINST)

Your job is to weigh both sides and produce the definitive review.

## Rules

1. **You decide severity.** The sub-agent's severity is a recommendation. \
The arbitrator's suggested severity is a counter-recommendation. You weigh \
the evidence and counter-evidence to set the final severity.
2. **High rebuttal confidence (>0.7) = likely downgrade or drop.** If the \
arbitrator found concrete counter-evidence, take it seriously.
3. **Low rebuttal confidence (<0.3) = finding is solid.** Keep the sub-agent's severity.
4. **Do not invent new issues.** Only discuss findings provided to you.
5. **Be precise.** Every finding must reference specific file:line locations.
6. **Consolidate duplicates.** Same root cause → one finding.
7. **Actionable fixes.** Concrete implementations, not "consider adding".
8. **Proportional tone.** Match review depth to actual risk.
9. **Praise good patterns** if applicable.

## Output format

```markdown
## Code Review Summary

<1-3 sentence overall assessment>

### Critical Issues
<numbered list, or "None" if no critical issues>

### Warnings
<numbered list, or "None">

### Suggestions & Nits
<numbered list, or "None">

### What's Done Well
<brief positive feedback if applicable>

### Recommendation
<One of: **Approve**, **Approve with follow-ups**, **Request Changes**>
<1 sentence justification>
```
"""


class PRBrainOrchestrator:
    """Deterministic pipeline for PR reviews, dispatching agents via Brain infrastructure.

    This is NOT an LLM loop. The workflow is fixed:
      1. Pre-compute context (deterministic)
      2. Dispatch review agents (LLM, via AgentToolExecutor)
      3. Post-process findings (deterministic)
      4. Dispatch arbitration agent (LLM)
      5. Merge recommendation (deterministic)
      6. Synthesis (LLM)
    """

    def __init__(
        self,
        provider: AIProvider,
        explorer_provider: AIProvider,
        workspace_path: str,
        diff_spec: str,
        pr_brain_config: PRBrainConfig,
        agent_registry: Dict[str, Any],
        tool_executor: ToolExecutor,
        trace_writer=None,
        event_sink: Optional[asyncio.Queue] = None,
        scratchpad=None,
        task_id: Optional[str] = None,
        pr_title: str = "",
        pr_description: str = "",
    ):
        self._provider = provider
        self._explorer_provider = explorer_provider
        self._workspace_path = workspace_path
        self._diff_spec = diff_spec
        self._config = pr_brain_config
        self._agent_registry = agent_registry
        self._trace_writer = trace_writer
        self._event_sink = event_sink
        self._task_id = task_id
        # PR intent — plumbed from caller; coordinator surfaces in user
        # message so agents can check "does this PR actually do what it
        # claims?" not just "is this diff pattern-wise suspicious?".
        self._pr_title = pr_title or ""
        self._pr_description = pr_description or ""

        # Phase 9.15 — task-scoped Fact Vault. Sub-agent tool calls are
        # routed through a CachedToolExecutor so identical grep / read_file /
        # find_symbol queries across 7 parallel review agents hit the vault
        # instead of re-running. Opt out via CONDUCTOR_SCRATCHPAD_ENABLED=0.
        #
        # ``task_id`` (e.g. "ado-pr-12345", "greptile-sentry-006") is folded
        # into the session_id so concurrent PR reviews produce readable
        # scratchpad filenames — isolation was already guaranteed by
        # per-session files, this just makes them traceable.
        import os as _os
        import re as _re
        import uuid as _uuid

        from app.scratchpad import CachedToolExecutor, FactStore

        self._owns_scratchpad = False
        if _os.environ.get("CONDUCTOR_SCRATCHPAD_ENABLED", "1") != "0" and scratchpad is None:
            if task_id:
                slug = _re.sub(r"[^A-Za-z0-9._-]+", "-", task_id).strip("-")[:48] or "pr"
                session_id = f"{slug}-{_uuid.uuid4().hex[:8]}"
            else:
                session_id = f"pr-{_uuid.uuid4().hex[:12]}"
            scratchpad = FactStore.open(
                session_id, workspace=workspace_path, task_id=task_id
            )
            self._owns_scratchpad = True
        self._scratchpad = scratchpad

        # Token returned by contextvars.ContextVar.set so cleanup() can
        # reset binding exactly once, even if cleanup is called twice.
        self._scratchpad_ctx_token = None
        if scratchpad is not None:
            from app.scratchpad.context import _current_store

            self._scratchpad_ctx_token = _current_store.set(scratchpad)
            self._tool_executor = CachedToolExecutor(tool_executor, scratchpad)
        else:
            self._tool_executor = tool_executor

    async def run_stream(self) -> AsyncGenerator[WorkflowEvent, None]:
        """Execute the full PR review pipeline, yielding progress events.

        Phases:
          1. Parse diff and classify risk (deterministic, no LLM).
          2. Dispatch review agents in parallel.
          3. Post-process findings (filter, dedup, rank).
          4. Arbitration agent challenges each finding.
          5. Merge recommendation (deterministic).
          6. Synthesis via the strong model (final judge).

        Yields:
            WorkflowEvent instances with kinds:
            ``pr_brain_start``, ``pr_context``, ``agents_dispatching``,
            ``agents_complete``, ``post_processing``, ``arbitration_complete``,
            ``done`` (or an early ``done`` on empty diff / oversized PR).
        """
        start_time = time.monotonic()

        logger.info(
            "PR Brain starting: workspace=%s, diff_spec=%s",
            self._workspace_path,
            self._diff_spec,
        )

        yield WorkflowEvent(
            "pr_brain_start",
            {
                "diff_spec": self._diff_spec,
                "workspace_path": self._workspace_path,
            },
        )

        # ------------------------------------------------------------------
        # Phase 1: Pre-compute (deterministic, no LLM calls)
        # ------------------------------------------------------------------

        pr_context = parse_diff(self._workspace_path, self._diff_spec)
        # Attach PR intent so downstream agents see "what this PR is
        # supposed to do" not just raw diff bytes. See __init__.
        pr_context.title = self._pr_title
        pr_context.description = self._pr_description
        logger.info(
            "PR parsed: %d files, %d lines changed, title=%r",
            pr_context.file_count,
            pr_context.total_changed_lines,
            (pr_context.title[:80] if pr_context.title else "(none)"),
        )

        if pr_context.file_count == 0:
            yield WorkflowEvent(
                "done",
                {
                    "answer": "No changes found in the diff.",
                    "findings": [],
                    "merge_recommendation": "approve",
                },
            )
            return

        rejection = should_reject_pr(
            pr_context,
            max_lines=self._config.limits.reject_above,
        )
        if rejection:
            yield WorkflowEvent(
                "done",
                {
                    "answer": rejection,
                    "findings": [],
                    "merge_recommendation": "request_changes",
                },
            )
            return

        risk_profile = classify_risk(pr_context)
        file_diffs = prefetch_diffs(self._workspace_path, self._diff_spec)
        impact_context = build_impact_context(self._workspace_path, pr_context)
        budget_multiplier = compute_budget_multiplier(pr_context)

        # Phase 9.17 lifecycle hook — pre-coordinator survey complete.
        # PR context, risk profile, impact graph all available; coordinator
        # is about to start dispatching. Hook consumers can peek at the
        # PR shape for telemetry / risk-classifier plugins / etc.
        fire_hook(
            "on_survey_complete",
            orchestrator=self,
            data={
                "pr_context": pr_context,
                "risk_profile": risk_profile,
                "impact_context": impact_context,
                "budget_multiplier": budget_multiplier,
                "file_count": len(pr_context.files),
            },
        )

        logger.info(
            "Risk: correctness=%s, concurrency=%s, security=%s, reliability=%s, operational=%s | budget=%.1fx",
            risk_profile.correctness.value,
            risk_profile.concurrency.value,
            risk_profile.security.value,
            risk_profile.reliability.value,
            risk_profile.operational.value,
            budget_multiplier,
        )

        yield WorkflowEvent(
            "pr_context",
            {
                "file_count": pr_context.file_count,
                "total_lines": pr_context.total_changed_lines,
                "budget_multiplier": budget_multiplier,
            },
        )

        # ------------------------------------------------------------------
        # Phase 2: Brain-as-coordinator dispatch loop (agent-as-tool)
        # ------------------------------------------------------------------
        # A single Brain (Sonnet) drives the coordinator loop described in
        # config/skills/pr_brain_coordinator.md. Brain surveys the PR,
        # plans investigations, dispatches scope-bounded sub-agents via
        # dispatch_subagent, replans on unexpected observations, and
        # synthesises with unified severity classification.
        async for event in self._run_v2_coordinator(
            pr_context, risk_profile, file_diffs, impact_context,
            budget_multiplier, start_time,
        ):
            yield event

    # ------------------------------------------------------------------
    # PR Brain v2 — coordinator loop
    # ------------------------------------------------------------------

    async def _run_v2_coordinator(
        self,
        pr_context: PRContext,
        risk_profile: RiskProfile,
        file_diffs: Dict[str, str],
        impact_context: str,
        budget_multiplier: float,
        start_time: float,
    ) -> AsyncGenerator[WorkflowEvent, None]:
        """Brain-as-coordinator loop for PR Brain v2.

        Instead of dispatching 7 fixed-role agents in parallel, we spawn ONE
        Brain (Sonnet) with:
          * system prompt = pr_brain_coordinator skill (the 5-phase loop +
            3-check contract + severity rubric)
          * tools = read-only survey tools + ``dispatch_subagent`` (the
            v2 primitive that runs scope-bounded workers returning
            severity-null findings)
          * user message = diff + impact_context + risk profile
        The Brain plans investigations, dispatches workers, replans, and
        emits a structured review directly as its final answer. We parse
        findings from the Brain's output and drop into the same
        post-processing / arbitration / synthesis phases the v1 path uses.

        Gated on CONDUCTOR_PR_BRAIN_V2=1. v1 path remains untouched when
        the flag is off — rollback is a single env var flip.
        """
        from app.workflow.loader import load_brain_config, load_swarm_registry

        from .brain import AgentToolExecutor, BrainBudgetManager
        from .config import BrainExecutorConfig

        logger.info(
            "[PR Brain v2] Coordinator loop starting: files=%d, lines=%d, budget=%.1fx",
            pr_context.file_count,
            pr_context.total_changed_lines,
            budget_multiplier,
        )

        yield WorkflowEvent(
            "v2_coordinator_start",
            {
                "mode": "pr_brain_v2",
                "file_count": pr_context.file_count,
            },
        )

        brain_config = load_brain_config()
        swarm_registry = load_swarm_registry()
        budget_mgr = BrainBudgetManager(
            self._config.limits.total_session_tokens,
        )
        llm_semaphore = asyncio.Semaphore(self._config.limits.llm_concurrency_limit)

        executor_cfg = BrainExecutorConfig(
            workspace_path=self._workspace_path,
            current_depth=0,
            max_depth=self._config.limits.max_depth,
            max_concurrent=self._config.limits.max_concurrent_agents,
            sub_agent_timeout=self._config.limits.sub_agent_timeout,
        )

        executor = AgentToolExecutor(
            inner_executor=self._tool_executor,
            agent_registry=self._agent_registry,
            swarm_registry=swarm_registry,
            agent_provider=self._explorer_provider,  # haiku for sub-agents
            strong_provider=self._provider,          # sonnet = the Brain itself
            config=executor_cfg,
            brain_config=brain_config,
            trace_writer=self._trace_writer,
            event_sink=self._event_sink,
            budget_manager=budget_mgr,
            llm_semaphore=llm_semaphore,
        )

        # ------------------------------------------------------------------
        # Phase 2 — Verify (existence-check sub-agent).
        #
        # Before planning any logic investigations, we dispatch ONE
        # mechanical worker whose job is to verify that every symbol the
        # diff newly references actually exists in the codebase. Its
        # output becomes authoritative existence_facts in the vault;
        # missing symbols short-circuit into "ImportError at runtime"
        # findings without needing a logic-check dispatch.
        #
        # Skipped when CONDUCTOR_PR_BRAIN_V2_SKIP_EXISTENCE=1 for
        # fallback / A-B test scenarios.
        # ------------------------------------------------------------------
        existence_summary = ""
        import os as _os_v2phase2
        if _os_v2phase2.environ.get("CONDUCTOR_PR_BRAIN_V2_SKIP_EXISTENCE", "0") != "1":
            try:
                async for ev in self._run_v2_phase2_existence(
                    executor, pr_context, file_diffs,
                ):
                    yield ev
                existence_summary = self._format_existence_summary_for_coordinator()
            except Exception as exc:
                logger.warning(
                    "[PR Brain v2] Phase 2 existence check failed (non-fatal): %s", exc,
                )

        # Build the coordinator's task — diff + impact + coordinator skill.
        coordinator_query = self._build_v2_coordinator_query(
            pr_context, risk_profile, file_diffs, impact_context,
            existence_summary=existence_summary,
        )

        # Dispatch the Brain itself via dynamic-compose. It gets a tool pool
        # including dispatch_subagent, read-only survey tools, and runs the
        # 5-phase loop under the pr_brain_coordinator skill's direction.
        coordinator_tools = [
            "grep", "read_file", "find_symbol", "file_outline",
            "get_callers", "get_callees", "get_dependencies",
            "git_diff", "git_diff_files", "git_show", "git_log",
            "dispatch_subagent",
            "dispatch_dimension_worker",
        ]

        coordinator_params = {
            "perspective": (
                "You are the PR Brain coordinator. You survey the diff, "
                "plan focused investigations, dispatch scope-bounded "
                "sub-agents via dispatch_subagent, and synthesize the "
                "final review. You classify severity yourself using the "
                "2-question rubric (provable? + blast radius?)."
            ),
            "skill": "pr_brain_coordinator",
            "tools": coordinator_tools,
            "model": "strong",
            # Bumped from 25 → 32 iterations and 400K → 550K tokens to
            # accommodate multi-role-per-cluster dispatch (up to 5 roles
            # × up to 5 clusters on large PRs). Each dispatch consumes
            # ~1 iteration of the coordinator loop; large PRs can now
            # realistically plan 12-16 dispatches without starving the
            # Survey + Synthesize phases.
            "max_iterations": int(32 * budget_multiplier),
            "budget_tokens": int(550_000 * budget_multiplier),
            "query": coordinator_query,
            "budget_weight": 1.0,
        }

        coordinator_result = await executor.execute(
            "dispatch_agent", coordinator_params,
        )

        logger.info(
            "[PR Brain v2] Coordinator loop done: success=%s",
            coordinator_result.success,
        )

        # Parse the coordinator's final answer into ReviewFindings + synthesis.
        review_output = self._parse_v2_coordinator_output(
            coordinator_result, pr_context,
        )

        # Phase 9.17 lifecycle hook — coordinator finished all dispatches
        # and returned a draft. Precision filter / synthesis hasn't run
        # yet. Hook consumers can read the coordinator-emitted findings
        # before any post-processing reshapes them.
        fire_hook(
            "on_dispatch_complete",
            orchestrator=self,
            data={
                "coordinator_success": coordinator_result.success,
                "draft_findings": list(review_output.get("findings", [])),
                "draft_finding_count": len(review_output.get("findings", [])),
            },
        )

        # ------------------------------------------------------------------
        # Phase 6 — Precision filter with adaptive verifier.
        #
        # Split findings by confidence into 3 bands:
        #   * >= 0.8 : direct final finding
        #   * 0.5-0.8: dispatch verifier(s) (Haiku x N if <=2, Sonnet batch if >=3)
        #              — verifier verdict is terminal
        #   * < 0.5  : secondary_notes (not in findings array; appended to
        #              synthesis text)
        #
        # Skip via env CONDUCTOR_PR_BRAIN_V2_SKIP_VERIFY=1 for A/B testing.
        # ------------------------------------------------------------------
        import os as _os_v2phase6
        if (
            _os_v2phase6.environ.get("CONDUCTOR_PR_BRAIN_V2_SKIP_VERIFY", "0") != "1"
            and review_output["findings"]
        ):
            try:
                review_output = await self._apply_v2_precision_filter(
                    executor, review_output, pr_context, file_diffs,
                )
            except Exception as exc:
                logger.warning(
                    "[PR Brain v2] Precision filter failed (non-fatal): %s", exc,
                )

        yield WorkflowEvent(
            "v2_coordinator_complete",
            {
                "finding_count": len(review_output["findings"]),
            },
        )

        # Phase 9.17 lifecycle hook — synthesis finished, precision
        # filter has run, findings + final synthesis text are ready.
        # Consumers: telemetry / Langfuse export / extract reusable
        # learnings → memory consolidation (future Phase 9.15
        # long-term extension) / metrics aggregation.
        fire_hook(
            "on_synthesize_complete",
            orchestrator=self,
            data={
                "findings": list(review_output.get("findings", [])),
                "synthesis": review_output.get("synthesis", ""),
                "merge_recommendation": review_output.get("merge_recommendation"),
                "finding_count": len(review_output.get("findings", [])),
            },
        )

        # Files reviewed = PR diff files ∪ everything any subagent touched
        files_reviewed_set: set[str] = {f.path for f in pr_context.files}
        if coordinator_result.success and isinstance(coordinator_result.data, dict):
            for fp in coordinator_result.data.get("files_accessed", []):
                if fp:
                    files_reviewed_set.add(fp)

        duration_ms = (time.monotonic() - start_time) * 1000.0

        # Extract total token usage from the coordinator's BudgetController.
        # ``budget_summary`` is the dict emitted by ``budget.summary()`` —
        # has ``total_tokens`` which is input+output across every LLM turn
        # the coordinator made (including dispatched sub-agent calls that
        # share the coordinator's budget controller).
        _total_tokens = 0
        _total_iterations = 0
        if isinstance(coordinator_result.data, dict):
            _total_iterations = coordinator_result.data.get("iterations", 0)
            budget_summary = coordinator_result.data.get("budget_summary")
            if isinstance(budget_summary, dict):
                _total_tokens = int(budget_summary.get("total_tokens", 0) or 0)

        yield WorkflowEvent(
            "done",
            {
                "answer": review_output["synthesis"],
                "findings": review_output["findings"],
                "files_reviewed": sorted(files_reviewed_set),
                "merge_recommendation": review_output["merge_recommendation"],
                "duration_ms": duration_ms,
                "total_tokens": _total_tokens,
                "total_iterations": _total_iterations,
                "agents_dispatched": 1,  # the coordinator itself, sub-dispatches tracked separately
                "findings_before_arbitration": len(review_output["findings"]),
                "mode": "pr_brain_v2",
            },
        )

    async def _run_v2_phase2_existence(
        self,
        executor,
        pr_context: PRContext,
        file_diffs: Dict[str, str],
    ):
        """Dispatch ONE pr_existence_check worker. Its JSON output is
        parsed and persisted to the Fact Vault's ``existence_facts`` table
        so the coordinator (and later sub-agents) can query via
        ``search_facts(kind="existence")``.

        Yielding WorkflowEvent for observability.
        """
        yield WorkflowEvent(
            "v2_phase2_start", {"phase": "existence_verification"},
        )

        # Pack the diff text the worker needs to inspect. Keep bounded so
        # the worker doesn't drown in bytes.
        diff_block: List[str] = []
        remaining = 20_000
        for path, diff_text in file_diffs.items():
            if remaining <= 0:
                diff_block.append(f"[...additional diffs truncated — use git_diff for {path}...]")
                break
            slice_ = diff_text[:remaining]
            diff_block.append(f"### {path}\n```diff\n{slice_}\n```")
            remaining -= len(slice_)

        # v2u — P13 deterministic scanners run BEFORE this LLM worker, so
        # import-level existence (Python `from X import Y`, Go bare-call
        # identifiers, Java class references) is already covered by the
        # mechanical path and persisted to the Fact Vault. Narrow the
        # worker's task to the class of checks P13 structurally cannot
        # do — signature-level invariants. This is why the orchestrator
        # timeout halved from 120s to 60s.
        task_text = (
            "A mechanical deterministic scanner has already verified "
            "every new import-level symbol in this diff (Python "
            "`from X import Y`, Go bare-call identifiers, Java class "
            "references). Whatever it found missing has already been "
            "written to the Fact Vault as `exists=false` — the "
            "'Pre-verified symbols' section below lists them by name.\n\n"
            "Your job is the class of checks mechanical grep cannot do:\n\n"
            "1. **Method call signatures** — for new method calls on `+` "
            "lines, verify the callee's parameter list matches the "
            "invocation (arg count, kwarg names, positional order).\n"
            "2. **Class instantiation shape** — for new `Foo(...)` on `+` "
            "lines, verify `__init__` / constructor params match.\n"
            "3. **Attribute access** — for new `obj.attr` access where "
            "`attr` wasn't present before, verify the type declares it.\n"
            "4. **Decorator application** — for new decorator usage, "
            "verify the decorator exists AND accepts the args you "
            "observe.\n"
            "5. **Overload resolution** — for languages with method "
            "overloading (Java, TS), verify the call's argument types "
            "match at least one overload signature.\n\n"
            "Do NOT re-verify import-level existence — the mechanical "
            "scanner already handled that. Do NOT re-check whether a "
            "class or top-level function 'exists by name' — same lane. "
            "Focus on signature / invocation correctness on everything "
            "else.\n\n"
            "Use find_symbol as your primary tool; grep only when "
            "find_symbol doesn't expose the signature info you need. "
            "Emit the JSON schema from your system prompt as your final "
            "message."
        )

        # P9 — per-language verification hint. Only injected when the diff
        # touches that language, so a Go-only PR doesn't pay for Java
        # prompt tokens. All four mainstream languages prefer
        # `find_symbol` over grep because tree-sitter handles overloads,
        # receivers, MRO, and nested definitions that signature grep
        # patterns can miss.
        lang_hints: List[str] = []
        extensions = {
            Path(f.path).suffix.lower() for f in pr_context.files if f.path
        }
        if ".java" in extensions:
            lang_hints.append(
                "**Java (`.java`)** — prefer `find_symbol(name)` over grep. "
                "The tree-sitter index enumerates classes, interfaces, "
                "methods, and fields (including overloads). For method "
                "calls with new argument shapes, inspect all overloads "
                "returned by `find_symbol` before flagging as missing — "
                "Java allows same-name methods with different parameter "
                "types. Only fall back to grep when `find_symbol` is "
                "empty AND the file isn't marked `extracted_via: regex`."
            )
        if ".py" in extensions:
            lang_hints.append(
                "**Python (`.py`)** — prefer `find_symbol(name)` over grep "
                "when verifying class methods, `__init__` parameters, or "
                "attributes. AST surfaces inherited methods via MRO and "
                "decorator-wrapped definitions that grep can miss. Grep "
                "on `class Name` / `def name` is acceptable only for "
                "top-level module symbols."
            )
        if ".go" in extensions:
            lang_hints.append(
                "**Go (`.go`)** — prefer `find_symbol(name)` over grep "
                "when checking method receivers (`func (r *R) Name`) or "
                "interface members. AST binds the method to its receiver "
                "type, which grep can't disambiguate across files. Grep "
                "on `func Name` / `type Name struct` is fine for free "
                "functions and simple types."
            )
        if extensions & {".ts", ".tsx", ".js", ".jsx"}:
            lang_hints.append(
                "**TypeScript / JavaScript (`.ts` / `.tsx` / `.js` / "
                "`.jsx`)** — prefer `find_symbol(name)` over grep. AST "
                "reliably picks up function overloads, interface members, "
                "class methods, and type aliases that grep conflates. For "
                "TS overloaded functions, inspect the full signature list "
                "returned by `find_symbol` before flagging a kwarg or "
                "param as missing."
            )

        hint_block = ""
        if lang_hints:
            hint_block = "\n\n## Language-specific hints\n\n" + "\n\n".join(lang_hints)

        # v2u reorder — STEP 1: run the deterministic P13 scanners FIRST.
        # These are mechanical (zero LLM cost, low tens-of-ms per file,
        # language-specific regex + grep) and cover import-level
        # existence comprehensively for Python / Go / Java. Running
        # them first lets us:
        #   (a) persist missing-symbol facts to the vault immediately,
        #       regardless of what the LLM worker does afterward,
        #   (b) tell the LLM worker what's already been checked so it
        #       can focus on the signature-level class of checks P13
        #       cannot do, and
        #   (c) guarantee coverage even if the LLM worker times out —
        #       which was observed to happen on virtually every
        #       sentry / grafana / keycloak case in v2t.
        from app.scratchpad import current_factstore

        store = current_factstore()
        missing_count = 0
        added_from_ast = 0
        p13_handled_names: set = set()
        p13_missing_details: List[Dict[str, str]] = []

        if store is not None:
            try:
                def _inject_phantom(found: Dict[str, str], *, kind: str) -> None:
                    nonlocal added_from_ast, missing_count
                    name = found["name"]
                    if name in p13_handled_names:
                        return
                    try:
                        store.put_existence(
                            symbol_name=name,
                            symbol_kind=kind,
                            referenced_at=found["referenced_at"],
                            exists=False,
                            evidence=found["evidence"],
                            signature_info=None,
                        )
                        p13_handled_names.add(name)
                        p13_missing_details.append({
                            "name": name, "kind": kind,
                            "referenced_at": found["referenced_at"],
                        })
                        added_from_ast += 1
                        missing_count += 1
                    except Exception as exc:
                        logger.debug(
                            "[PR Brain v2] P13 put_existence failed for %s: %s",
                            name, exc,
                        )

                for found in _scan_new_python_imports_for_missing(
                    self._workspace_path, file_diffs,
                ):
                    _inject_phantom(found, kind="import")

                for found in _scan_new_go_references_for_missing(
                    self._workspace_path, file_diffs,
                ):
                    _inject_phantom(found, kind="reference")

                for found in _scan_new_java_references_for_missing(
                    self._workspace_path, file_diffs,
                ):
                    _inject_phantom(found, kind="class")
            except Exception as exc:
                logger.warning(
                    "[PR Brain v2] P13 deterministic scan failed "
                    "(non-fatal): %s", exc,
                )

        if added_from_ast:
            logger.info(
                "[PR Brain v2] P13 deterministic scan (Python/Go/Java) "
                "flagged %d missing symbol(s) BEFORE LLM dispatch",
                added_from_ast,
            )

        # STEP 2: build the LLM worker's query with a "pre-verified"
        # block. The worker sees what P13 already caught so it can skip
        # those names and focus on signature-level checks.
        pre_verified_block = ""
        if p13_missing_details:
            pre_verified_block = (
                "\n\n## Pre-verified missing symbols (mechanical scan — DO NOT re-check)\n\n"
                "These symbols have already been identified by the "
                "deterministic pre-scanner as missing. They are already "
                "in the Fact Vault as `exists=false`. Ignore them in "
                "your analysis — do not waste tool calls re-verifying "
                "import-level existence for these names.\n\n"
                + "\n".join(
                    f"- `{d['name']}` (kind={d['kind']}, at `{d['referenced_at']}`)"
                    for d in p13_missing_details[:40]
                )
            )

        query = (
            "# PR existence verification (signature focus)\n\n"
            + task_text
            + pre_verified_block
            + hint_block
            + "\n\n## Files changed\n\n"
            + "\n".join(f"- `{f.path}` (+{f.additions} −{f.deletions})" for f in pr_context.files)
            + "\n\n## Diff\n\n"
            + "\n".join(diff_block)
        )

        params = {
            "template": "pr_existence_check",
            "query": query,
            "budget_weight": 0.5,
        }

        # STEP 3: dispatch LLM worker with the narrowed task + tight
        # 60s wall-clock. Task narrowing is the justification for the
        # shorter timeout — the worker no longer enumerates every
        # symbol, just the signature-level class.
        llm_symbols: List[Dict[str, Any]] = []
        llm_error: Optional[str] = None
        llm_timeout: bool = False

        try:
            result = await asyncio.wait_for(
                executor.execute("dispatch_agent", params),
                timeout=float(_PHASE2_TIMEOUT_SECONDS),
            )
        except TimeoutError:
            logger.warning(
                "[PR Brain v2] existence-check LLM worker hit %ds "
                "wall-clock timeout. P13 facts already persisted (%d "
                "missing symbols); coordinator proceeds with those.",
                _PHASE2_TIMEOUT_SECONDS, added_from_ast,
            )
            llm_timeout = True
            result = None

        if result is not None and not result.success:
            logger.warning(
                "[PR Brain v2] existence-check dispatch failed: %s", result.error,
            )
            llm_error = str(result.error)
            result = None

        if result is not None:
            condensed = result.data or {}
            raw_answer = (
                condensed.get("answer") or condensed.get("final_answer") or ""
            )
            parsed = _parse_existence_json(raw_answer)
            if parsed is None:
                logger.warning(
                    "[PR Brain v2] existence worker output did not parse as JSON",
                )
                llm_error = "parse_failed"
            else:
                llm_symbols = parsed.get("symbols", []) or []

        # STEP 4: persist LLM-contributed symbols, skipping any whose
        # name was already handled by P13 (P13 is deterministic truth
        # on its lane; LLM's signature-focus contributions are
        # additive, not overriding).
        if store is not None and llm_symbols:
            for sym in llm_symbols:
                if not isinstance(sym, dict):
                    continue
                name = sym.get("name") or ""
                if not name:
                    continue
                if name in p13_handled_names:
                    continue  # P13 wins — don't let the LLM overwrite
                exists = bool(sym.get("exists", True))
                if not exists:
                    missing_count += 1
                try:
                    store.put_existence(
                        symbol_name=name,
                        symbol_kind=(sym.get("kind") or "symbol")[:32],
                        referenced_at=(sym.get("referenced_at") or "")[:256],
                        exists=exists,
                        evidence=(sym.get("evidence") or "")[:1000],
                        signature_info=sym.get("signature_info"),
                    )
                except Exception as exc:
                    logger.debug("put_existence failed for %s: %s", name, exc)

        logger.info(
            "[PR Brain v2] Phase 2 existence: P13 flagged %d, LLM worker "
            "added %d more signature-level facts, total missing=%d "
            "(llm_timeout=%s, llm_error=%s)",
            added_from_ast, len(llm_symbols), missing_count,
            llm_timeout, llm_error,
        )
        yield WorkflowEvent(
            "v2_phase2_complete",
            {
                "phase": "existence_verification",
                "p13_missing": added_from_ast,
                "llm_symbols": len(llm_symbols),
                "missing": missing_count,
                "llm_timeout": llm_timeout,
                "llm_error": llm_error,
            },
        )

    def _format_existence_summary_for_coordinator(self) -> str:
        """Render a compact summary of existence_facts the coordinator can
        read inline in its user message. Empty string when no facts.

        Designed to be **hard to ignore** — the coordinator MUST emit each
        missing symbol as a finding in the exact shape shown, not
        speculate about logic in non-existent code.
        """
        from app.scratchpad import current_factstore

        store = current_factstore()
        if store is None:
            return ""

        missing: List = list(store.iter_existence(exists=False))
        present: List = list(store.iter_existence(exists=True))

        if not missing and not present:
            return ""

        lines: List[str] = []
        lines.append("## Phase 2 — Existence verification (AUTHORITATIVE)")
        lines.append("")

        if missing:
            lines.append("### ⚠️ Missing symbols — DIRECT FINDINGS REQUIRED")
            lines.append("")
            lines.append(
                f"The Phase 2 verifier grep/find_symbol'd every new "
                f"reference in this PR. **{len(missing)} symbol(s) are "
                f"NOT defined anywhere in the codebase.** The PR will "
                f"raise `ImportError` / `NameError` / `TypeError` at "
                f"runtime the moment affected code is loaded."
            )
            lines.append("")
            lines.append("**MANDATORY**: your final findings JSON MUST include one "
                         "entry per missing symbol, pointing at the REFERENCE "
                         "site (not where the symbol 'would' be defined), with "
                         "title of the form 'ImportError at runtime: {name} "
                         "not defined in codebase'. Severity = `critical`. "
                         "Category = `correctness`. Confidence = `0.99`.")
            lines.append("")
            lines.append("**DO NOT** speculate about what the non-existent "
                         "symbol 'would have done'. Do NOT emit findings "
                         "about negative offsets, null checks, or any logic "
                         "inside a phantom class. The class does not exist — "
                         "the ImportError IS the bug. Stop there.")
            lines.append("")
            lines.append("**Required finding template** (copy this shape — fill the brackets):")
            lines.append("")
            lines.append("```json")
            lines.append("{")
            lines.append('  "title": "ImportError at runtime: <SYMBOL> not defined in codebase",')
            lines.append('  "severity": "critical",')
            lines.append('  "confidence": 0.99,')
            lines.append('  "file": "<FILE where the reference is>",')
            lines.append('  "start_line": <LINE of the reference>,')
            lines.append('  "end_line": <LINE of the reference>,')
            lines.append('  "evidence": ["grep \'class <SYMBOL>\' / \'def <SYMBOL>\' returned 0 matches in the codebase"],')
            lines.append('  "risk": "Every call path that loads <FILE> raises ImportError/NameError at runtime.",')
            lines.append('  "suggested_fix": "Either define <SYMBOL> in the imported module, or remove the reference. The current PR is unshippable as written.",')
            lines.append('  "category": "correctness"')
            lines.append("}")
            lines.append("```")
            lines.append("")
            lines.append("**Missing symbols (one finding each — do not merge, do not skip):**")
            lines.append("")
            for m in missing:
                ref = m.referenced_at or "(unknown)"
                ev = (m.evidence or "").strip()[:200]
                lines.append(
                    f"- `{m.symbol_name}` ({m.symbol_kind}) referenced at `{ref}` — evidence: {ev}"
                )
            lines.append("")

        if present:
            sig_mismatch = [
                p for p in present
                if p.signature_info and p.signature_info.get("missing_params")
            ]
            if sig_mismatch:
                lines.append("### ⚠️ Signature mismatches — DIRECT FINDINGS REQUIRED")
                lines.append("")
                lines.append(
                    f"**{len(sig_mismatch)} method(s) exist but are called "
                    f"with parameter(s) they don't accept.** Runtime "
                    f"behaviour: `TypeError: unexpected keyword argument`. "
                    f"Emit one finding each using the same template shape "
                    f"above, but with title 'TypeError at runtime: "
                    f"{{method}}() does not accept {{kwarg}}'."
                )
                lines.append("")
                for m in sig_mismatch:
                    missing_params = m.signature_info.get("missing_params", [])
                    lines.append(
                        f"- `{m.symbol_name}` at `{m.referenced_at}` — "
                        f"missing params: {missing_params}"
                    )
                lines.append("")
            other_present = [p for p in present if p not in sig_mismatch]
            if other_present:
                lines.append(
                    f"**{len(other_present)} other symbol(s) verified present.** "
                    f"Use `search_facts(kind=\"existence\", symbol=\"X\")` to "
                    f"look up any of them; sub-agents you dispatch can "
                    f"skip the verify-existence-first step for these."
                )
                lines.append("")
        return "\n".join(lines)

    def _build_v2_coordinator_query(
        self,
        pr_context: PRContext,
        risk_profile: RiskProfile,
        file_diffs: Dict[str, str],
        impact_context: str,
        existence_summary: str = "",
    ) -> str:
        """Compose the user message for the v2 coordinator Brain.

        Includes: file list with +/- counts, risk profile summary, condensed
        impact context, and the diff itself (truncated per budget). The
        pr_brain_coordinator skill in the system prompt drives the loop.
        """
        lines: List[str] = []
        lines.append("# PR Review — coordinator task")
        lines.append("")
        lines.append(f"Diff spec: `{self._diff_spec}`")
        lines.append(f"Files changed: {pr_context.file_count}  "
                     f"Lines changed: {pr_context.total_changed_lines}")
        lines.append("")

        # ------------------------------------------------------------------
        # PR intent — the single most important seed for Plan phase.
        # Without this, the coordinator can only pattern-match on the diff;
        # with it, the coordinator can derive invariants to check.
        # ------------------------------------------------------------------
        pr_title = getattr(pr_context, "title", "") or ""
        pr_desc = getattr(pr_context, "description", "") or ""
        if pr_title or pr_desc:
            lines.append("## PR intent — what this PR CLAIMS to do")
            lines.append("")
            if pr_title:
                lines.append(f"**Title**: {pr_title}")
                lines.append("")
            if pr_desc:
                lines.append("**Description**:")
                lines.append("")
                lines.append(pr_desc.strip()[:1800])
                if len(pr_desc.strip()) > 1800:
                    lines.append("\n[...description truncated — fetch more with tools if needed...]")
                lines.append("")
            lines.append(
                "**Before planning investigations**: extract 3-5 concrete "
                "invariants from the intent above. Each invariant should be "
                "a falsifiable predicate of the shape 'After this PR, {X} "
                "must hold at {location/type}'. These invariants drive your "
                "dispatch_subagent check questions — every check should map "
                "to one invariant. If an invariant cannot be checked from "
                "the diff alone, grep / find_symbol first to find the "
                "relevant code."
            )
            lines.append("")
            lines.append(
                "**Intent check**: use the intent as context for your "
                "regular investigations. If a concrete code bug already "
                "captures the problem, emit ONE finding about that bug — "
                "do NOT also emit a separate 'intent mismatch' meta-finding "
                "covering the same defect. Only emit a standalone intent "
                "finding when the diff visibly fails to achieve the stated "
                "goal AND no concrete code-level bug explains the gap."
            )
            lines.append("")

        lines.append("## Files in diff")
        lines.append("")
        for f in pr_context.files:
            lines.append(
                f"- `{f.path}`  (+{f.additions} −{f.deletions}, "
                f"{f.status}, category={f.category.value})"
            )
        lines.append("")
        lines.append("## Risk profile")
        lines.append("")
        lines.append(f"- correctness: {risk_profile.correctness.value}")
        lines.append(f"- security: {risk_profile.security.value}")
        lines.append(f"- reliability: {risk_profile.reliability.value}")
        lines.append(f"- concurrency: {risk_profile.concurrency.value}")
        lines.append(f"- operational: {risk_profile.operational.value}")
        lines.append("")

        # Phase 2 output (existence facts) injected inline. Missing
        # symbols here are directly promotable findings — the coordinator
        # should NOT dispatch logic checks on them.
        if existence_summary:
            lines.append(existence_summary)
            lines.append("")

        # Impact context (condensed). Keep it bounded.
        if impact_context:
            lines.append("## Impact context (dependency graph + callers)")
            lines.append("")
            lines.append(impact_context[:8000])
            if len(impact_context) > 8000:
                lines.append("\n[...truncated, use tools to explore further...]")
            lines.append("")

        # File diffs — include but bound size. Full diffs are the primary
        # evidence; coordinator will read files directly for deeper cuts.
        lines.append("## Diff (per-file)")
        lines.append("")
        diff_budget = 30_000  # chars across all diffs
        remaining = diff_budget
        for path, diff_text in file_diffs.items():
            if remaining <= 0:
                lines.append("[...more diffs truncated, use git_diff tool to fetch...]")
                break
            slice_ = diff_text[: min(len(diff_text), remaining)]
            lines.append(f"### `{path}`")
            lines.append("```diff")
            lines.append(slice_)
            lines.append("```")
            lines.append("")
            remaining -= len(slice_)

        # Mandatory-dispatch injection. Path-anchored (Tier 1) +
        # content-anchored (Tier 2) rules that the coordinator CANNOT
        # decide to skip regardless of PR size or apparent complexity.
        # See ``_detect_required_dispatches``.
        required = _detect_required_dispatches(file_diffs)
        if required:
            lines.append("## MANDATORY investigations (Phase 1 detected)")
            lines.append("")
            lines.append(
                "**These dispatches are non-skippable** — Phase 1 "
                "detectors flagged files and/or `+` line content whose "
                "failure modes cannot be adequately assessed by survey "
                "alone. Your **first dispatches** must satisfy this "
                "list. Do not claim that 'the survey was sufficient' "
                "for items listed here; it is not. If you still "
                "genuinely believe a listed role is unnecessary for "
                "this specific PR, you must dispatch it anyway AND "
                "justify the skip in your Synthesize note — one-line "
                "per skipped role, citing a concrete reason tied to "
                "the diff content."
            )
            lines.append("")
            # De-dup: if BOTH tiers fire for the same role, we still
            # render each entry separately (the reasons differ — path-
            # anchored trigger vs content-anchored trigger — and seeing
            # both strengthens the coordinator's conviction to
            # dispatch). Group by role for readability.
            for req in required:
                tier = req.get("_tier", 1)
                tier_label = "Tier 1 — path" if tier == 1 else "Tier 2 — diff content"
                lines.append(
                    f"### `role=\"{req['role']}\"` — REQUIRED ({tier_label})"
                )
                lines.append("")
                lines.append(f"**Trigger reason**: {req['reason']}")
                lines.append("")
                if "matching_paths" in req:
                    lines.append("**Matching paths**:")
                    for p in req["matching_paths"]:
                        lines.append(f"- `{p}`")
                    lines.append("")
                elif "matching_evidence" in req:
                    lines.append("**Matching evidence** (file:line — why):")
                    for ev in req["matching_evidence"]:
                        snippet = ev["snippet"].replace("\n", " ")
                        lines.append(
                            f"- `{ev['file']}:{ev['line']}` — {ev['reason']} "
                            f"— `{snippet[:80]}`"
                        )
                    lines.append("")
            logger.info(
                "[PR Brain v2] Mandatory-dispatch Phase 1 detected %d "
                "required role(s) across 2 tiers: %s",
                len(required),
                ", ".join(
                    f"{r['role']}(T{r.get('_tier', 1)})" for r in required
                ),
            )

        # P12b — Dimension-worker trigger hints. OPT-IN, not mandatory.
        # These surface changed files whose cross-file caller footprint is
        # large enough that file-range dispatch would split the pattern.
        # The coordinator decides whether to actually fire
        # dispatch_dimension_worker; we just tell it "here's where
        # a cross-file sweep would pay off".
        dim_triggers = _detect_dimension_triggers(
            self._workspace_path, pr_context,
        )
        n_files_for_cap = len(pr_context.files)
        dim_cap = _dimension_dispatch_cap(n_files_for_cap)
        if dim_triggers and dim_cap > 0:
            lines.append("## Dimension-worker opportunities (P12b)")
            lines.append("")
            lines.append(
                f"Phase 1 spotted {len(dim_triggers)} changed file(s) with "
                f"a cross-file caller footprint that file-range dispatch "
                f"would split up. These are CANDIDATES for "
                f"`dispatch_dimension_worker` (not mandatory). "
                f"You may fire **up to {dim_cap} dimension worker(s)** "
                f"for this PR — reserve them for cases where a pattern "
                f"(new contract, signature change, shared middleware "
                f"edit) must be verified at every caller site, and a "
                f"bunch of narrow scoped dispatches would miss the "
                f"cross-cut. `model_tier=\"explorer\"` default @ 150K "
                f"budget; escalate to `model_tier=\"strong\"` only when "
                f"cross-file logical inference is required."
            )
            lines.append("")
            for trig in dim_triggers[:6]:
                lines.append(f"### `{trig['file']}`")
                lines.append("")
                lines.append(
                    f"- Caller files: {trig['caller_count']} distinct "
                    f"({', '.join(f'`{c}`' for c in trig['caller_files'][:5])}"
                    f"{'...' if len(trig['caller_files']) > 5 else ''})"
                )
                if trig['hotspot_symbols']:
                    lines.append(
                        "- Hotspot symbols: "
                        f"{', '.join(f'`{s}`' for s in trig['hotspot_symbols'][:5])}"
                    )
                lines.append("")
            logger.info(
                "[PR Brain v2] P12b dimension triggers: %d candidate file(s), "
                "cap=%d: %s",
                len(dim_triggers),
                dim_cap,
                ", ".join(t["file"] for t in dim_triggers[:5]),
            )

        # Dispatch cap scales with PR size (your skill covers the "why"
        # in the Plan section; here we give you the numeric cap). Caps
        # bumped in v2o to give multi-role-per-cluster (0-5 roles) real
        # room — a 4-cluster large PR with 2-3 roles per cluster easily
        # wants 10-14 dispatches.
        n_files = len(pr_context.files)
        if n_files < 5:
            dispatch_cap = 5
            size_label = "small"
        elif n_files < 15:
            dispatch_cap = 10
            size_label = "medium"
        else:
            dispatch_cap = 16
            size_label = "large"

        lines.append("## Dispatch budget for THIS PR")
        lines.append("")
        lines.append(
            f"- PR size: **{size_label}** ({n_files} files, "
            f"{pr_context.total_changed_lines} lines changed)"
        )
        lines.append(
            f"- Hard cap: **{dispatch_cap} dispatches** across all replan rounds"
        )
        if size_label == "large":
            lines.append(
                "- Cluster first: group files by feature/intent in Survey, "
                "then dispatch 1-2 role agents per cluster"
            )
        else:
            lines.append(
                "- Small PR: 1-3 targeted dispatches typically suffice. "
                "Don't pad."
            )
        lines.append("")

        lines.append("## Your task")
        lines.append("")
        lines.append(
            "Run your 5-phase coordinator loop (Survey → Plan → Execute → "
            "Replan → Synthesize). Use read-only tools for the Survey. "
            "Dispatch scope-bounded investigations via dispatch_subagent "
            f"(≤5 files per dispatch, ≤{dispatch_cap} total dispatches). "
            "Two dispatch modes available — pick per investigation: "
            "(a) `checks=[q1, q2, q3]` for localised suspicions where "
            "you have concrete yes/no questions; (b) `role=\"security\"|"
            "\"correctness\"|\"concurrency\"|\"reliability\"|\"performance\"|"
            "\"test_coverage\"` + `direction_hint=\"...\"` for specialist "
            "deep-dive on a risk dimension. You may combine: "
            "`role=\"security\", checks=[...]`. "
            "At Synthesize, classify severity yourself using the "
            "`## Severity rubric` section of your skill — reserve `critical` "
            "and `high` for their listed categories, default borderline "
            "findings to `medium`. Write `suggested_fix` in the concrete, "
            "location-bearing shape shown in the `## Suggested_fix` section."
        )
        lines.append("")
        lines.append("## Final output — MANDATORY SHAPE")
        lines.append("")
        lines.append(
            "Your final answer must be a JSON array of findings inside a "
            "```json fenced block. Each finding has these fields:"
        )
        lines.append("")
        lines.append("```json")
        lines.append("[")
        lines.append("  {")
        lines.append('    "title": "concise description",')
        lines.append('    "severity": "critical | high | medium | low | nit | praise",')
        lines.append('    "confidence": 0.0-1.0,')
        lines.append('    "file": "path/to/file.py",')
        lines.append('    "start_line": 120,')
        lines.append('    "end_line": 135,')
        lines.append('    "evidence": ["line quote", "cross-reference"],')
        lines.append('    "risk": "what could go wrong in production",')
        lines.append('    "suggested_fix": "concrete, implementable fix",')
        lines.append('    "category": "correctness | security | reliability | concurrency | performance | test_coverage"')
        lines.append("  }")
        lines.append("]")
        lines.append("```")
        lines.append("")
        lines.append(
            "**Always emit at least one finding.** A reviewer reading your "
            "output expects a signal per PR. If after honest investigation "
            "you do NOT see any correctness/security/reliability bugs, "
            "emit a single `praise` severity entry pointing at the primary "
            "change (or an `info` entry noting what you verified and why "
            "nothing rose above the bar). This keeps downstream tooling "
            "happy and gives the author confidence the review was "
            "substantive. Do NOT invent filler bugs — praise/info on a "
            "clean PR is honest and useful. After the JSON block you may "
            "add a short prose synthesis, but the JSON array is what "
            "downstream tooling parses — it must be present, valid, and "
            "non-empty."
        )
        return "\n".join(lines)

    async def _apply_v2_precision_filter(
        self,
        executor,
        review_output: Dict[str, Any],
        pr_context: PRContext,
        file_diffs: Dict[str, str],
    ) -> Dict[str, Any]:
        """3-band precision filter — adaptive verifier.

        Bands:
          * >= 0.8 : keep as final finding (no re-verification)
          * 0.5-0.8: verify via sub-agent (Haiku x N if count <= 2,
                      Sonnet batch if count >= 3). Verdict is terminal.
          * < 0.5  : demote to secondary_notes appended to synthesis.
        """
        findings = review_output.get("findings", [])

        # Step 0: dedup by (file, line±5). When two findings point at
        # (approximately) the same location, keep the one with highest
        # confidence. Deterministic tiebreak: critical > high > medium >
        # low > nit > praise.
        findings = _dedup_findings_by_location(findings)

        # Step 0b: mechanically enforce "one finding per missing symbol"
        # from Phase 2 existence verification. The coordinator skill
        # marks this MANDATORY, but LLM variance can drop or merge these.
        # Injecting synthetic findings here guarantees the review reports
        # every runtime error the diff introduces.
        findings, injected_count = _inject_missing_symbol_findings(findings)
        if injected_count:
            logger.info(
                "[PR Brain v2] Injected %d missing-symbol finding(s) "
                "that coordinator omitted",
                injected_count,
            )

        # Step 0b-2: P14 — inject findings for stub-function call sites
        # detected mechanically from the diff. For each (stub_def,
        # caller) pair found by _scan_for_stub_call_sites, if the
        # coordinator didn't already flag the site, synthesize a
        # finding. Guards against coordinator missing multi-site stub
        # bugs (grafana-009 class).
        findings, stub_injected = _inject_stub_caller_findings(
            findings, file_diffs,
        )
        if stub_injected:
            logger.info(
                "[PR Brain v2] P14 injected %d stub-call-site finding(s)",
                stub_injected,
            )

        # Step 0c: external-signal reflection (P8). Drop findings whose
        # premise contradicts Phase 2 existence facts (e.g. "X doesn't
        # exist" when Phase 2 confirmed exists=True). External signal >
        # intrinsic self-correction (+18.5pp in published research).
        findings, reflection_drops = _reflect_against_phase2_facts(findings)
        if reflection_drops:
            logger.info(
                "[PR Brain v2] Reflection pass dropped %d finding(s) "
                "whose premise contradicts Phase 2 facts",
                reflection_drops,
            )

        if not findings:
            return review_output

        direct: List[Dict[str, Any]] = []
        unclear: List[Dict[str, Any]] = []
        low: List[Dict[str, Any]] = []

        for f in findings:
            conf = float(f.get("confidence", 0) or 0)
            if conf >= 0.8:
                direct.append(f)
            elif conf >= 0.5:
                unclear.append(f)
            else:
                low.append(f)

        logger.info(
            "[PR Brain v2] Precision filter: direct=%d unclear=%d low=%d",
            len(direct), len(unclear), len(low),
        )

        confirmed_from_verifier: List[Dict[str, Any]] = []
        refuted_count = 0
        unclear_after_verify: List[Dict[str, Any]] = []

        # Phase 9.16 — build the verifier system prefix ONCE per
        # _apply_v2_precision_filter call. Skill text + PR context are
        # identical across every verifier invocation in this PR review,
        # so structuring them as the cache-stable prefix lets calls 2..N
        # hit the prompt cache (input cost ~10% of fresh).
        verifier_prefix = self._build_verifier_system_prefix(
            pr_context, file_diffs,
        )

        if unclear:
            if len(unclear) <= 2:
                # Fast tier per-finding (forked — no AgentLoopService overhead)
                for f in unclear:
                    verdict = await self._verify_single(f, file_diffs, verifier_prefix)
                    if verdict == "confirmed":
                        confirmed_from_verifier.append(f)
                    elif verdict == "refuted":
                        refuted_count += 1
                    else:
                        unclear_after_verify.append(f)
            else:
                # Strong tier batch (forked — same prefix amortized via cache)
                results = await self._verify_batch(unclear, file_diffs, verifier_prefix)
                for f, verdict in zip(unclear, results):
                    if verdict == "confirmed":
                        confirmed_from_verifier.append(f)
                    elif verdict == "refuted":
                        refuted_count += 1
                    else:
                        unclear_after_verify.append(f)

        logger.info(
            "[PR Brain v2] Verifier: confirmed=%d refuted=%d still_unclear=%d",
            len(confirmed_from_verifier), refuted_count, len(unclear_after_verify),
        )

        final_findings = direct + confirmed_from_verifier
        secondary = unclear_after_verify + low

        # Step 6: per-finding diff-scope verification (P11 cheap).
        # Inspired by UltraReview's "every finding independently verified".
        # Mechanical LLM-free check: a finding targeting a file outside
        # the PR diff is almost always a coordinator hallucination. Move
        # such findings to secondary_notes instead of emitting.
        final_findings, scope_demoted, scope_demoted_count = (
            _filter_findings_to_diff_scope(final_findings, file_diffs)
        )
        if scope_demoted_count:
            logger.info(
                "[PR Brain v2] Diff-scope filter demoted %d finding(s) "
                "whose file is not in the PR diff",
                scope_demoted_count,
            )
            secondary = scope_demoted + secondary

        # Append secondary notes to synthesis as a "Secondary observations"
        # block. They don't enter the findings array → don't count against
        # precision / recall in the eval scorer.
        synthesis = review_output.get("synthesis", "")
        if secondary:
            secondary_block_lines = [
                "",
                "---",
                "",
                "## Secondary observations (not scored, low-confidence or "
                "unverified)",
                "",
            ]
            for s in secondary:
                title = s.get("title", "(untitled)")
                file_ = s.get("file", "")
                line = s.get("start_line", "")
                conf = s.get("confidence", "")
                secondary_block_lines.append(
                    f"- **{title}** — `{file_}:{line}` (conf={conf})"
                )
            synthesis = synthesis + "\n".join(secondary_block_lines)

        return {
            **review_output,
            "findings": final_findings,
            "synthesis": synthesis,
            "_precision_filter_stats": {
                "direct_findings": len(direct),
                "unclear_input": len(unclear),
                "confirmed_by_verifier": len(confirmed_from_verifier),
                "refuted_by_verifier": refuted_count,
                "still_unclear": len(unclear_after_verify),
                "low_confidence": len(low),
                "reflection_dropped": reflection_drops,
                "diff_scope_demoted": scope_demoted_count,
            },
        }

    def _build_verifier_system_prefix(
        self, pr_context: PRContext, file_diffs: Dict[str, str],
    ) -> str:
        """Phase 9.16 — assemble the verifier's static system prefix.

        Same content for every verifier invocation in this PR review.
        Structured so calls 2..N hit the provider's prompt cache:

            [pr_verification_check skill]      ← from INVESTIGATION_SKILLS
            [PR title + description]           ← stable per-PR
            [PR diff text]                     ← stable per-PR (≤30K chars)

        The user message (per-finding details) is the only varying part
        across verifier calls.
        """
        from app.agent_loop.forked import build_pr_context_prefix
        from app.agent_loop.prompts import INVESTIGATION_SKILLS

        skill_text = INVESTIGATION_SKILLS.get("pr_verification_check", "")
        # Render the same per-file ```diff blocks the coordinator already uses
        # — keeps the cache-key shape identical across verifier and coordinator
        # calls within the session (free cache hits).
        diff_block_lines: List[str] = []
        for path, diff_text in file_diffs.items():
            diff_block_lines.append(f"### `{path}`\n```diff\n{diff_text}\n```")
        diff_text = "\n\n".join(diff_block_lines)

        ctx_prefix = build_pr_context_prefix(
            pr_title=self._pr_title,
            pr_description=self._pr_description,
            file_diffs_text=diff_text,
        )
        return f"{skill_text}\n\n{ctx_prefix}".strip()

    async def _verify_single(
        self, finding: Dict[str, Any], file_diffs: Dict[str, str],
        system_prefix: str,
    ) -> str:
        """Phase 9.16 forked verifier — single finding via fork_call.

        Uses the explorer-tier provider (fast). Returns verdict string:
        'confirmed' | 'refuted' | 'unclear' (the latter on any failure).
        """
        from app.agent_loop.forked import fork_call

        title = finding.get("title", "")
        file_ = finding.get("file", "")
        start = finding.get("start_line", 0)
        end = finding.get("end_line", 0)
        evidence_hint = finding.get("evidence") or []
        if isinstance(evidence_hint, list):
            evidence_hint = "; ".join(str(e) for e in evidence_hint[:3])

        user_message = (
            f"# Verify this single finding\n\n"
            f"**Title**: {title}\n"
            f"**File**: {file_}\n"
            f"**Lines**: {start}-{end}\n"
            f"**Original confidence**: {finding.get('confidence', 0)}\n"
            f"**Agent's evidence claim**: {evidence_hint}\n\n"
            f"Return the JSON verdict from your system prompt."
        )

        raw = await fork_call(
            provider=self._explorer_provider,
            system_prompt=system_prefix,
            user_message=user_message,
            max_tokens=600,
            label=f"verify_single:{file_}:{start}",
        )
        if not raw:
            return "unclear"
        return _extract_single_verdict(raw)

    async def _verify_batch(
        self, unclear: List[Dict[str, Any]], file_diffs: Dict[str, str],
        system_prefix: str,
    ) -> List[str]:
        """Phase 9.16 forked verifier — N>=3 findings via fork_call.

        Uses the strong-tier provider (more capacity for cross-finding
        reasoning). Returns one verdict per input finding, same order.

        The PR diff is in the cached system_prefix already, so the
        per-call user message only carries the findings list — no need
        to re-include diff snippets here. That cuts ~10K tokens off
        the per-call cost AND lets the cache prefix stay stable.
        """
        from app.agent_loop.forked import fork_call

        findings_block_lines: List[str] = []
        for i, f in enumerate(unclear):
            title = f.get("title", "")
            file_ = f.get("file", "")
            start = f.get("start_line", 0)
            end = f.get("end_line", 0)
            conf = f.get("confidence", 0)
            ev_raw = f.get("evidence") or []
            if isinstance(ev_raw, list):
                ev_raw = "; ".join(str(e) for e in ev_raw[:3])
            findings_block_lines.append(
                f"### Finding [{i}]\n"
                f"- Title: {title}\n"
                f"- File: {file_}:{start}-{end}\n"
                f"- Original confidence: {conf}\n"
                f"- Agent's evidence claim: {ev_raw}\n"
            )

        user_message = (
            "# Verify these findings in batch\n\n"
            "For each finding, return confirmed|refuted|unclear with "
            "file:line evidence (the PR diff is in your system context). "
            "Cross-reference allowed.\n\n"
            + "\n".join(findings_block_lines)
            + "\n\nReturn the JSON verdicts object from your system prompt."
        )

        raw = await fork_call(
            provider=self._provider,  # strong tier for batch
            system_prompt=system_prefix,
            user_message=user_message,
            max_tokens=2000,
            label=f"verify_batch:{len(unclear)}",
        )
        if not raw:
            return ["unclear"] * len(unclear)
        return _extract_batch_verdicts(raw, expected_count=len(unclear))

    def _parse_v2_coordinator_output(
        self,
        coordinator_result,
        pr_context: PRContext,
    ) -> Dict[str, Any]:
        """Extract findings + merge recommendation from the v2 coordinator's
        final Markdown answer.

        Uses the existing ``parse_findings`` + ``merge_recommendation``
        helpers from ``code_review.shared`` so the output shape matches
        v1's. If the coordinator's answer can't be parsed, falls back to
        returning the raw answer as synthesis with zero findings — the
        agent still produced SOMETHING, no reason to hide it.
        """
        from app.code_review.shared import (
            merge_recommendation as _merge_rec,
        )
        from app.code_review.shared import (
            parse_findings as _parse_findings,
        )

        default = {
            "findings": [],
            "synthesis": "",
            "merge_recommendation": "comment",
        }

        if not coordinator_result.success:
            err = getattr(coordinator_result, "error", "unknown error")
            default["synthesis"] = (
                f"PR Brain v2 coordinator failed: {err}"
            )
            return default

        data = coordinator_result.data
        if not isinstance(data, dict):
            return default

        raw_answer = data.get("answer") or data.get("final_answer") or ""

        from app.code_review.models import FindingCategory as _FC

        try:
            # parse_findings accepts a default category and will override per
            # finding when the LLM included a "Category:" marker in its block.
            review_findings = _parse_findings(
                raw_answer,
                agent_name="pr_brain_v2",
                category=_FC.CORRECTNESS,
                warn_on_empty=False,
            )
        except Exception as exc:
            logger.warning(
                "[PR Brain v2] Failed to parse coordinator output: %s. "
                "Returning raw answer as synthesis with 0 findings.",
                exc,
            )
            return {
                "findings": [],
                "synthesis": raw_answer or default["synthesis"],
                "merge_recommendation": "comment",
            }

        try:
            merge_rec = _merge_rec(review_findings)
        except Exception:
            merge_rec = "comment"

        findings_dicts = [_finding_to_dict(f) for f in review_findings]
        return {
            "findings": findings_dicts,
            "synthesis": raw_answer,
            "merge_recommendation": merge_rec or "comment",
        }

    def cleanup(self) -> None:
        """Close and delete the session-owned Fact Vault, if any.

        Must be called once the orchestrator is done (success OR failure).
        Callers that passed a vault via ``scratchpad=`` keep ownership —
        we only delete what we created ourselves. Safe to call multiple
        times; second call is a no-op.

        Also resets the ContextVar binding so ``search_facts`` in any
        other concurrent task stops pointing at our (now-deleted) DB.

        Phase 9.17 — fires the ``on_task_end`` lifecycle hook BEFORE
        deleting the scratchpad so consumers (telemetry exporters,
        consolidation extractors) can read the vault one last time.
        """
        # Phase 9.17 — fire on_task_end first so hooks can still read
        # scratchpad state. Hooks are fire-and-forget; failures don't
        # block cleanup.
        fire_hook(
            "on_task_end",
            orchestrator=self,
            data={
                "scratchpad_owned": self._owns_scratchpad,
                "scratchpad_present": self._scratchpad is not None,
            },
        )

        # Reset the ContextVar binding regardless of ownership — if we
        # set it, we reset it, so concurrent search_facts calls won't hit
        # a deleted store.
        if self._scratchpad_ctx_token is not None:
            try:
                from app.scratchpad.context import _current_store

                _current_store.reset(self._scratchpad_ctx_token)
            except (LookupError, ValueError) as e:
                # Token already reset or context mismatch; safe to ignore.
                logger.debug("Scratchpad ContextVar reset skipped: %s", e)
            self._scratchpad_ctx_token = None

        if not self._owns_scratchpad or self._scratchpad is None:
            return
        try:
            stats = self._scratchpad.stats()
            exec_stats = getattr(self._tool_executor, "stats", None)
            # WARNING level so the line lands in default-level loggers
            # (root level is WARNING). One emit per PR review — low noise,
            # high signal: hits / misses / range_hits / negative_hits /
            # skipped from CachedToolExecutor + facts/negative_facts/
            # skip_facts counts from FactStore. Critical observability for
            # the eval harness.
            logger.warning(
                "Scratchpad close: session=%s stats=%s cache_perf=%s",
                self._scratchpad.session_id,
                stats,
                exec_stats,
            )
            self._scratchpad.delete()
        except Exception as e:
            logger.warning("Scratchpad cleanup failed: %s", e)
        self._scratchpad = None
        self._owns_scratchpad = False


_SEVERITY_RANK = {
    "critical": 5, "high": 4, "medium": 3,
    "low": 2, "nit": 1, "praise": 0,
}


def _dedup_findings_by_location(findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge findings pointing at the same (file, line±5) range.

    Keeps the finding with the highest (severity_rank, confidence) tuple.
    This catches the "coordinator produces the concrete bug finding PLUS
    a meta-finding about it" duplication observed on requests-012.
    """
    if not findings:
        return findings

    keep: List[Dict[str, Any]] = []

    def _better(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
        sev_a = _SEVERITY_RANK.get(str(a.get("severity", "low")).lower(), 1)
        sev_b = _SEVERITY_RANK.get(str(b.get("severity", "low")).lower(), 1)
        if sev_a != sev_b:
            return sev_a > sev_b
        return float(a.get("confidence", 0) or 0) > float(b.get("confidence", 0) or 0)

    for f in findings:
        file_ = f.get("file", "") or ""
        start = int(f.get("start_line", 0) or 0)
        end = int(f.get("end_line", 0) or start or 0)

        merged = False
        for i, existing in enumerate(keep):
            ef = existing.get("file", "") or ""
            if ef != file_:
                continue
            es = int(existing.get("start_line", 0) or 0)
            ee = int(existing.get("end_line", 0) or es or 0)
            # Overlap or adjacency within 5 lines
            if start <= ee + 5 and end >= es - 5:
                # Same region — keep the stronger one
                if _better(f, existing):
                    keep[i] = f
                merged = True
                break
        if not merged:
            keep.append(f)

    return keep


def _finding_covers_symbol(
    finding: Dict[str, Any], symbol_name: str, reference_file: str,
) -> bool:
    """True if ``finding`` already reports the missing-symbol bug for
    ``symbol_name``. Matching rules — ANY one is enough:
      * title contains the symbol name (case-sensitive: class/method
        names are meaningful identifiers)
      * any evidence entry mentions the symbol name
      * the finding's file matches the reference site AND the title
        signals a runtime error (ImportError/NameError/TypeError/
        undefined/not defined)
    """
    if not symbol_name:
        return True  # nothing to enforce

    title = str(finding.get("title", "") or "")
    if symbol_name in title:
        return True

    evidence = finding.get("evidence") or []
    if isinstance(evidence, list):
        for e in evidence:
            if symbol_name in str(e):
                return True
    elif isinstance(evidence, str) and symbol_name in evidence:
        return True

    # Fallback: same file + runtime-error title phrasing.
    f_file = str(finding.get("file", "") or "")
    ref_file = reference_file.split(":", 1)[0] if reference_file else ""
    if f_file and ref_file and f_file == ref_file:
        lowered = title.lower()
        for marker in (
            "importerror", "nameerror", "typeerror",
            "undefined", "not defined", "does not exist",
            "missing symbol",
        ):
            if marker in lowered:
                return True
    return False


def _parse_reference_location(ref: str) -> tuple[str, int]:
    """Split ``"path/to/file.py:42"`` → ``("path/to/file.py", 42)``.
    Falls back to ``(ref, 0)`` when no colon or unparsable line number."""
    if not ref:
        return ("", 0)
    if ":" not in ref:
        return (ref, 0)
    path, _, tail = ref.rpartition(":")
    try:
        return (path, int(tail.strip()))
    except (ValueError, TypeError):
        return (ref, 0)


def _inject_missing_symbol_findings(
    findings: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], int]:
    """Ensure every Phase-2 missing symbol AND signature mismatch has a
    finding in the review.

    Two classes of enforcement:
      * ``exists=False`` — symbol referenced but never defined anywhere.
        Synthesize an ImportError/NameError finding at the reference site.
      * ``exists=True`` with ``signature_info.missing_params`` — method
        is called with kwargs it doesn't accept. Synthesize a TypeError
        finding at the call site.

    Returns ``(findings_with_injections, injected_count)``. Safe to call
    when no FactStore is active — returns the input unchanged.
    """
    from app.scratchpad import current_factstore

    store = current_factstore()
    if store is None:
        return (findings, 0)

    try:
        missing = list(store.iter_existence(exists=False))
        present = list(store.iter_existence(exists=True))
    except Exception as exc:
        logger.warning(
            "[PR Brain v2] missing-symbol post-pass skipped — "
            "iter_existence failed: %s", exc,
        )
        return (findings, 0)

    sig_mismatches = [
        p for p in present
        if p.signature_info
        and p.signature_info.get("missing_params")
    ]

    if not missing and not sig_mismatches:
        return (findings, 0)

    injected = 0
    result = list(findings)

    for m in missing:
        if any(
            _finding_covers_symbol(f, m.symbol_name, m.referenced_at or "")
            for f in result
        ):
            continue
        ref_file, ref_line = _parse_reference_location(m.referenced_at or "")
        evidence_detail = (m.evidence or "").strip()[:300]
        synthetic = {
            "title": (
                f"ImportError at runtime: {m.symbol_name} "
                f"not defined in codebase"
            ),
            "severity": "critical",
            "confidence": 0.99,
            "file": ref_file,
            "start_line": ref_line,
            "end_line": ref_line,
            "evidence": [
                f"Phase 2 verifier: no definition found for `{m.symbol_name}` "
                f"({m.symbol_kind}) anywhere in the workspace.",
                evidence_detail or "grep/find_symbol returned 0 matches.",
            ],
            "risk": (
                f"Every call path that loads `{ref_file}` raises "
                f"ImportError/NameError at runtime — the PR is unshippable "
                f"as-is."
            ),
            "suggested_fix": (
                f"Either define `{m.symbol_name}` in the imported module, "
                f"or remove the reference at {m.referenced_at or ref_file}."
            ),
            "category": "correctness",
            "_injected_from": "phase2_existence_missing",
        }
        result.append(synthetic)
        injected += 1

    for p in sig_mismatches:
        bad_params = p.signature_info.get("missing_params") or []
        if not bad_params:
            continue
        bad_list = [str(bp) for bp in bad_params]
        # Check each bad-param name against existing findings — skip if
        # any kwarg is already covered.
        if any(
            any(_finding_covers_symbol(f, bp, p.referenced_at or "")
                for f in result)
            for bp in bad_list
        ):
            continue
        ref_file, ref_line = _parse_reference_location(p.referenced_at or "")
        accepted = p.signature_info.get("actual_params") or []
        synthetic = {
            "title": (
                f"TypeError at runtime: {p.symbol_name}() does not accept "
                f"{', '.join(bad_list)}"
            ),
            "severity": "high",
            "confidence": 0.97,
            "file": ref_file,
            "start_line": ref_line,
            "end_line": ref_line,
            "evidence": [
                f"Phase 2 verifier: `{p.symbol_name}` signature accepts "
                f"{accepted}; this call passes {bad_list} which are not in "
                f"the signature.",
            ],
            "risk": (
                f"Every invocation raises `TypeError: unexpected keyword "
                f"argument '{bad_list[0]}'` at runtime."
            ),
            "suggested_fix": (
                f"Either extend `{p.symbol_name}`'s signature to accept "
                f"{bad_list}, or drop the unsupported kwarg(s) from the "
                f"call at {p.referenced_at or ref_file}."
            ),
            "category": "correctness",
            "_injected_from": "phase2_existence_sigmismatch",
        }
        result.append(synthetic)
        injected += 1

    return (result, injected)


_PYTHON_FROM_IMPORT_RE = re.compile(
    r"^\+\s*from\s+([.\w]+)\s+import\s+(.+?)\s*$",
)
_PYTHON_BARE_IMPORT_RE = re.compile(
    r"^\+\s*import\s+([\w.]+)(?:\s+as\s+(\w+))?\s*$",
)
_DIFF_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)")


def _scan_new_python_imports_for_missing(
    workspace_path: str,
    file_diffs: Dict[str, str],
    *,
    max_symbols_checked: int = 24,
    grep_timeout_s: float = 8.0,
) -> List[Dict[str, str]]:
    """P13 — Deterministic Python import verifier.

    Scans each Python file's unified diff for newly added imports
    (``+from X import Y`` or ``+import X``) and verifies each imported
    name is defined somewhere in the workspace via a mechanical grep
    for ``class Y`` / ``def Y`` / ``Y = ...``. Returns the list of
    UNDEFINED names, each as ``{"name", "referenced_at", "evidence"}``.

    This is a safety net against the LLM Phase 2 worker missing a
    phantom symbol. Runs always; cheap; Python-only.

    Guards:
      * caps at ``max_symbols_checked`` greps per PR to bound runtime on
        large diffs
      * ``grep_timeout_s`` on each subprocess (so a giant repo cannot
        wedge the review)
      * skips wildcard (``*``), relative (``from .foo import``), and
        framework (``os/re/typing/logging/django/...``) imports
      * fails soft — any exception just returns current findings
    """
    if not workspace_path or not file_diffs:
        return []

    found: List[Dict[str, str]] = []
    checked = 0
    seen_names: set = set()

    for file_path, diff_text in file_diffs.items():
        if not file_path.endswith(".py"):
            continue
        current_new_line = 0
        for raw in diff_text.splitlines():
            if raw.startswith("@@"):
                m = _DIFF_HUNK_HEADER_RE.match(raw)
                if m:
                    current_new_line = int(m.group(1))
                continue
            if raw.startswith("---") or raw.startswith("+++"):
                continue
            is_addition = raw.startswith("+")
            if is_addition:
                from_match = _PYTHON_FROM_IMPORT_RE.match(raw)
                if from_match:
                    module = from_match.group(1)
                    if module.startswith("."):
                        # Relative imports — skip (would need file path
                        # resolution; rare phantom-bug source).
                        pass
                    elif _is_framework_module(module):
                        pass
                    elif not _module_is_first_party(workspace_path, module):
                        # Module doesn't resolve to a file in the workspace
                        # — it's an external package (e.g. `arroyo`, `kombu`).
                        # We can't verify external symbols via workspace grep
                        # without false positives. Skip.
                        pass
                    else:
                        names_chunk = from_match.group(2)
                        for name in _split_import_names(names_chunk):
                            if name in seen_names:
                                continue
                            if checked >= max_symbols_checked:
                                break
                            seen_names.add(name)
                            checked += 1
                            if _python_symbol_defined_anywhere(
                                workspace_path, name,
                                timeout_s=grep_timeout_s,
                            ):
                                continue
                            found.append({
                                "name": name,
                                "referenced_at": (
                                    f"{file_path}:{current_new_line}"
                                ),
                                "evidence": (
                                    f"Deterministic grep for `class {name}`, "
                                    f"`def {name}`, `{name} =` in "
                                    f"`*.py` → 0 matches. Import `from "
                                    f"{module} import {name}` will raise "
                                    f"ImportError at runtime."
                                ),
                            })
                else:
                    bare_match = _PYTHON_BARE_IMPORT_RE.match(raw)
                    if bare_match:
                        module = bare_match.group(1)
                        if (
                            not module.startswith(".")
                            and not _is_framework_module(module)
                        ):
                            # For `import X.Y`, we check if the root module
                            # X has any .py file. Skip for now — bare
                            # imports rarely produce phantom-symbol bugs.
                            pass
            # advance new-line counter for + and context (unchanged)
            if not raw.startswith("-"):
                current_new_line += 1
            if checked >= max_symbols_checked:
                break
        if checked >= max_symbols_checked:
            break
    return found


_FRAMEWORK_MODULE_PREFIXES = (
    "os", "sys", "re", "json", "typing", "logging", "abc", "collections",
    "contextlib", "dataclasses", "enum", "functools", "io", "itertools",
    "math", "pathlib", "random", "subprocess", "time", "unittest",
    "warnings", "asyncio", "concurrent", "datetime", "decimal",
    "django", "flask", "rest_framework", "pydantic", "sqlalchemy",
    "requests", "urllib3", "numpy", "pandas", "pytest", "mypy",
    "starlette", "fastapi", "click", "boto3", "botocore", "sentry_sdk",
)


def _is_framework_module(module: str) -> bool:
    """True if ``module`` is the stdlib or a well-known third-party that
    we never want to verify existence for."""
    if not module:
        return True
    root = module.split(".", 1)[0]
    return root in _FRAMEWORK_MODULE_PREFIXES


def _module_is_first_party(workspace_path: str, module: str) -> bool:
    """True when ``module`` resolves to a file inside the workspace.

    Principled complement to ``_is_framework_module`` — instead of an
    ever-growing blacklist of third-party libraries, we check whether
    the module's expected file-system path exists in the workspace.
    If not, it's an external package (installed via pip) and we should
    not flag its imports as missing — P13 has no way to verify external
    package symbols via workspace grep anyway.

    Checks for both layouts:
      * ``module/path/to/X.py``
      * ``module/path/to/X/__init__.py``

    Returns False on any error / missing workspace (fail-safe: skip).
    """
    import os as _os
    if not workspace_path or not module or module.startswith("."):
        return False
    try:
        candidate = module.replace(".", "/")
        for suffix in (".py", "/__init__.py"):
            if _os.path.exists(_os.path.join(workspace_path, candidate + suffix)):
                return True
        # Also try under common repo layouts: src/<module>, backend/<module>
        for root_prefix in ("src", "backend", "lib"):
            for suffix in (".py", "/__init__.py"):
                if _os.path.exists(
                    _os.path.join(workspace_path, root_prefix, candidate + suffix)
                ):
                    return True
    except Exception:
        return False
    return False


def _split_import_names(names_chunk: str) -> List[str]:
    """Parse the comma-separated tail of a ``from X import ...`` line.

    Handles parentheses, trailing commas, and ``as`` aliases. Filters
    wildcards and keeps only valid Python identifiers."""
    cleaned = names_chunk.strip().strip("()").rstrip(",")
    parts = [p.strip() for p in cleaned.split(",") if p.strip()]
    out: List[str] = []
    for p in parts:
        # Drop the "as alias" portion; we want the imported name.
        primary = p.split(" as ", 1)[0].strip()
        if primary == "*" or not primary.isidentifier():
            continue
        out.append(primary)
    return out


def _python_symbol_defined_anywhere(
    workspace_path: str, name: str, *, timeout_s: float = 8.0,
) -> bool:
    """Grep the workspace for a Python definition of ``name``.

    Matches ``class name``, ``def name``, or ``name = ...`` at line
    start (with optional leading whitespace). Returns True on first
    match; False on zero matches; True on error (fail-safe — never
    report a symbol missing we couldn't verify)."""
    import subprocess

    # Anchor definitions at line start + optional indent only. This
    # avoids matching ``from X import name`` or ``foo(name=...)``.
    # Using extended regex: ^\s*(class|def)\s+name\b  OR
    # ^\s*name\s*=
    pattern = (
        rf"^\s*(class|def)\s+{re.escape(name)}\b|"
        rf"^\s*{re.escape(name)}\s*="
    )
    try:
        r = subprocess.run(
            [
                "grep", "-r", "-E", pattern, workspace_path,
                "--include=*.py", "--max-count=1", "-l",
                "--exclude-dir=.git", "--exclude-dir=.venv",
                "--exclude-dir=node_modules", "--exclude-dir=__pycache__",
            ],
            capture_output=True, text=True, timeout=timeout_s,
        )
        # exit 0 = found ≥1 match; exit 1 = no match; exit 2 = error
        if r.returncode == 0 and r.stdout.strip():
            return True
        # exit 1 = no match → symbol missing
        # any other non-zero = grep error → fail-safe "True" (don't flag)
        return r.returncode != 1
    except Exception:
        return True


# ---------------------------------------------------------------------------
# P13-Go — deterministic bare-identifier phantom detector for Go.
# ---------------------------------------------------------------------------
# Targets the "call to undefined function in same package" class of bug
# — a `go build` compile error the LLM worker routinely misses because
# the identifier name LOOKS plausible (e.g. `endpointQueryData`). This
# scanner reads the diff, extracts every bare call (no dot prefix) in
# newly-added lines, and greps the package directory for a definition.
# Zero matches → phantom; inject with severity=critical, conf=0.99.
#
# Scope-out by design (to avoid false positives):
#   * `pkg.Foo(...)` — requires import resolution; MVP skips
#   * `obj.Method(...)` — requires type inference; MVP skips
#   * Files using dot-imports (`import . "..."`) — can't disambiguate
# ---------------------------------------------------------------------------

# Matches a bare call `name(` where `name` is not preceded by `.`, not
# a function declaration, not a type keyword. Lookbehind for `.` is
# emulated via a character-class preceding context filter.
_GO_BARE_CALL_RE = re.compile(
    r"""
    (?:^|[\s=,;\[\]{}(+\-*/&|<>!])   # allowed preceding char (no `.`)
    (?!func\s+)(?!type\s+)            # not a decl keyword directly
    ([A-Za-z_][A-Za-z0-9_]*)          # the identifier
    \s*\(                             # followed by (
    """,
    re.VERBOSE,
)
# Bare identifier at an argument position: `(name,` / `,name,` / `,name)`.
# Captures identifiers passed as arguments that look substantial enough
# to be package-level constants/vars/functions (>=6 chars OR camelCase).
# Filters out obvious locals like `ctx`, `req`, `err`.
_GO_BARE_ARG_RE = re.compile(
    r"""
    (?:\(|,)\s*                       # preceded by ( or , + ws
    ([A-Za-z_][A-Za-z0-9_]*)          # the identifier
    \s*(?=,|\))                       # followed by , or ) — arg position
    """,
    re.VERBOSE,
)
# Dot-import marker. Any file containing this is skipped.
_GO_DOT_IMPORT_RE = re.compile(r'^\s*(?:import\s+)?\.\s+"[^"]+"\s*$', re.MULTILINE)
# Same-line `func name(` declaration — used to drop self-matches where
# the bare-call regex would fire on the function header itself.
_GO_FUNC_DECL_RE = re.compile(
    r"^\s*(?:func\s+(?:\(\s*\w+\s+\*?\w+(?:\[[^\]]*\])?\s*\)\s+)?)(\w+)\s*\(",
)
# Substantive identifier heuristic: >=6 chars, OR contains mixed case
# (camelCase), OR contains underscore (snake_case). Filters out short
# generic locals like `ctx`, `req`, `err`, `res`, `i`, `n`, `x`.
_GO_SUBSTANTIVE_IDENT_RE = re.compile(
    r"^(?:"
    r"[A-Za-z_][A-Za-z0-9_]{5,}|"      # >= 6 chars
    r"[a-z][a-z0-9]*[A-Z][A-Za-z0-9_]*|"  # camelCase
    r"[A-Z][A-Za-z0-9]*[_A-Z][A-Z_]*|"   # UPPER_SNAKE
    r"[A-Za-z]+_[A-Za-z_]+"              # snake_case (non-leading _)
    r")$"
)
# Matches `func (recv *T) Name(` or `func Name(` and captures the
# receiver name (group 1) and the func name (group 2).
_GO_FUNC_SIG_START_RE = re.compile(
    r"^\s*func\s+(?:\(\s*(\w+)\s+\*?[\w.\[\]]+(?:\[[^\]]*\])?\s*\)\s+)?(\w+)\s*\(",
)
# Matches a line that looks like a method signature: `Name(args) ret?`.
# Supports: interface method decls (`Foo(x int) string`), function type
# decls, and any "name + paren + optional return" shape-only lines.
# CALLS are distinguished by their args lacking typed params.
_GO_METHOD_SIG_RE = re.compile(
    r"""
    ^\s*
    \w+\s*                               # method name
    \(                                   # open paren
    [^)]*                                # params (no nested parens — MVP)
    \)                                   # close paren
    \s*
    (?:                                  # optional return type
        \([^)]*\)                        #   multi-return `(T1, T2)`
        |
        [\w*.<>\[\],\s]+                 #   single return type
    )?
    \s*$
    """,
    re.VERBOSE,
)
# Typed parameter pattern inside parens: `name Type` (lowercase-start
# identifier followed by whitespace followed by a type token). If
# present, the parens contain typed params → signature. If absent,
# the parens contain values/expressions → call.
_GO_TYPED_PARAM_RE = re.compile(
    r"[a-z_]\w*\s+(?:\*|\[\]|\.\.\.)*[\w.]"
)

# Go keywords + built-in identifiers + universe block. Covers every
# identifier a Go file can reference without a definition in user code.
_GO_BUILTINS: set[str] = {
    # keywords
    "break", "case", "chan", "const", "continue", "default", "defer",
    "else", "fallthrough", "for", "func", "go", "goto", "if", "import",
    "interface", "map", "package", "range", "return", "select",
    "struct", "switch", "type", "var",
    # pre-declared types
    "bool", "byte", "complex64", "complex128", "error", "float32",
    "float64", "int", "int8", "int16", "int32", "int64", "rune",
    "string", "uint", "uint8", "uint16", "uint32", "uint64", "uintptr",
    "any", "comparable",
    # pre-declared values
    "true", "false", "iota", "nil",
    # built-in functions
    "append", "cap", "clear", "close", "complex", "copy", "delete",
    "imag", "len", "make", "max", "min", "new", "panic", "print",
    "println", "real", "recover",
}


def _go_dir_has_dot_import(file_path: str, workspace_path: str) -> bool:
    """True if the diff file uses dot-imports (skip entire file when so)."""
    import os as _os
    full = _os.path.join(workspace_path, file_path)
    try:
        with open(full, encoding="utf-8", errors="replace") as f:
            return bool(_GO_DOT_IMPORT_RE.search(f.read(16384)))
    except Exception:
        return True  # fail-safe: if we can't read, skip scanning


def _go_symbol_defined_anywhere(
    workspace_path: str, name: str, *, timeout_s: float = 8.0,
) -> bool:
    """Workspace-wide grep for ANY Go top-level definition of `name`,
    OR for `name` appearing as a function parameter anywhere.

    Matches:
      * `func NAME(` / `func (r R) NAME(` / `var NAME` / `const NAME`
        / `type NAME` / `NAME :=` / `NAME = ` (package level)
      * `(NAME Type` / `,NAME Type` — parameter position (handles the
        common case where NAME is a function param in one file and used
        as an argument in another file)

    Workspace-wide plus parameter-aware matches prevent false positives
    on (a) interface methods implemented in other packages and
    (b) parameters used across files in the same enclosing function.

    Returns True on any match; False on zero matches; True on error
    (fail-safe — never false-positive when grep errors)."""
    import subprocess

    # POSIX ERE (grep -E): non-capture `(?:...)` isn't supported;
    # use plain `(...)` groups. `\s` / `\w` work as GNU ERE extensions.
    pattern = (
        rf"^[[:space:]]*(func[[:space:]]+(\([^)]*\)[[:space:]]+)?{re.escape(name)}[[:space:]]*[(\[]|"
        rf"var[[:space:]]+{re.escape(name)}[[:space:]]|"
        rf"const[[:space:]]+{re.escape(name)}[[:space:]]|"
        rf"type[[:space:]]+{re.escape(name)}[[:space:]]|"
        rf"{re.escape(name)}[[:space:]]*:?=)|"
        # parameter position: `(name Type` or `, name Type`
        rf"[(,][[:space:]]*{re.escape(name)}[[:space:]]+(\*|\[\]|\.\.\.)*[[:alnum:]._]"
    )
    try:
        r = subprocess.run(
            [
                "grep", "-r", "-E", pattern, workspace_path,
                "--include=*.go", "--max-count=1", "-l",
                "--exclude-dir=.git", "--exclude-dir=vendor",
                "--exclude-dir=node_modules", "--exclude-dir=.venv",
            ],
            capture_output=True, text=True, timeout=timeout_s,
        )
        if r.returncode == 0 and r.stdout.strip():
            return True
        return r.returncode != 1
    except Exception:
        return True


def _extract_go_locals_from_diff(diff_text: str) -> set[str]:
    """Scan `+` lines for names that are LOCAL (not package-level):
    method receivers, function parameters, and `:=` short-var decls.

    We use this as a skip-list when looking for phantom references —
    a reference to a local is not a compile error.
    """
    locals_set: set[str] = set()
    for raw in diff_text.splitlines():
        if not raw.startswith("+"):
            continue
        body = raw[1:]
        stripped = body.lstrip()
        if stripped.startswith("//") or not stripped:
            continue
        if "//" in body:
            body = body.split("//", 1)[0]

        # Short-var decl: `name, name2 := ...`
        for m in re.finditer(
            r"(?:^|[\s;{}])([a-z_][\w]*(?:\s*,\s*[a-z_][\w]*)*)\s*:=",
            body,
        ):
            chunk = m.group(1)
            for n in chunk.split(","):
                n = n.strip()
                if n.isidentifier():
                    locals_set.add(n)

        # Function signature: capture receiver + param names
        sig_m = _GO_FUNC_SIG_START_RE.match(body)
        if sig_m:
            recv = sig_m.group(1)
            if recv:
                locals_set.add(recv)
            # Extract param list — everything between the opening `(`
            # of the signature's param list and the matching `)`.
            open_idx = body.find("(", sig_m.end() - 1)
            if open_idx != -1:
                depth = 0
                close_idx = -1
                for idx in range(open_idx, len(body)):
                    c = body[idx]
                    if c == "(":
                        depth += 1
                    elif c == ")":
                        depth -= 1
                        if depth == 0:
                            close_idx = idx
                            break
                if close_idx != -1:
                    params = body[open_idx + 1: close_idx]
                    # Each param is `name Type` or `name1, name2 Type`.
                    # Split on commas, extract leading identifier.
                    for seg in params.split(","):
                        seg = seg.strip()
                        # seg can be `name Type`, `name`, or just
                        # `Type` (unnamed result). Only keep first
                        # token if it's followed by whitespace + Type.
                        first = re.match(r"^([a-z_][\w]*)\s+", seg)
                        if first:
                            locals_set.add(first.group(1))
    return locals_set


def _extract_go_bare_references_from_diff(
    diff_text: str,
    *,
    skip_names: Optional[set[str]] = None,
) -> List[tuple[str, int]]:
    """Yield (name, new_line_number) for every substantive bare-identifier
    reference on `+` lines.

    Captured positions:
      * Function call: `name(`
      * Function argument: `,name,` / `(name,` / `,name)`

    Filters applied:
      * Go keywords + built-ins (`len`, `make`, etc.)
      * Method-on-obj / package-qualified (preceded by `.`)
      * Function declaration self-match (`func X(` — X is the decl name)
      * Local names (parameters / receivers / `:=` vars) via skip_names
      * Interface method signatures (whole-line matching `Name(args) ret?`)
      * Identifiers that are NOT substantive (locals like `ctx`, `req`,
        `err`, `i`) — filtered via `_GO_SUBSTANTIVE_IDENT_RE`
      * Comment / string / blank lines
    """
    skip_names = skip_names or set()
    results: List[tuple[str, int]] = []
    current_new_line = 0
    for raw in diff_text.splitlines():
        if raw.startswith("@@"):
            m = _DIFF_HUNK_HEADER_RE.match(raw)
            if m:
                current_new_line = int(m.group(1))
            continue
        if raw.startswith("---") or raw.startswith("+++"):
            continue
        is_addition = raw.startswith("+")
        if is_addition:
            body = raw[1:]
            stripped = body.strip()
            if (
                stripped.startswith("//")
                or stripped.startswith("/*")
                or stripped.startswith("*")
                or not stripped
            ):
                if not raw.startswith("-"):
                    current_new_line += 1
                continue
            if "//" in body:
                body = body.split("//", 1)[0]

            # Skip lines that look like a method signature (interface
            # method decl): `Foo(x int) string`. Distinguished from a
            # call (`foo("x")`) by having a TYPED param inside the
            # parens — `name Type` pattern.
            if (
                not stripped.startswith("func ")
                and _GO_METHOD_SIG_RE.match(body)
            ):
                # Extract content between outer `(` and matching `)`
                open_idx = body.find("(")
                if open_idx >= 0:
                    depth = 0
                    close_idx = -1
                    for _idx in range(open_idx, len(body)):
                        _c = body[_idx]
                        if _c == "(":
                            depth += 1
                        elif _c == ")":
                            depth -= 1
                            if depth == 0:
                                close_idx = _idx
                                break
                    if close_idx > open_idx:
                        params = body[open_idx + 1: close_idx]
                        if _GO_TYPED_PARAM_RE.search(params):
                            # Typed param → signature → skip line
                            if not raw.startswith("-"):
                                current_new_line += 1
                            continue
            # Skip the function declaration on this line to avoid
            # self-match against its own name.
            decl_m = _GO_FUNC_DECL_RE.match(body)
            decl_name = decl_m.group(1) if decl_m else None

            # Inline accept-filter. Uses loop-locals deliberately
            # (consumed within the same iteration; no closure capture
            # escapes the loop body).
            def _go_ident_accepted(name: str, start: int) -> bool:
                if name in _GO_BUILTINS:
                    return False
                if name in skip_names:
                    return False
                if decl_name and name == decl_name:  # noqa: B023
                    return False
                if start > 0 and body[start - 1] == ".":  # noqa: B023
                    return False
                return bool(_GO_SUBSTANTIVE_IDENT_RE.match(name))

            # Position 1: bare CALL sites (name followed by `(`)
            for m in _GO_BARE_CALL_RE.finditer(body):
                name = m.group(1)
                if _go_ident_accepted(name, m.start(1)):
                    results.append((name, current_new_line))
            # Position 2: bare ARGUMENT positions (name between `(|,`
            # and `,|)`). Captures constants/vars passed as arguments.
            for m in _GO_BARE_ARG_RE.finditer(body):
                name = m.group(1)
                if _go_ident_accepted(name, m.start(1)):
                    results.append((name, current_new_line))
        if not raw.startswith("-"):
            current_new_line += 1
    return results


# Backwards-compat alias for any in-tree callers / future re-use.
_extract_go_bare_calls_from_diff = _extract_go_bare_references_from_diff


def _scan_new_go_references_for_missing(
    workspace_path: str,
    file_diffs: Dict[str, str],
    *,
    max_symbols_checked: int = 24,
    grep_timeout_s: float = 8.0,
) -> List[Dict[str, str]]:
    """P13-Go — Deterministic Go phantom bare-identifier detector.

    Scans `.go` file diffs for newly-added bare call sites (no `pkg.`
    prefix, not a method call) and verifies each name resolves to a
    top-level definition in the SAME PACKAGE DIRECTORY. Names that
    grep finds zero matches for are phantom — a `go build` compile
    error the LLM worker routinely misses.

    Guards:
      * Skips files using dot-imports (can't disambiguate)
      * Filters Go keywords + built-ins (`len`, `append`, `make`, …)
      * Caps `max_symbols_checked` grep calls per PR
      * `grep_timeout_s` subprocess timeout per symbol
      * Fails soft — any exception returns current findings unchanged
    """
    if not workspace_path or not file_diffs:
        return []

    found: List[Dict[str, str]] = []
    checked = 0
    seen_names: set = set()

    for file_path, diff_text in file_diffs.items():
        if not file_path.endswith(".go"):
            continue
        # Test files routinely reference _test helpers that live across
        # package boundaries; scope out to avoid noise.
        if file_path.endswith("_test.go"):
            continue
        if _go_dir_has_dot_import(file_path, workspace_path):
            continue
        locals_set = _extract_go_locals_from_diff(diff_text)
        for name, line in _extract_go_bare_references_from_diff(
            diff_text, skip_names=locals_set,
        ):
            if name in seen_names:
                continue
            if checked >= max_symbols_checked:
                break
            seen_names.add(name)
            checked += 1
            if _go_symbol_defined_anywhere(
                workspace_path, name, timeout_s=grep_timeout_s,
            ):
                continue
            found.append({
                "name": name,
                "referenced_at": f"{file_path}:{line}",
                "evidence": (
                    f"Deterministic workspace-wide grep for "
                    f"`func/var/const/type {name}` in any `.go` "
                    f"file → 0 matches. Bare identifier reference "
                    f"will fail `go build` with 'undefined: {name}'."
                ),
            })
        if checked >= max_symbols_checked:
            break
    return found


# ---------------------------------------------------------------------------
# P13-Java — deterministic phantom class reference detector for Java.
# ---------------------------------------------------------------------------
# Targets phantom class references (can't compile) introduced by a PR:
#   * `new Foo(...)`
#   * `Foo var = ...` (type declaration)
#   * `Foo.staticMethod(...)` (static entry)
#   * `<Foo>` (generic parameter)
# Verification: the class must be either
#   (a) imported in the file (read actual file content, not just diff);
#   (b) defined in same-package `.java` files; or
#   (c) a java.lang.* implicit import.
# ---------------------------------------------------------------------------

_JAVA_CLASS_REF_PATTERNS: List[re.Pattern[str]] = [
    # new ClassName(
    re.compile(r"\bnew\s+([A-Z][A-Za-z0-9_]*)\s*[(<]"),
    # ClassName.staticCall( — UPPER start disambiguates class from variable
    re.compile(r"(?:^|[\s=,(\[{;])([A-Z][A-Za-z0-9_]*)\.[a-z_][A-Za-z0-9_]*\s*\("),
    # <ClassName> or <ClassName, …> — generic parameters
    re.compile(r"<\s*([A-Z][A-Za-z0-9_]*)\s*(?:,|>)"),
    # extends/implements/throws ClassName
    re.compile(r"\b(?:extends|implements|throws)\s+([A-Z][A-Za-z0-9_]*)"),
]

_JAVA_IMPORT_RE = re.compile(
    r"^\s*import\s+(?:static\s+)?([\w.]+)(?:\.(\*|\w+))?\s*;\s*$",
    re.MULTILINE,
)
_JAVA_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)

# java.lang.* classes are implicitly imported in every Java file.
# List covers the standard classes + common exceptions. If a rare one
# is missing (e.g. `ClassValue`) the scanner may over-flag — but these
# are rare enough in PR diffs that we prefer the cleaner filter.
_JAVA_LANG_CLASSES: set[str] = {
    # primitives wrappers
    "Boolean", "Byte", "Character", "Double", "Float", "Integer",
    "Long", "Short", "Void",
    # core
    "Object", "String", "StringBuilder", "StringBuffer", "Math",
    "System", "Thread", "ThreadGroup", "ThreadLocal", "Runtime",
    "Process", "ProcessBuilder", "Class", "ClassLoader", "Package",
    "Enum", "Record", "Number", "Comparable", "Iterable", "Readable",
    "CharSequence", "AutoCloseable", "Cloneable", "Runnable",
    # throwables
    "Throwable", "Exception", "Error", "RuntimeException",
    "NullPointerException", "IllegalArgumentException",
    "IllegalStateException", "UnsupportedOperationException",
    "ClassNotFoundException", "ClassCastException",
    "ArrayIndexOutOfBoundsException", "IndexOutOfBoundsException",
    "StringIndexOutOfBoundsException", "ArithmeticException",
    "NumberFormatException", "InterruptedException",
    "NoSuchMethodException", "NoSuchFieldException",
    "SecurityException", "OutOfMemoryError", "StackOverflowError",
    "AssertionError", "LinkageError", "NoClassDefFoundError",
    "VerifyError", "AbstractMethodError", "IncompatibleClassChangeError",
}
# Java primitive keywords (never class references)
_JAVA_PRIMITIVES: set[str] = {
    "void", "boolean", "byte", "char", "short", "int", "long", "float",
    "double",
}


def _parse_java_file_imports(workspace_path: str, file_path: str) -> tuple[set[str], set[str], str]:
    """Return (imported_simple_names, star_imports_prefixes, own_package).

    Reads the actual file on disk (head 16KB is enough — imports are at top).
    Handles both `import com.foo.Bar;` (adds `Bar` to the simple-name set)
    and `import com.foo.*;` (adds `com.foo` to the star set).
    """
    import os as _os
    full = _os.path.join(workspace_path, file_path)
    imported_simple: set[str] = set()
    star_imports: set[str] = set()
    own_package = ""
    try:
        with open(full, encoding="utf-8", errors="replace") as f:
            head = f.read(16384)
    except Exception:
        return (imported_simple, star_imports, own_package)

    pkg_m = _JAVA_PACKAGE_RE.search(head)
    if pkg_m:
        own_package = pkg_m.group(1)

    for m in _JAVA_IMPORT_RE.finditer(head):
        body = m.group(1)
        tail = m.group(2)
        if tail == "*":
            star_imports.add(body)  # e.g. `com.foo` from `import com.foo.*;`
        else:
            simple = tail if tail else body.rsplit(".", 1)[-1]
            imported_simple.add(simple)
    return (imported_simple, star_imports, own_package)


def _java_source_set_peers(package_dir: str) -> List[str]:
    """Return the package_dir plus its Maven/Gradle source-set peers.

    In Maven/Gradle conventions, ``src/main/java/com/foo`` and
    ``src/test/java/com/foo`` (plus less-common ``src/integrationTest/java``)
    hold the same Java package — classes in ``main`` are visible from
    ``test`` by the "same package" rule without an import statement.

    Returns the input dir first (unchanged), followed by any peer
    directories that exist logically (caller verifies on disk). If the
    input doesn't sit under a known source-set root, returns just
    the input unchanged.

    Regression target: PR #14161 flagged `PaymentController` as phantom
    from `src/test/java/.../controller/PaymentControllerTest.java`
    because the scanner only checked the test package-dir. The class
    lives in the peer `src/main/java/.../controller/PaymentController.java`.
    """
    peers: List[str] = [package_dir]
    known_roots = ("src/test/java/", "src/main/java/", "src/integrationTest/java/")
    matched_root = None
    for root in known_roots:
        if f"/{root}" in f"/{package_dir}/" or package_dir.startswith(root):
            matched_root = root
            break
    if not matched_root:
        return peers
    for other in known_roots:
        if other == matched_root:
            continue
        peer = package_dir.replace(matched_root, other, 1)
        if peer != package_dir and peer not in peers:
            peers.append(peer)
    return peers


def _java_class_defined_in_package(
    workspace_path: str, package_dir: str, name: str, *, timeout_s: float = 8.0,
) -> bool:
    """Grep package dir (+ its Maven source-set peers) for a top-level
    class/interface/enum/record definition.

    For a file in ``src/test/java/com/foo/``, also checks the peer
    ``src/main/java/com/foo/`` (same Java package, different source
    root). Without this check we false-positive every
    ``FooControllerTest`` that references ``FooController`` in the
    same package.
    """
    import os as _os
    import subprocess

    candidate_files: List[str] = []
    for peer in _java_source_set_peers(package_dir):
        full_dir = _os.path.join(workspace_path, peer)
        if not _os.path.isdir(full_dir):
            continue
        try:
            candidate_files.extend(
                _os.path.join(full_dir, f)
                for f in _os.listdir(full_dir)
                if f.endswith(".java")
            )
        except OSError:
            continue

    if not candidate_files:
        return True  # fail-safe when no peer resolves on disk

    pattern = (
        rf"^\s*(public\s+|private\s+|protected\s+)?"
        rf"(abstract\s+|final\s+|static\s+|sealed\s+)*"
        rf"(class|interface|enum|record|@interface)\s+{re.escape(name)}\b"
    )
    try:
        r = subprocess.run(
            ["grep", "-E", "-l", "--max-count=1", pattern, *candidate_files],
            capture_output=True, text=True, timeout=timeout_s,
        )
        if r.returncode == 0 and r.stdout.strip():
            return True
        return r.returncode != 1
    except Exception:
        return True


def _extract_java_class_refs_from_diff(diff_text: str) -> List[tuple[str, int]]:
    """Yield (class_name, new_line_number) for every class reference
    in `+` lines. Best-effort: skips obvious comment/string-only lines."""
    results: List[tuple[str, int]] = []
    current_new_line = 0
    for raw in diff_text.splitlines():
        if raw.startswith("@@"):
            m = _DIFF_HUNK_HEADER_RE.match(raw)
            if m:
                current_new_line = int(m.group(1))
            continue
        if raw.startswith("---") or raw.startswith("+++"):
            continue
        if raw.startswith("+"):
            body = raw[1:]
            stripped = body.strip()
            if (
                stripped.startswith("//")
                or stripped.startswith("/*")
                or stripped.startswith("*")
                or stripped.startswith("import ")
                or stripped.startswith("package ")
                or not stripped
            ):
                if not raw.startswith("-"):
                    current_new_line += 1
                continue
            # Drop inline // comment tail
            if "//" in body:
                body = body.split("//", 1)[0]
            for pat in _JAVA_CLASS_REF_PATTERNS:
                for m in pat.finditer(body):
                    name = m.group(1)
                    if name in _JAVA_PRIMITIVES:
                        continue
                    results.append((name, current_new_line))
        if not raw.startswith("-"):
            current_new_line += 1
    return results


def _scan_new_java_references_for_missing(
    workspace_path: str,
    file_diffs: Dict[str, str],
    *,
    max_symbols_checked: int = 24,
    grep_timeout_s: float = 8.0,
) -> List[Dict[str, str]]:
    """P13-Java — Deterministic Java phantom class reference detector.

    Scans `.java` file diffs for newly-referenced class names (via
    `new X(`, `X var =`, `X.staticMethod(`, `<X>`, `extends X`, etc.).
    A class name is a phantom if it is NOT:
      * imported in the file (parsed from actual file content);
      * covered by a `com.foo.*` star import (conservative: we skip
        these, cannot verify without FQN resolution);
      * defined in a same-package `.java` file;
      * a java.lang.* implicit import.

    Guards:
      * Caps `max_symbols_checked` grep calls per PR
      * `grep_timeout_s` subprocess timeout per symbol
      * Fails soft on any error
    """
    import os as _os
    if not workspace_path or not file_diffs:
        return []

    found: List[Dict[str, str]] = []
    checked = 0
    # Dedup globally across the PR — same (file, name) reported only
    # once even if referenced multiple times.
    seen: set = set()

    for file_path, diff_text in file_diffs.items():
        if not file_path.endswith(".java"):
            continue
        imported, star_imports, _pkg = _parse_java_file_imports(
            workspace_path, file_path,
        )
        # If the file uses star-imports, MVP skips the file to avoid
        # false positives. Star-imports hide which exact class names
        # are available.
        if star_imports:
            continue
        package_dir = _os.path.dirname(file_path)
        for name, line in _extract_java_class_refs_from_diff(diff_text):
            key = (file_path, name)
            if key in seen:
                continue
            if checked >= max_symbols_checked:
                break
            seen.add(key)
            # Filter: java.lang, explicitly imported, or same-package
            if name in _JAVA_LANG_CLASSES:
                continue
            if name in imported:
                continue
            checked += 1
            if _java_class_defined_in_package(
                workspace_path, package_dir, name,
                timeout_s=grep_timeout_s,
            ):
                continue
            found.append({
                "name": name,
                "referenced_at": f"{file_path}:{line}",
                "evidence": (
                    f"Deterministic grep for `class/interface/enum/"
                    f"record {name}` in Java package directory "
                    f"`{package_dir}/` → 0 matches; `{name}` is not "
                    f"imported in the file nor in `java.lang`. "
                    f"Compilation will fail with 'cannot find symbol: "
                    f"class {name}'."
                ),
            })
        if checked >= max_symbols_checked:
            break
    return found


# P14 — Mechanical stub-function detector (Python + Go).
# A "stub function" is one whose body unconditionally returns a
# "not implemented" sentinel. In a PR that ostensibly adds new
# functionality, every stub should either be TODO-tagged OR be
# obviously not called — anything else is a bug. We look for two
# shapes:
#   Go  : `return ..., errors.New("not implemented")` / `return errors.New("not implemented")`
#   Py  : `raise NotImplementedError`
# Then we scan the diff for callers of those functions. A call-site
# inside the diff is a strong signal the stub is live code path, not
# a TODO.
_GO_STUB_BODY_RE = re.compile(
    r"""^\s*return\s+                       # return statement
        (?:[^,]+,\s*)?                      # optional first tuple element
        errors\.New\(
        ["'](?:not\ implemented|Not\ Implemented|TODO:?\s*implement)["']
        \)\s*$""",
    re.VERBOSE | re.MULTILINE,
)
_PY_STUB_BODY_RE = re.compile(
    r"^\s*raise\s+NotImplementedError\b", re.MULTILINE,
)
# Java: `throw new UnsupportedOperationException(...)` is the canonical
# "stub" pattern. `NotImplementedException` is Apache Commons. For
# generic runtime exceptions we require the message to mention "not
# implemented" / "not supported" to avoid flagging legitimate errors.
_JAVA_STUB_BODY_RE = re.compile(
    r"""^\s*throw\s+new\s+
        (?:
            UnsupportedOperationException\s*\([^)]*\)
            |
            NotImplementedException\s*\([^)]*\)
            |
            (?:RuntimeException|AssertionError|IllegalStateException)
            \s*\(\s*["'][^"']*
            (?:not\s*implement|Not\s*Implement|not\s*supported|Not\s*Supported)
            [^"']*["']\s*\)
        )
        \s*;\s*$""",
    re.VERBOSE | re.MULTILINE,
)
_GO_FUNC_HEADER_RE = re.compile(
    r"^\+func\s+(?:\(\s*\w+\s+\*?\w+\s*\)\s+)?(\w+)\s*\(",
)
_PY_FUNC_HEADER_RE = re.compile(
    r"^\+\s*def\s+(\w+)\s*\(",
)
# Java method declaration: optional annotations on same line, one or
# more modifiers, optional generic-type parameter, a return type, the
# method name, open paren, closing paren, and opening brace — all on
# the same line. Multi-line signatures are rare in stubs; accept the
# single-line shape as sufficient.
_JAVA_FUNC_HEADER_RE = re.compile(
    r"""^\+\s*
        (?:@\w+(?:\([^)]*\))?\s+)*
        (?:(?:public|private|protected|static|final|synchronized|abstract|default|native)\s+)+
        (?:<[^>]+>\s+)?
        (?:[\w.<>\[\],\s?]+?\s+)?
        (\w+)
        \s*\(""",
    re.VERBOSE,
)
# Same-line marker that a Java code line is (probably) a method
# declaration — used during call-site scanning to exclude declarations
# from being counted as calls when the stub method is also declared in
# the diff (e.g. interface + impl both in scope).
_JAVA_METHOD_DECL_MARKER_RE = re.compile(
    r"^(?:@\w+(?:\([^)]*\))?\s+)*"
    r"(?:(?:public|private|protected|static|final|synchronized|abstract|default|native)\s+)+"
)


def _scan_for_stub_call_sites(
    file_diffs: Dict[str, str],
) -> List[Dict[str, str]]:
    """P14 — Detect stub functions introduced by the PR and match them
    against call sites also in the diff. Returns one dict per detected
    (stub_name, caller_site) pair.

    Operates purely on the diff text; no workspace read. Narrow by
    design: we only flag stubs whose function body in the diff
    contains a literal "not implemented" error return, and we only
    flag call sites that are also added by the diff. This avoids
    flagging legitimate TODO placeholders.
    """
    if not file_diffs:
        return []

    # Step 1: enumerate new stub functions.
    #   { name -> (file, line_in_new_file) }
    stubs: Dict[str, tuple] = {}
    for file_path, diff_text in file_diffs.items():
        is_go = file_path.endswith(".go")
        is_py = file_path.endswith(".py")
        is_java = file_path.endswith(".java")
        if not (is_go or is_py or is_java):
            continue
        # Walk diff line by line tracking hunks + function bodies.
        current_new_line = 0
        current_fn_name: Optional[str] = None
        current_fn_body_lines: List[str] = []
        current_fn_decl_line: int = 0
        for raw in diff_text.splitlines():
            if raw.startswith("@@"):
                m = _DIFF_HUNK_HEADER_RE.match(raw)
                if m:
                    current_new_line = int(m.group(1))
                current_fn_name = None
                current_fn_body_lines = []
                continue
            if raw.startswith("---") or raw.startswith("+++"):
                continue
            is_addition = raw.startswith("+")
            if is_addition:
                if is_go:
                    header_re = _GO_FUNC_HEADER_RE
                elif is_py:
                    header_re = _PY_FUNC_HEADER_RE
                else:  # is_java
                    header_re = _JAVA_FUNC_HEADER_RE
                hm = header_re.match(raw)
                if hm:
                    # New function declaration — reset buffer.
                    current_fn_name = hm.group(1)
                    current_fn_decl_line = current_new_line
                    current_fn_body_lines = [raw]
                elif current_fn_name:
                    current_fn_body_lines.append(raw)
                    # Check for closing `}` (Go or Java) or a stub line
                    # body (Python). For Java the `}` is usually indented
                    # (method-in-class); for Go it's usually column 0.
                    # Accept either case.
                    is_brace_close = (
                        (is_go or is_java)
                        and raw.startswith("+")
                        and raw[1:].strip() == "}"
                    )
                    if is_brace_close:
                        body = "\n".join(
                            ln.lstrip("+ \t")
                            for ln in current_fn_body_lines
                        )
                        body_re = (
                            _GO_STUB_BODY_RE if is_go else _JAVA_STUB_BODY_RE
                        )
                        if body_re.search(body):
                            stubs[current_fn_name] = (
                                file_path, current_fn_decl_line,
                            )
                        current_fn_name = None
                        current_fn_body_lines = []
                    elif is_py:
                        # Strip leading `+` to match against clean code.
                        code_line = raw[1:] if raw.startswith("+") else raw
                        if _PY_STUB_BODY_RE.search(code_line):
                            stubs[current_fn_name] = (
                                file_path, current_fn_decl_line,
                            )
                        # don't reset — Python fn may have more lines
            if not raw.startswith("-"):
                current_new_line += 1

    if not stubs:
        return []

    # Step 2: scan diff for callers of those stub names. Callers can
    # live on + lines (newly added calls) OR unchanged context lines
    # (pre-existing call sites that now hit a NEW stub because the
    # stub's definition was just introduced). We skip - lines (removed)
    # and function-declaration lines (avoid matching `func Name(` or
    # `def Name(` as a call of Name).
    findings: List[Dict[str, str]] = []
    # Call pattern: Name( not preceded by `func ` (Go), `def ` (Python),
    # `type `, `class `, or a period alone (to keep `obj.method(`).
    # Use a look-behind check via pre-filter.
    _DECL_PREFIXES = ("func ", "def ", "type ", "class ")
    for stub_name, (stub_file, stub_line) in stubs.items():
        call_re = re.compile(rf"\b{re.escape(stub_name)}\s*\(")
        for file_path, diff_text in file_diffs.items():
            current_new_line = 0
            seen_sites: set = set()
            for raw in diff_text.splitlines():
                if raw.startswith("@@"):
                    m = _DIFF_HUNK_HEADER_RE.match(raw)
                    if m:
                        current_new_line = int(m.group(1))
                    continue
                if raw.startswith("---") or raw.startswith("+++"):
                    continue
                # Skip removed lines entirely.
                if raw.startswith("-"):
                    continue
                is_code_line = raw.startswith(("+", " "))
                if is_code_line and call_re.search(raw):
                    # Strip leading +/space to examine the code.
                    code = raw[1:] if raw.startswith(("+", " ")) else raw
                    code_stripped = code.lstrip()
                    # Skip function/class declarations that happen to
                    # share the name (`func TablesList(...)` is NOT a
                    # call to TablesList, it's defining a same-name
                    # function in a different package/receiver).
                    is_decl = any(
                        code_stripped.startswith(p) for p in _DECL_PREFIXES
                    )
                    # Java method declarations start with annotations /
                    # modifier keywords (`public Foo(...)`, `@Override
                    # public <T> Foo()`). Treat any line whose stripped
                    # prefix matches that shape as a decl, not a call.
                    if not is_decl and file_path.endswith(".java"):
                        is_decl = bool(
                            _JAVA_METHOD_DECL_MARKER_RE.match(code_stripped)
                        )
                    # Also skip the stub definition line itself.
                    is_self_site = (
                        file_path == stub_file
                        and current_new_line == stub_line
                    )
                    if not is_decl and not is_self_site:
                        key = (file_path, current_new_line)
                        if key not in seen_sites:
                            seen_sites.add(key)
                            findings.append({
                                "stub_name": stub_name,
                                "stub_file": stub_file,
                                "stub_line": str(stub_line),
                                "caller_file": file_path,
                                "caller_line": str(current_new_line),
                            })
                # Advance new-file counter for + and context lines.
                current_new_line += 1
    return findings


def _inject_stub_caller_findings(
    findings: List[Dict[str, Any]],
    file_diffs: Dict[str, str],
) -> tuple[List[Dict[str, Any]], int]:
    """P14 injection — turn (stub, caller) pairs into synthetic findings.

    Each finding points at the CALLER site with a high-confidence
    'calls a stub function that always returns not implemented'
    description. Skips injection if the coordinator already flagged
    the caller site at approximately the same line (±3).

    Returns (findings_with_injections, injected_count).
    """
    if not file_diffs:
        return (findings, 0)

    pairs = _scan_for_stub_call_sites(file_diffs)
    if not pairs:
        return (findings, 0)

    result = list(findings)
    injected = 0
    for p in pairs:
        caller_file = p["caller_file"]
        try:
            caller_line = int(p["caller_line"])
        except (ValueError, TypeError):
            continue
        stub_name = p["stub_name"]
        # Skip if an existing finding covers this (file, ±3 lines).
        covered = False
        for f in result:
            if f.get("file") != caller_file:
                continue
            fl = int(f.get("start_line") or 0)
            if abs(fl - caller_line) <= 3:
                # Also check the finding mentions the stub or concept.
                title = str(f.get("title", "") or "")
                if (
                    stub_name in title
                    or "stub" in title.lower()
                    or "not implemented" in title.lower()
                ):
                    covered = True
                    break
        if covered:
            continue
        synthetic = {
            "title": (
                f"Call to `{stub_name}()` hits a stub that always "
                f"returns 'not implemented' — runtime failure"
            ),
            "severity": "high",
            "confidence": 0.95,
            "file": caller_file,
            "start_line": caller_line,
            "end_line": caller_line,
            "evidence": [
                f"Stub definition at `{p['stub_file']}:{p['stub_line']}` "
                f"returns an error literal 'not implemented' / "
                f"NotImplementedError.",
                f"Call site at `{caller_file}:{caller_line}` invokes "
                f"the stub and does not guard against the failure.",
            ],
            "risk": (
                f"Every code path that reaches `{caller_file}:"
                f"{caller_line}` will surface the 'not implemented' "
                f"error to the user. The feature being implemented in "
                f"this PR is unshippable until the stub is filled in."
            ),
            "suggested_fix": (
                f"Either implement `{stub_name}` at "
                f"`{p['stub_file']}:{p['stub_line']}` with real logic, "
                f"or gate the caller behind a feature flag / explicit "
                f"'unsupported' error response until the implementation "
                f"lands."
            ),
            "category": "correctness",
            "_injected_from": "p14_stub_caller",
        }
        result.append(synthetic)
        injected += 1
    return (result, injected)


_EXISTS_NEGATION_MARKERS = (
    "does not exist",
    "doesn't exist",
    "not defined",
    "undefined",
    "is missing",
    "never defined",
    "no such symbol",
    "importerror",
    "nameerror",
    "not found in",
    "could not be found",
)


def _finding_claims_symbol_missing(finding: Dict[str, Any], symbol: str) -> bool:
    """Heuristic: does this finding claim `symbol` is missing/undefined?

    Used by the Phase 2 reflection pass to catch findings whose premise
    contradicts an `exists=True` fact. We match only when the finding
    BOTH mentions the symbol AND uses existence-negation phrasing —
    mentioning the symbol alone is not enough (many real bugs involve
    existing symbols)."""
    if not symbol:
        return False
    haystack_parts: List[str] = [
        str(finding.get("title", "") or ""),
        str(finding.get("risk", "") or ""),
        str(finding.get("suggested_fix", "") or ""),
    ]
    evidence = finding.get("evidence")
    if isinstance(evidence, list):
        haystack_parts.extend(str(e) for e in evidence)
    elif isinstance(evidence, str):
        haystack_parts.append(evidence)
    haystack = " ".join(haystack_parts)
    if symbol not in haystack:
        return False
    lowered = haystack.lower()
    return any(marker in lowered for marker in _EXISTS_NEGATION_MARKERS)


def _reflect_against_phase2_facts(
    findings: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], int]:
    """External-signal reflection (P8).

    Drops findings whose premise is contradicted by Phase 2 existence
    facts. The mechanical rule — deliberately narrow to avoid over-
    filtering — is:

        If the finding claims "symbol X doesn't exist / is undefined /
        will raise ImportError" AND Phase 2 recorded ``exists=True`` for
        X, drop the finding. Its premise is demonstrably wrong.

    Injected Phase 2 findings (``_injected_from`` set) are never dropped
    by this pass — they came FROM the facts, so they cannot contradict.

    Returns (kept_findings, dropped_count). Safe when no FactStore is
    active (returns input unchanged).
    """
    from app.scratchpad import current_factstore

    store = current_factstore()
    if store is None:
        return (findings, 0)

    try:
        present = list(store.iter_existence(exists=True))
    except Exception as exc:
        logger.warning(
            "[PR Brain v2] reflection pass skipped — iter_existence failed: %s",
            exc,
        )
        return (findings, 0)

    if not present:
        return (findings, 0)

    present_symbols = {p.symbol_name for p in present if p.symbol_name}
    if not present_symbols:
        return (findings, 0)

    kept: List[Dict[str, Any]] = []
    dropped = 0
    for f in findings:
        if f.get("_injected_from"):
            kept.append(f)
            continue
        contradicted = False
        for symbol in present_symbols:
            if _finding_claims_symbol_missing(f, symbol):
                logger.info(
                    "[PR Brain v2] Reflection drop: finding %r claims "
                    "`%s` is missing but Phase 2 confirmed exists=True",
                    f.get("title", "")[:80], symbol,
                )
                contradicted = True
                break
        if contradicted:
            dropped += 1
        else:
            kept.append(f)
    return (kept, dropped)


def _filter_findings_to_diff_scope(
    findings: List[Dict[str, Any]],
    file_diffs: Dict[str, str],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], int]:
    """Per-finding diff-scope verification (P11 cheap).

    Inspired by Claude Code `/ultrareview`'s "every reported finding is
    independently reproduced and verified". This is the mechanical half
    of that pattern — an LLM-free check that a finding's file is actually
    touched by the diff. Findings that point at files the PR does not
    modify are almost always coordinator hallucinations (e.g. it confused
    a cross-file reference with a diff change).

    Kept injected findings untouched — Phase 2 may flag a diff file's
    reference to a symbol defined in an un-touched file, and that is
    legitimate scope.

    Returns (kept, demoted, demoted_count). Demoted findings are handed
    back so the caller can append them to the secondary-notes block.
    """
    if not file_diffs or not findings:
        return (list(findings), [], 0)

    touched_files = set(file_diffs.keys())
    # Allow trailing-slash / normalisation mismatches by also matching
    # basename when the coordinator reported a short path.
    touched_basenames = {p.rsplit("/", 1)[-1] for p in touched_files}

    kept: List[Dict[str, Any]] = []
    demoted: List[Dict[str, Any]] = []
    demoted_count = 0

    for f in findings:
        if f.get("_injected_from"):
            kept.append(f)
            continue
        file_claim = str(f.get("file", "") or "").strip()
        if not file_claim:
            kept.append(f)
            continue
        base = file_claim.rsplit("/", 1)[-1]
        in_diff = file_claim in touched_files or base in touched_basenames
        if in_diff:
            kept.append(f)
            continue
        logger.info(
            "[PR Brain v2] Diff-scope drop: finding %r targets `%s` "
            "which is not in the PR diff (touched: %d files)",
            f.get("title", "")[:80], file_claim, len(touched_files),
        )
        f = {**f, "_demoted_reason": "file_not_in_diff"}
        demoted.append(f)
        demoted_count += 1

    return (kept, demoted, demoted_count)


def _extract_single_verdict(raw: str) -> str:
    """Parse the single-finding verifier's JSON. Returns one of
    confirmed / refuted / unclear. Defaults to unclear on parse failure."""
    import json as _json
    import re as _re

    fenced = _re.findall(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw)
    candidates = list(reversed(fenced)) if fenced else [raw[max(0, raw.rfind("{")):]]
    for candidate in candidates:
        try:
            parsed = _json.loads(candidate)
            if isinstance(parsed, dict) and "verdict" in parsed:
                v = str(parsed["verdict"]).lower()
                if v in ("confirmed", "refuted", "unclear"):
                    return v
        except (ValueError, _json.JSONDecodeError):
            continue
    return "unclear"


def _extract_batch_verdicts(raw: str, expected_count: int) -> List[str]:
    """Parse the batch verifier's JSON. Returns verdict list aligned to
    input order. Missing / malformed entries default to 'unclear'."""
    import json as _json
    import re as _re

    fenced = _re.findall(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw)
    candidates = list(reversed(fenced))
    for candidate in candidates:
        try:
            parsed = _json.loads(candidate)
            if not isinstance(parsed, dict):
                continue
            verdicts_list = parsed.get("verdicts") or []
            if not isinstance(verdicts_list, list):
                continue
            # Build index→verdict map first so out-of-order lists are handled.
            verdict_map: Dict[int, str] = {}
            for item in verdicts_list:
                if not isinstance(item, dict):
                    continue
                idx = item.get("finding_index")
                v = str(item.get("verdict", "")).lower()
                if isinstance(idx, int) and v in ("confirmed", "refuted", "unclear"):
                    verdict_map[idx] = v
            if verdict_map:
                return [verdict_map.get(i, "unclear") for i in range(expected_count)]
        except (ValueError, _json.JSONDecodeError):
            continue
    return ["unclear"] * expected_count


def _parse_existence_json(raw: str) -> Optional[Dict[str, Any]]:
    """Best-effort extraction of the existence-worker's JSON output.

    Accepts:
      * Fenced ```json {...} ``` blocks (prefer the LAST — models often
        restate near the end)
      * Bare JSON object with "symbols" key anywhere in the text

    Returns the dict on success, ``None`` on failure.
    """
    import json as _json
    import re as _re

    if not raw:
        return None

    fenced = _re.findall(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw)
    candidates: list = list(reversed(fenced))
    if not candidates:
        # Fallback: find a top-level {..} with "symbols" key
        for start in range(len(raw) - 1, -1, -1):
            if raw[start] != "{":
                continue
            depth = 0
            for end in range(start, len(raw)):
                if raw[end] == "{":
                    depth += 1
                elif raw[end] == "}":
                    depth -= 1
                    if depth == 0:
                        snippet = raw[start: end + 1]
                        if '"symbols"' in snippet:
                            candidates.append(snippet)
                        break
            if candidates:
                break
    for candidate in candidates:
        try:
            parsed = _json.loads(candidate)
            if isinstance(parsed, dict) and "symbols" in parsed:
                return parsed
        except (ValueError, _json.JSONDecodeError):
            continue
    return None


def _finding_to_dict(f: ReviewFinding) -> dict:
    """Convert a ReviewFinding to a serializable dict."""
    return {
        "title": f.title,
        "category": f.category.value,
        "severity": f.severity.value,
        "confidence": f.confidence,
        "file": f.file,
        "start_line": f.start_line,
        "end_line": f.end_line,
        "evidence": f.evidence,
        "risk": f.risk,
        "suggested_fix": f.suggested_fix,
        "agent": f.agent,
    }
