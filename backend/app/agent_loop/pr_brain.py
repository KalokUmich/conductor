"""PR Brain — coordinator-worker orchestrator for PR reviews.

Agent-as-tool design: ONE Brain (Sonnet) acts as the coordinator. It
surveys the diff, plans investigations, dispatches scope-bounded
workers (Haiku, via ``dispatch_verify``), replans on surprises, and
synthesises a final review. Mechanical safety nets run alongside the
LLM loop — Phase 2 existence check plus P13 / P14 deterministic
verifiers catch compilation-class and stub-call bug classes regardless
of LLM sampling.

Flow:
  Phase 1: Pre-compute (parse diff, classify risk, prefetch diffs, impact graph)
  Phase 2: Existence check (LLM + P13 phantom-symbol scanners + P14 stub detector)
  Phase 3: Coordinator dispatch loop (survey + dispatch_verify + synthesise)
  Phase 4: Post-process (missing-symbol injection, reflection, diff-scope filter)
  Phase 5: Merge recommendation (deterministic)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

from app.agent_loop.budget_economics import TaskSignals, get_budget_economics

# The deterministic existence/phantom-symbol/stub-scanner subsystem (pure
# functions + their regex/constant tables) lives in existence_scanners.py.
# Re-exported (explicit `x as x` = intentional re-export) so the orchestrator
# methods below and the test suite keep referencing them by name and via
# module attribute.
from app.agent_loop.existence_scanners import (
    _DIFF_HUNK_HEADER_RE as _DIFF_HUNK_HEADER_RE,
)
from app.agent_loop.existence_scanners import (
    _dedup_findings_by_location as _dedup_findings_by_location,
)
from app.agent_loop.existence_scanners import (
    _extract_batch_verdicts as _extract_batch_verdicts,
)
from app.agent_loop.existence_scanners import (
    _extract_go_bare_calls_from_diff as _extract_go_bare_calls_from_diff,
)
from app.agent_loop.existence_scanners import (
    _extract_go_bare_references_from_diff as _extract_go_bare_references_from_diff,
)
from app.agent_loop.existence_scanners import (
    _extract_go_locals_from_diff as _extract_go_locals_from_diff,
)
from app.agent_loop.existence_scanners import (
    _extract_java_class_refs_from_diff as _extract_java_class_refs_from_diff,
)
from app.agent_loop.existence_scanners import (
    _extract_single_verdict as _extract_single_verdict,
)
from app.agent_loop.existence_scanners import (
    _filter_findings_describing_own_fix as _filter_findings_describing_own_fix,
)
from app.agent_loop.existence_scanners import (
    _filter_findings_to_diff_scope as _filter_findings_to_diff_scope,
)
from app.agent_loop.existence_scanners import (
    _finding_claims_symbol_missing as _finding_claims_symbol_missing,
)
from app.agent_loop.existence_scanners import (
    _finding_covers_symbol as _finding_covers_symbol,
)
from app.agent_loop.existence_scanners import (
    _finding_to_dict as _finding_to_dict,
)
from app.agent_loop.existence_scanners import (
    _go_dir_has_dot_import as _go_dir_has_dot_import,
)
from app.agent_loop.existence_scanners import (
    _go_symbol_defined_anywhere as _go_symbol_defined_anywhere,
)
from app.agent_loop.existence_scanners import (
    _inject_missing_symbol_findings as _inject_missing_symbol_findings,
)
from app.agent_loop.existence_scanners import (
    _inject_stub_caller_findings as _inject_stub_caller_findings,
)
from app.agent_loop.existence_scanners import (
    _is_framework_module as _is_framework_module,
)
from app.agent_loop.existence_scanners import (
    _java_class_defined_in_package as _java_class_defined_in_package,
)
from app.agent_loop.existence_scanners import (
    _java_source_set_peers as _java_source_set_peers,
)
from app.agent_loop.existence_scanners import (
    _module_is_first_party as _module_is_first_party,
)
from app.agent_loop.existence_scanners import (
    _parse_existence_json as _parse_existence_json,
)
from app.agent_loop.existence_scanners import (
    _parse_java_file_imports as _parse_java_file_imports,
)
from app.agent_loop.existence_scanners import (
    _parse_reference_location as _parse_reference_location,
)
from app.agent_loop.existence_scanners import (
    _python_symbol_defined_anywhere as _python_symbol_defined_anywhere,
)
from app.agent_loop.existence_scanners import (
    _recompute_merge_recommendation as _recompute_merge_recommendation,
)
from app.agent_loop.existence_scanners import (
    _reflect_against_phase2_facts as _reflect_against_phase2_facts,
)
from app.agent_loop.existence_scanners import (
    _scan_for_stub_call_sites as _scan_for_stub_call_sites,
)
from app.agent_loop.existence_scanners import (
    _scan_new_go_references_for_missing as _scan_new_go_references_for_missing,
)
from app.agent_loop.existence_scanners import (
    _scan_new_java_references_for_missing as _scan_new_java_references_for_missing,
)
from app.agent_loop.existence_scanners import (
    _scan_new_python_imports_for_missing as _scan_new_python_imports_for_missing,
)
from app.agent_loop.existence_scanners import (
    _split_import_names as _split_import_names,
)
from app.agent_loop.lifecycle import fire_hook
from app.ai_provider.base import AIProvider
from app.code_review.diff_parser import parse_diff
from app.code_review.models import (
    PRContext,
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

# Wall-clock cap (seconds) for the Phase 2 existence-check worker. Unlike a
# normal review sub-agent (``sub_agent_timeout`` = 600s in pr_review.yaml), this
# worker runs under a hard orchestrator-side ``asyncio.wait_for`` so a runaway
# never blocks the review — after the deadline it's cancelled and the coordinator
# proceeds with the P13 deterministic facts alone.
#
# Sizing: the worker runs a full ReAct loop (grep / read_file / find_symbol +
# reasoning). Its FIRST symbol lookup triggers a one-time COLD symbol-index build
# — ~57s on a ~2.4K-file worktree, more on larger repos — because every PR gets a
# fresh worktree, so the index is always cold. The old 60s cap was almost entirely
# eaten by that build (observed: index ready at 57s, timeout at 60s, zero facts),
# so the worker reliably produced nothing. 180s leaves a real ReAct budget after
# the cold build while still bounding hangs far below the 600s review cap. The
# structural fix is to pre-warm the index in Phase 1 (then this can drop again).
# Override with CONDUCTOR_PHASE2_TIMEOUT_S.
_PHASE2_TIMEOUT_SECONDS = int(os.environ.get("CONDUCTOR_PHASE2_TIMEOUT_S", "180"))


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
            r"(?:^|/)(?:migrations?|changelog|flyway|liquibase)(?:/|$)" r"|V\d+__[A-Za-z0-9_]+\.sql$",
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
    ".kt": "kotlin",  # kotlin reuses Java Spring Security
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
_SECURITY_PATTERNS_JAVA: List[tuple] = _compile_content_patterns(
    [
        (
            r"@(?:PreAuthorize|Secured|RolesAllowed|WithMockUser|EnableWebSecurity|PermitAll|DenyAll|PostAuthorize|PreFilter|PostFilter)\b",
            "Spring Security annotation (access control)",
        ),
        (
            r"\bnew\s+(?:BCrypt|Argon2|Pbkdf2|SCrypt)PasswordEncoder\s*\(",
            "Password encoder constructor (hashing policy)",
        ),
        (
            r"\b(?:HttpSecurity|SecurityFilterChain|AuthenticationManager|AuthenticationProvider|UserDetailsService|PasswordEncoder|JwtDecoder|JwtAuthenticationConverter|OAuth2AuthenticationToken|JwtEncoder)\b",
            "Spring Security configuration / token primitive",
        ),
        (r"\bMessageDigest\.isEqual\s*\(", "Constant-time byte comparison"),
        (r"\bSecureRandom\s*\(", "Cryptographic RNG construction"),
        (r"\bJwts\.(?:builder|parser|parserBuilder|SIG)\b", "JJWT library call (token sign/verify)"),
        (r'"grant_type"\s*[:,]|"access_token"\s*[:,]|"refresh_token"\s*[:,]', "OAuth2 grant / token field string"),
        (r"\bCipher\.getInstance\s*\(", "Cipher construction (crypto primitive)"),
        (r"\b(?:KeyPairGenerator|KeyFactory|KeyGenerator)\.getInstance\s*\(", "Crypto key material setup"),
    ]
)

# Python: decorators + security library imports + password / crypto funcs.
_SECURITY_PATTERNS_PYTHON: List[tuple] = _compile_content_patterns(
    [
        (
            r"^\s*@(?:login_required|permission_required|csrf_exempt|staff_member_required|user_passes_test|api_key_required|token_required)\b",
            "Auth / CSRF decorator",
        ),
        (
            r"^\s*(?:from|import)\s+(?:bcrypt|cryptography|jose|jwt|passlib|authlib|django_otp|oauthlib|pyotp|argon2)\b",
            "Security library import",
        ),
        (
            r"\b(?:check_password|make_password|compare_digest|pbkdf2_hmac|constant_time_compare)\s*\(",
            "Password / constant-time function call",
        ),
        (r"\bbcrypt\.(?:hashpw|checkpw|gensalt)\s*\(", "bcrypt call"),
        (r"\bhmac\.(?:compare_digest|new)\s*\(", "HMAC operation"),
        (r"\bjwt\.(?:encode|decode|get_unverified_claims)\s*\(", "JWT encode/decode"),
        (r"\b(?:AES|RSA|Fernet|Ed25519|X25519)\.", "Cryptographic primitive class"),
    ]
)

# Go: security-critical std + popular libraries.
_SECURITY_PATTERNS_GO: List[tuple] = _compile_content_patterns(
    [
        (
            r'"(?:crypto/subtle|crypto/rand|crypto/hmac|crypto/rsa|crypto/ecdsa|crypto/tls|crypto/x509)"',
            "Crypto stdlib import",
        ),
        (
            r'"(?:golang\.org/x/crypto/bcrypt|golang\.org/x/crypto/argon2|golang\.org/x/crypto/scrypt)"',
            "Password hashing library import",
        ),
        (
            r'"(?:github\.com/golang-jwt/jwt|github\.com/dgrijalva/jwt-go|github\.com/lestrrat-go/jwx)',
            "JWT library import",
        ),
        (r"\bsubtle\.ConstantTimeCompare\s*\(", "Constant-time comparison"),
        (r"\bbcrypt\.(?:CompareHashAndPassword|GenerateFromPassword)\s*\(", "bcrypt operation"),
        (r"\bjwt\.(?:Parse|ParseWithClaims|Sign|New|NewWithClaims)\b", "JWT operation"),
        (r"\bmiddleware\.(?:BasicAuth|JWTAuth|RequireAuth)\b", "Auth middleware"),
    ]
)

# TypeScript / JavaScript (shared): Node + React + browser auth patterns.
_SECURITY_PATTERNS_TSJS: List[tuple] = _compile_content_patterns(
    [
        # Imports / requires from auth/security packages
        (
            r"(?:from\s+|require\s*\(\s*)['\"](?:jsonwebtoken|bcrypt(?:js)?|passport(?:-[\w-]+)?|express-session|next-auth|@auth0/[\w-]+|@okta/[\w-]+|firebase/auth|@clerk/[\w-]+|iron-session|cookie-session|csurf|helmet|express-rate-limit|argon2|scrypt-kdf)['\"]",
            "Auth/security npm package import",
        ),
        # JWT / bcrypt function calls
        (r"\b(?:jwt\.(?:sign|verify|decode)|bcrypt\.(?:compare|hash|genSalt))\s*\(", "JWT / bcrypt call"),
        # Browser credential storage — strong signal for XSS/exfil risk
        (
            r"(?:localStorage|sessionStorage)\.(?:setItem|getItem)\s*\(\s*['\"](?:token|auth|session|jwt|accessToken|refreshToken|apiKey)",
            "Browser-storage credential (XSS exfil surface)",
        ),
        (r"document\.cookie\s*[=+]", "Direct cookie write"),
        # React auth components / hooks
        (
            r"<(?:AuthGuard|ProtectedRoute|RequireAuth|RoleGuard|PrivateRoute|AuthProvider)\b",
            "React auth wrapper component",
        ),
        (r"\b(?:useAuth|useSession|useUser|useClerk|useAuth0)\s*\(", "Auth React hook"),
        # Passport / middleware
        (r"\bpassport\.authenticate\s*\(", "Passport strategy invocation"),
        # CSRF / CORS middleware
        (r"\b(?:csrf|csurf|helmet|cors)\s*\(\s*\{?", "Security middleware invocation"),
    ]
)

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
        (r"(?:whitelist|allowlist|blocklist|denylist|blacklist)", "Allow/deny list concept"),
        (r"(?:firewall|ratelimit|rate_limit|throttl\w*)", "Firewall / rate limit concept"),
        (r"\b(?:allowed_ips?|denied_ips?|trusted_ips?|blocked_ips?)\b", "IP allow/deny list"),
        (
            r"\bcsrf[-_]?token\b|\bcsrf_exempt\b|\bSameSite\b|\bHttpOnly\b|\bSecure\s*[;=]",
            "Cookie / CSRF security attribute",
        ),
        (r"\b(?:Bearer |Basic )\s+?\{?[A-Za-z0-9._-]+\}?", "HTTP Authorization scheme literal"),
    ],
    case_insensitive=True,
)

# Reliability content patterns — DDL / migration SQL that may ship
# outages regardless of whether the file sits in a /migrations/ dir.
_RELIABILITY_CONTENT_PATTERNS: List[tuple] = _compile_content_patterns(
    [
        (r"\bALTER\s+TABLE\b.*?\b(?:ADD|DROP|ALTER|RENAME)\s+COLUMN\b", "DDL column change (lock / rewrite risk)"),
        (r"\bDROP\s+(?:TABLE|INDEX|CONSTRAINT|VIEW)\b", "Destructive DDL"),
        (r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\b", "Index creation (potentially long lock)"),
    ]
)


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
        bucket.append(
            {
                "file": file_path,
                "line": line_no,
                "snippet": snippet[:120],  # truncate for log / prompt safety
                "reason": reason,
            }
        )

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
            "critical, the code added here is (triggers: " + ", ".join(reasons) + ")"
        )
        results.append(
            {
                "role": role,
                "reason": combined_reason,
                "matching_evidence": hits[role],
            }
        )
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
            requirements.append(
                {
                    "role": role,
                    "reason": reason,
                    "matching_paths": matches,
                    "_tier": 1,
                }
            )

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
# coordinator decides whether to actually fire dispatch_sweep
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
            triggers.append(
                {
                    "file": f.path,
                    "caller_files": caller_files_distinct[:10],
                    "caller_count": len(caller_files_distinct),
                    "hotspot_symbols": sorted(hotspot_symbols_set)[:10],
                }
            )

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
        ticket_context: str = "",
        prior_review_context: str = "",
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
        # Pre-fetched Jira tickets + Confluence pages referenced by this PR,
        # already flattened to markdown by the ADO router. Injected into
        # both the coordinator query and the P11 verifier prefix so a single
        # fetch serves every downstream call.
        self._ticket_context = ticket_context or ""
        # Second-pass re-review context (prior comments + verified status).
        # Empty for a normal first-pass review.
        self._prior_review_context = prior_review_context or ""

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

        # Stable session id for BOTH the Fact Vault filename AND task telemetry: it
        # folds the human-readable task_id (e.g. "ado-Abound-pr-12345") in as a
        # prefix so the whole task tree is queryable by PR. Computed unconditionally
        # (even when the scratchpad is disabled) so telemetry always links.
        if task_id:
            slug = _re.sub(r"[^A-Za-z0-9._-]+", "-", task_id).strip("-")[:48] or "pr"
            self._session_id = f"{slug}-{_uuid.uuid4().hex[:8]}"
        else:
            self._session_id = f"pr-{_uuid.uuid4().hex[:12]}"

        self._owns_scratchpad = False
        if _os.environ.get("CONDUCTOR_SCRATCHPAD_ENABLED", "1") != "0" and scratchpad is None:
            scratchpad = FactStore.open(self._session_id, workspace=workspace_path, task_id=task_id)
            self._owns_scratchpad = True
        elif scratchpad is not None:
            # Caller-supplied scratchpad — adopt its session id for telemetry parity.
            self._session_id = getattr(scratchpad, "session_id", None) or self._session_id
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
        # dispatch_verify, replans on unexpected observations, and
        # synthesises with unified severity classification.
        async for event in self._run_v2_coordinator(
            pr_context,
            risk_profile,
            file_diffs,
            impact_context,
            budget_multiplier,
            start_time,
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
          * tools = read-only survey tools + ``dispatch_verify`` (the
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
        from .task_telemetry import record_complete, record_start

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

        # Pre-flight USD budget consult (BudgetEconomics). PR review is a MANDATORY
        # consult: estimate a loose total ceiling + per-leaf cap from the diff size
        # before any worker is dispatched. The hard anchor is a 2000-line PR → $50
        # total; caps are loose safety circuit-breakers (a real leaf spends
        # ~$0.05-0.50), so they never throttle a normal review — they only stop a
        # runaway loop. The per-leaf cap rides the SDK leaf via max_budget_usd; the
        # total cap is recorded for monitoring + future coordinator-level cutoff.
        budget_plan = get_budget_economics().estimate(
            "pr",
            TaskSignals(
                diff_lines=pr_context.total_changed_lines,
                expected_leaves=max(1, pr_context.file_count),
            ),
        )
        logger.info("[PR Brain v2] Budget plan: %s", budget_plan.to_dict())
        yield WorkflowEvent("budget_plan", budget_plan.to_dict())

        budget_mgr = BrainBudgetManager(
            self._config.limits.total_session_tokens,
        )
        llm_semaphore = asyncio.Semaphore(self._config.limits.llm_concurrency_limit)

        # Telemetry root (06d): one ``pr_review`` row anchoring this review's task
        # tree. The coordinator + every dispatched worker link to it via
        # parent_task_id / root_task_id (all = self._session_id), so the whole-PR
        # token + USD total is a single SUM over root_task_id, queryable by the
        # human PR id embedded in session_id. Best-effort (no-op without a DB).
        await record_start(
            task_id=self._session_id,
            root_task_id=self._session_id,
            session_id=self._session_id,
            kind="pr_review",
            agent_name="pr_review_root",
            engine="orchestrator",
            query=self._pr_title or None,
        )

        executor_cfg = BrainExecutorConfig(
            workspace_path=self._workspace_path,
            current_depth=0,
            max_depth=self._config.limits.max_depth,
            max_concurrent=self._config.limits.max_concurrent_agents,
            sub_agent_timeout=self._config.limits.sub_agent_timeout,
            # BudgetEconomics per-leaf cap → every SDK leaf dispatched in this review.
            leaf_max_usd=budget_plan.per_leaf_max_usd,
        )

        executor = AgentToolExecutor(
            inner_executor=self._tool_executor,
            agent_registry=self._agent_registry,
            swarm_registry=swarm_registry,
            agent_provider=self._explorer_provider,  # haiku for sub-agents
            strong_provider=self._provider,  # sonnet = the Brain itself
            config=executor_cfg,
            brain_config=brain_config,
            trace_writer=self._trace_writer,
            event_sink=self._event_sink,
            budget_manager=budget_mgr,
            llm_semaphore=llm_semaphore,
            task_id=self._session_id,
            root_task_id=self._session_id,
            session_id=self._session_id,
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
                    executor,
                    pr_context,
                    file_diffs,
                ):
                    yield ev
                existence_summary = self._format_existence_summary_for_coordinator()
            except Exception as exc:
                logger.warning(
                    "[PR Brain v2] Phase 2 existence check failed (non-fatal): %s",
                    exc,
                )

        # Build the coordinator's task — diff + impact + coordinator skill.
        coordinator_query = self._build_v2_coordinator_query(
            pr_context,
            risk_profile,
            file_diffs,
            impact_context,
            existence_summary=existence_summary,
        )

        # Dispatch the Brain itself via dynamic-compose. It gets a tool pool
        # including dispatch_verify, read-only survey tools, and runs the
        # 5-phase loop under the pr_brain_coordinator skill's direction.
        coordinator_tools = [
            "grep",
            "read_file",
            "find_symbol",
            "file_outline",
            "get_callers",
            "get_callees",
            "get_dependencies",
            "git_diff",
            "git_diff_files",
            "git_show",
            "git_log",
            "dispatch_verify",
            "dispatch_sweep",
        ]

        coordinator_params = {
            "perspective": (
                "You are the PR Brain coordinator. You survey the diff, "
                "plan focused investigations, dispatch scope-bounded "
                "sub-agents via dispatch_verify, and synthesize the "
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
            "dispatch_explore",
            coordinator_params,
        )

        logger.info(
            "[PR Brain v2] Coordinator loop done: success=%s",
            coordinator_result.success,
        )

        # Parse the coordinator's final answer into ReviewFindings + synthesis.
        review_output = self._parse_v2_coordinator_output(
            coordinator_result,
            pr_context,
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

        if _os_v2phase6.environ.get("CONDUCTOR_PR_BRAIN_V2_SKIP_VERIFY", "0") != "1" and review_output["findings"]:
            try:
                review_output = await self._apply_v2_precision_filter(
                    executor,
                    review_output,
                    pr_context,
                    file_diffs,
                )
            except Exception as exc:
                logger.warning(
                    "[PR Brain v2] Precision filter failed (non-fatal): %s",
                    exc,
                )

        yield WorkflowEvent(
            "v2_coordinator_complete",
            {
                "finding_count": len(review_output["findings"]),
            },
        )

        # Phase 9.17 lifecycle hook — synthesis finished, precision
        # filter has run, findings + final synthesis text are ready.
        # Consumers: telemetry export / extract reusable
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

        # Token + USD totals for the review stats. The coordinator's own
        # ``budget_summary`` only covers ITS in-house turns — leaf SDK workers run
        # as separate subprocesses and report into ``budget_mgr`` instead. So the
        # reliable session total is the budget manager's aggregate (coordinator +
        # every dispatched sub-agent), with the coordinator's own summary as a
        # floor/fallback.
        _total_iterations = 0
        _coord_tokens = 0
        if isinstance(coordinator_result.data, dict):
            _total_iterations = coordinator_result.data.get("iterations", 0)
            budget_summary = coordinator_result.data.get("budget_summary")
            if isinstance(budget_summary, dict):
                _coord_tokens = int(budget_summary.get("total_tokens", 0) or 0)
        _total_tokens = max(_coord_tokens, budget_mgr.total_tokens_used)
        _total_cost_usd = budget_mgr.total_cost_usd

        # Close the telemetry root row. Own-usage stays 0 (the anchor holds no LLM
        # turns of its own) — the PR total is the SUM over root_task_id across the
        # tree, exactly _total_tokens / _total_cost_usd reported here.
        await record_complete(
            task_id=self._session_id,
            status="done",
            duration_ms=duration_ms,
            iterations=_total_iterations,
        )

        yield WorkflowEvent(
            "done",
            {
                "answer": review_output["synthesis"],
                "findings": review_output["findings"],
                "files_reviewed": sorted(files_reviewed_set),
                "merge_recommendation": review_output["merge_recommendation"],
                "duration_ms": duration_ms,
                "total_tokens": _total_tokens,
                "total_cost_usd": _total_cost_usd,
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
            "v2_phase2_start",
            {"phase": "existence_verification"},
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
        extensions = {Path(f.path).suffix.lower() for f in pr_context.files if f.path}
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
                    # Cross-check the symbol index before declaring a phantom.
                    # P13's per-language scanners only see the diff's `+` lines
                    # plus a same-package grep, so a reference to a symbol
                    # DEFINED elsewhere (e.g. a pre-existing same-file
                    # `static final` constant referenced as `CONST.method()`,
                    # which P13's Java pattern mistakes for a class) gets
                    # false-flagged. find_symbol queries the whole tree-sitter
                    # index (now incl. constants/fields), so a hit means the
                    # symbol really exists -> suppress. Best-effort: any error
                    # falls through to the original flag-it behaviour.
                    try:
                        from app.code_tools.tools import find_symbol as _find_symbol

                        _fs = _find_symbol(self._workspace_path, name)
                        if _fs.success and _fs.data:
                            logger.info(
                                "[PR Brain v2] P13 phantom '%s' suppressed — " "find_symbol found %d definition(s)",
                                name,
                                len(_fs.data),
                            )
                            p13_handled_names.add(name)
                            return
                    except Exception as exc:
                        logger.debug(
                            "[PR Brain v2] P13 find_symbol cross-check failed " "for %s (proceeding to flag): %s",
                            name,
                            exc,
                        )
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
                        p13_missing_details.append(
                            {
                                "name": name,
                                "kind": kind,
                                "referenced_at": found["referenced_at"],
                            }
                        )
                        added_from_ast += 1
                        missing_count += 1
                    except Exception as exc:
                        logger.debug(
                            "[PR Brain v2] P13 put_existence failed for %s: %s",
                            name,
                            exc,
                        )

                for found in _scan_new_python_imports_for_missing(
                    self._workspace_path,
                    file_diffs,
                ):
                    _inject_phantom(found, kind="import")

                for found in _scan_new_go_references_for_missing(
                    self._workspace_path,
                    file_diffs,
                ):
                    _inject_phantom(found, kind="reference")

                for found in _scan_new_java_references_for_missing(
                    self._workspace_path,
                    file_diffs,
                ):
                    _inject_phantom(found, kind="class")
            except Exception as exc:
                logger.warning(
                    "[PR Brain v2] P13 deterministic scan failed " "(non-fatal): %s",
                    exc,
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
                    f"- `{d['name']}` (kind={d['kind']}, at `{d['referenced_at']}`)" for d in p13_missing_details[:40]
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
                executor.execute("dispatch_explore", params),
                timeout=float(_PHASE2_TIMEOUT_SECONDS),
            )
        except TimeoutError:
            logger.warning(
                "[PR Brain v2] existence-check LLM worker hit %ds "
                "wall-clock timeout. P13 facts already persisted (%d "
                "missing symbols); coordinator proceeds with those.",
                _PHASE2_TIMEOUT_SECONDS,
                added_from_ast,
            )
            llm_timeout = True
            result = None

        if result is not None and not result.success:
            logger.warning(
                "[PR Brain v2] existence-check dispatch failed: %s",
                result.error,
            )
            llm_error = str(result.error)
            result = None

        if result is not None:
            condensed = result.data or {}
            raw_answer = condensed.get("answer") or condensed.get("final_answer") or ""
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
            added_from_ast,
            len(llm_symbols),
            missing_count,
            llm_timeout,
            llm_error,
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
            lines.append(
                "**MANDATORY**: your final findings JSON MUST include one "
                "entry per missing symbol, pointing at the REFERENCE "
                "site (not where the symbol 'would' be defined), with "
                "title of the form 'ImportError at runtime: {name} "
                "not defined in codebase'. Severity = `critical`. "
                "Category = `correctness`. Confidence = `0.99`."
            )
            lines.append("")
            lines.append(
                "**DO NOT** speculate about what the non-existent "
                "symbol 'would have done'. Do NOT emit findings "
                "about negative offsets, null checks, or any logic "
                "inside a phantom class. The class does not exist — "
                "the ImportError IS the bug. Stop there."
            )
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
            lines.append(
                "  \"evidence\": [\"grep 'class <SYMBOL>' / 'def <SYMBOL>' returned 0 matches in the codebase\"],"
            )
            lines.append('  "risk": "Every call path that loads <FILE> raises ImportError/NameError at runtime.",')
            lines.append(
                '  "suggested_fix": "Either define <SYMBOL> in the imported module, or remove the reference. The current PR is unshippable as written.",'
            )
            lines.append('  "category": "correctness"')
            lines.append("}")
            lines.append("```")
            lines.append("")
            lines.append("**Missing symbols (one finding each — do not merge, do not skip):**")
            lines.append("")
            for m in missing:
                ref = m.referenced_at or "(unknown)"
                ev = (m.evidence or "").strip()[:200]
                lines.append(f"- `{m.symbol_name}` ({m.symbol_kind}) referenced at `{ref}` — evidence: {ev}")
            lines.append("")

        if present:
            sig_mismatch = [p for p in present if p.signature_info and p.signature_info.get("missing_params")]
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
                    lines.append(f"- `{m.symbol_name}` at `{m.referenced_at}` — " f"missing params: {missing_params}")
                lines.append("")
            other_present = [p for p in present if p not in sig_mismatch]
            if other_present:
                lines.append(
                    f"**{len(other_present)} other symbol(s) verified present.** "
                    f'Use `search_facts(kind="existence", symbol="X")` to '
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
        lines.append(f"Files changed: {pr_context.file_count}  " f"Lines changed: {pr_context.total_changed_lines}")
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
                "dispatch_verify check questions — every check should map "
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

        # Jira tickets + Confluence pages referenced by this PR, pre-fetched
        # via the readonly service-account path. The coordinator treats
        # these as authoritative statement of *requirements* — see the
        # pr_brain_coordinator skill for how to use them.
        if self._ticket_context:
            lines.append("## Linked tickets & docs (authoritative requirements)")
            lines.append("")
            lines.append(self._ticket_context)
            lines.append("")

        # Second-pass re-review: prior comments + their verified resolution
        # status. Tells the coordinator what was already raised so it doesn't
        # re-report genuinely-fixed items and concentrates on still-open ones +
        # regressions the fixes introduced.
        if self._prior_review_context:
            lines.append("## Prior review — second pass")
            lines.append("")
            lines.append(self._prior_review_context)
            lines.append("")

        lines.append("## Files in diff")
        lines.append("")
        for f in pr_context.files:
            lines.append(f"- `{f.path}`  (+{f.additions} −{f.deletions}, " f"{f.status}, category={f.category.value})")
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
                lines.append(f"### `role=\"{req['role']}\"` — REQUIRED ({tier_label})")
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
                        lines.append(f"- `{ev['file']}:{ev['line']}` — {ev['reason']} " f"— `{snippet[:80]}`")
                    lines.append("")
            logger.info(
                "[PR Brain v2] Mandatory-dispatch Phase 1 detected %d " "required role(s) across 2 tiers: %s",
                len(required),
                ", ".join(f"{r['role']}(T{r.get('_tier', 1)})" for r in required),
            )

        # P12b — Dimension-worker trigger hints. OPT-IN, not mandatory.
        # These surface changed files whose cross-file caller footprint is
        # large enough that file-range dispatch would split the pattern.
        # The coordinator decides whether to actually fire
        # dispatch_sweep; we just tell it "here's where
        # a cross-file sweep would pay off".
        dim_triggers = _detect_dimension_triggers(
            self._workspace_path,
            pr_context,
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
                f"`dispatch_sweep` (not mandatory). "
                f"You may fire **up to {dim_cap} dimension worker(s)** "
                f"for this PR — reserve them for cases where a pattern "
                f"(new contract, signature change, shared middleware "
                f"edit) must be verified at every caller site, and a "
                f"bunch of narrow scoped dispatches would miss the "
                f'cross-cut. `model_tier="explorer"` default @ 150K '
                f'budget; escalate to `model_tier="strong"` only when '
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
                if trig["hotspot_symbols"]:
                    lines.append("- Hotspot symbols: " f"{', '.join(f'`{s}`' for s in trig['hotspot_symbols'][:5])}")
                lines.append("")
            logger.info(
                "[PR Brain v2] P12b dimension triggers: %d candidate file(s), " "cap=%d: %s",
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
            f"- PR size: **{size_label}** ({n_files} files, " f"{pr_context.total_changed_lines} lines changed)"
        )
        lines.append(f"- Hard cap: **{dispatch_cap} dispatches** across all replan rounds")
        if size_label == "large":
            lines.append(
                "- Cluster first: group files by feature/intent in Survey, " "then dispatch 1-2 role agents per cluster"
            )
        else:
            lines.append("- Small PR: 1-3 targeted dispatches typically suffice. " "Don't pad.")
        lines.append("")

        lines.append("## Your task")
        lines.append("")
        lines.append(
            "Run your 5-phase coordinator loop (Survey → Plan → Execute → "
            "Replan → Synthesize). Use read-only tools for the Survey. "
            "Dispatch scope-bounded investigations via dispatch_verify "
            f"(≤5 files per dispatch, ≤{dispatch_cap} total dispatches). "
            "Two dispatch modes available — pick per investigation: "
            "(a) `checks=[q1, q2, q3]` for localised suspicions where "
            'you have concrete yes/no questions; (b) `role="security"|'
            '"correctness"|"concurrency"|"reliability"|"performance"|'
            '"test_coverage"` + `direction_hint="..."` for specialist '
            "deep-dive on a risk dimension. You may combine: "
            '`role="security", checks=[...]`. '
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
        lines.append(
            '    "category": "correctness | security | reliability | concurrency | performance | test_coverage"'
        )
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
                "[PR Brain v2] Injected %d missing-symbol finding(s) " "that coordinator omitted",
                injected_count,
            )

        # Step 0b-2: P14 — inject findings for stub-function call sites
        # detected mechanically from the diff. For each (stub_def,
        # caller) pair found by _scan_for_stub_call_sites, if the
        # coordinator didn't already flag the site, synthesize a
        # finding. Guards against coordinator missing multi-site stub
        # bugs (grafana-009 class).
        findings, stub_injected = _inject_stub_caller_findings(
            findings,
            file_diffs,
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
                "[PR Brain v2] Reflection pass dropped %d finding(s) " "whose premise contradicts Phase 2 facts",
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
            len(direct),
            len(unclear),
            len(low),
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
            pr_context,
            file_diffs,
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
            len(confirmed_from_verifier),
            refuted_count,
            len(unclear_after_verify),
        )

        final_findings = direct + confirmed_from_verifier
        secondary = unclear_after_verify + low

        # Step 6: per-finding diff-scope verification (P11 cheap).
        # Inspired by UltraReview's "every finding independently verified".
        # Mechanical LLM-free check: a finding targeting a file outside
        # the PR diff is almost always a coordinator hallucination. Move
        # such findings to secondary_notes instead of emitting.
        final_findings, scope_demoted, scope_demoted_count = _filter_findings_to_diff_scope(final_findings, file_diffs)
        if scope_demoted_count:
            logger.info(
                "[PR Brain v2] Diff-scope filter demoted %d finding(s) " "whose file is not in the PR diff",
                scope_demoted_count,
            )
            secondary = scope_demoted + secondary

        # Step 6b: drop findings that describe the PR's own FIX as a defect.
        # A bug living only on the `-` (removed) lines was deleted by this PR;
        # flagging it (esp. as high/critical + Request Changes) tells the author
        # to fix what they already fixed. Conservative, LLM-free: only demotes a
        # finding whose own text self-contradicts ("the PR correctly fixes this"
        # / "old code" + "new code"). The coordinator skill is the primary guard;
        # this is the mechanical backstop. (Real bug: PR 14442 NatWest finding.)
        final_findings, fix_demoted, fix_demoted_count = _filter_findings_describing_own_fix(final_findings)
        if fix_demoted_count:
            logger.info(
                "[PR Brain v2] Fix-as-defect filter demoted %d finding(s) "
                "that describe the PR's own fix as an issue",
                fix_demoted_count,
            )
            secondary = fix_demoted + secondary

        # Append secondary notes to synthesis as a "Secondary observations"
        # block. They don't enter the findings array → don't count against
        # precision / recall in the eval scorer.
        synthesis = review_output.get("synthesis", "")
        if secondary:
            secondary_block_lines = [
                "",
                "---",
                "",
                "## Secondary observations (not scored, low-confidence or " "unverified)",
                "",
            ]
            for s in secondary:
                title = s.get("title", "(untitled)")
                file_ = s.get("file", "")
                line = s.get("start_line", "")
                conf = s.get("confidence", "")
                secondary_block_lines.append(f"- **{title}** — `{file_}:{line}` (conf={conf})")
            synthesis = synthesis + "\n".join(secondary_block_lines)

        # Recompute the merge recommendation from the SURVIVING findings. The
        # original was computed before precision filtering / fix-as-defect /
        # scope demotion; if those removed the blocking finding(s), the vote must
        # relax accordingly (else we'd Request Changes with zero real defects —
        # exactly the PR 14442 false -5). Mirrors code_review.shared logic on dicts.
        merge_rec = _recompute_merge_recommendation(
            final_findings, review_output.get("merge_recommendation", "comment")
        )

        return {
            **review_output,
            "findings": final_findings,
            "synthesis": synthesis,
            "merge_recommendation": merge_rec,
            "_precision_filter_stats": {
                "direct_findings": len(direct),
                "unclear_input": len(unclear),
                "confirmed_by_verifier": len(confirmed_from_verifier),
                "refuted_by_verifier": refuted_count,
                "still_unclear": len(unclear_after_verify),
                "low_confidence": len(low),
                "reflection_dropped": reflection_drops,
                "diff_scope_demoted": scope_demoted_count,
                "fix_as_defect_demoted": fix_demoted_count,
            },
        }

    def _build_verifier_system_prefix(
        self,
        pr_context: PRContext,
        file_diffs: Dict[str, str],
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
            ticket_context=self._ticket_context,
        )
        return f"{skill_text}\n\n{ctx_prefix}".strip()

    async def _verify_single(
        self,
        finding: Dict[str, Any],
        file_diffs: Dict[str, str],
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
        self,
        unclear: List[Dict[str, Any]],
        file_diffs: Dict[str, str],
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
            default["synthesis"] = f"PR Brain v2 coordinator failed: {err}"
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
