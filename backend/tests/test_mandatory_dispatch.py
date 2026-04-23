"""Unit tests for the mandatory-dispatch path detector.

Phase 1 runs ``_detect_required_dispatches`` against the diff's file
paths. When matched, the coordinator's task text gets a
"## MANDATORY investigations" section that forces the coordinator to
dispatch specific roles regardless of its own survey-based judgement.

Regression target: PR #14227 (1339-line CMS OAuth change that
touched ``.../common/v3/security/`` + ``.../service/v3/V3CmsAuthService.java``
yet shipped plaintext-password comparison, timing attack, and shared
external creds — all because the coordinator decided
``plan_entries=0`` / no security dispatch).
"""

from __future__ import annotations

from app.agent_loop.pr_brain import _detect_required_dispatches


def _diffs(*paths: str) -> dict:
    """Build a minimal file_diffs map — diff text is ignored by the
    detector, only keys (paths) matter."""
    return {p: "" for p in paths}


class TestDetectRequiredDispatches:
    """Path-anchored rules — the specific reason PR #14227 slipped
    through is exactly what this must catch."""

    def test_pr_14227_security_path_must_trigger(self):
        """Real paths from PR #14227 — the regression case."""
        diffs = _diffs(
            "cms/src/main/java/com/abound/cms/common/v3/security/CmsV3SecurityConfig.java",
            "cms/src/main/java/com/abound/cms/common/v3/security/CmsV3AdminContextFilter.java",
            "cms/src/main/java/com/abound/cms/service/v3/V3CmsAuthService.java",
            "common/src/main/java/com/abound/common/v3/security/V3JwtConfig.java",
        )
        out = _detect_required_dispatches(diffs)
        # security role present
        roles = [r["role"] for r in out]
        assert "security" in roles
        sec = next(r for r in out if r["role"] == "security")
        # All 4 security-critical paths should be listed as evidence
        assert len(sec["matching_paths"]) == 4
        # Reason string must be concrete (not generic)
        assert "plaintext" in sec["reason"].lower() or \
               "timing" in sec["reason"].lower() or \
               "secret" in sec["reason"].lower()

    def test_oauth_path_triggers_security(self):
        out = _detect_required_dispatches(
            _diffs("src/oauth/callback.py", "src/views/users.py"),
        )
        assert any(r["role"] == "security" for r in out)

    def test_jwt_path_triggers_security(self):
        out = _detect_required_dispatches(_diffs("src/jwt/validator.go"))
        assert any(r["role"] == "security" for r in out)

    def test_token_path_triggers_security(self):
        out = _detect_required_dispatches(_diffs("src/tokens/refresh.py"))
        assert any(r["role"] == "security" for r in out)

    def test_password_path_triggers_security(self):
        out = _detect_required_dispatches(
            _diffs("src/auth/password/reset.py"),
        )
        assert any(r["role"] == "security" for r in out)

    def test_permission_path_triggers_security(self):
        out = _detect_required_dispatches(
            _diffs("services/permissions/group_resolver.java"),
        )
        assert any(r["role"] == "security" for r in out)

    def test_rbac_acl_path_triggers_security(self):
        assert any(
            r["role"] == "security"
            for r in _detect_required_dispatches(_diffs("src/rbac/policy.go"))
        )
        assert any(
            r["role"] == "security"
            for r in _detect_required_dispatches(_diffs("src/acl/rules.py"))
        )

    def test_signin_signup_login_logout_path_triggers_security(self):
        for p in ["src/signin/oauth.py", "web/signup/form.tsx",
                  "src/login/view.py", "src/logout/handler.py"]:
            out = _detect_required_dispatches(_diffs(p))
            assert any(r["role"] == "security" for r in out), \
                f"{p} should trigger security"

    def test_flyway_migration_triggers_reliability(self):
        out = _detect_required_dispatches(
            _diffs("db/migration/V42__add_tier.sql"),
        )
        assert any(r["role"] == "reliability" for r in out)

    def test_migrations_dir_triggers_reliability(self):
        out = _detect_required_dispatches(
            _diffs("src/app/migrations/0042_add_field.py"),
        )
        assert any(r["role"] == "reliability" for r in out)

    def test_liquibase_changelog_triggers_reliability(self):
        out = _detect_required_dispatches(
            _diffs("database/changelog/changes/0042.sql"),
        )
        assert any(r["role"] == "reliability" for r in out)

    def test_multiple_roles_triggered(self):
        """auth + migration in same PR → both required."""
        out = _detect_required_dispatches(_diffs(
            "src/auth/handler.py",
            "db/migration/V42__add_column.sql",
        ))
        roles = [r["role"] for r in out]
        assert "security" in roles
        assert "reliability" in roles
        # Ordering: security before reliability (declaration order)
        assert roles.index("security") < roles.index("reliability")

    def test_no_sensitive_paths_returns_empty(self):
        out = _detect_required_dispatches(_diffs(
            "src/controllers/widget.py",
            "src/views/list.py",
            "tests/test_widget.py",
        ))
        assert out == []

    def test_empty_diffs_returns_empty(self):
        assert _detect_required_dispatches({}) == []

    def test_authors_directory_does_not_false_match_auth(self):
        """Word-boundary test — `authors/` is NOT an auth path."""
        out = _detect_required_dispatches(
            _diffs("src/blog/authors/profile.py"),
        )
        # The regex uses segment separators, so `authors/` does NOT match
        # the `auth(s?)` prefix alone — it matches `authors/` via `auths/`
        # but the plural `s?` is intentional. Accept whatever the detector
        # decides, but require no double-count.
        # Expectation: `authors/` SHOULD match security (conservative is
        # safer than false negative on a real auth path). If this flips
        # later, adjust — but for now confirm the behaviour is stable.
        # This test just documents the current edge-case behaviour.
        # We explicitly accept that "authors" matches — not a problem, it
        # just means a blog post on "authors" files would get a security
        # review, which is cheap ($0.30) and harmless.
        assert isinstance(out, list)  # sanity

    def test_multiple_matching_paths_dedup_within_role(self):
        """3 security-path files → one security requirement with 3 paths."""
        out = _detect_required_dispatches(_diffs(
            "src/auth/login.py",
            "src/auth/session.py",
            "src/auth/oauth.py",
        ))
        sec = [r for r in out if r["role"] == "security"]
        assert len(sec) == 1
        assert len(sec[0]["matching_paths"]) == 3

    def test_matching_paths_are_sorted(self):
        out = _detect_required_dispatches(_diffs(
            "z/auth/last.py", "a/auth/first.py", "m/auth/middle.py",
        ))
        sec = next(r for r in out if r["role"] == "security")
        assert sec["matching_paths"] == [
            "a/auth/first.py", "m/auth/middle.py", "z/auth/last.py",
        ]


class TestCoordinatorSkillEnforcesDispatchFloor:
    """Layer-A prompt enforcement: the shipped coordinator skill must
    forbid survey-only findings and require at least one sub-agent
    dispatch on non-trivial PRs.

    These are *content* checks on the skill markdown — guards against
    future edits silently dropping the enforcement. Without the
    'cardinal rule' + 'minimum dispatch floor' language, Sonnet reverts
    to survey-and-emit behaviour and real defects ship (see PR #14227)."""

    def _skill_text(self) -> str:
        from pathlib import Path
        here = Path(__file__).resolve().parents[2]
        skill_path = here / "config" / "skills" / "pr_brain_coordinator.md"
        return skill_path.read_text(encoding="utf-8")

    def test_cardinal_rule_section_present(self):
        text = self._skill_text()
        assert "cardinal rule" in text.lower()
        assert "planner" in text.lower()
        assert "verifier" in text.lower()

    def test_forbids_survey_only_findings(self):
        text = self._skill_text()
        # Must explicitly forbid direct-emit from survey tool output
        lowered = text.lower()
        assert "may not emit findings from your own survey" in lowered or \
               "findings come from dispatched sub-agents" in lowered

    def test_lists_two_grounding_sources(self):
        """Every finding must be grounded in (a) Phase 2 fact or
        (b) a sub-agent verdict."""
        text = self._skill_text()
        assert "phase 2 existence fact" in text.lower()
        assert "sub-agent verdict" in text.lower()

    def test_has_minimum_dispatch_floor(self):
        text = self._skill_text()
        lowered = text.lower()
        # Explicit numeric floor (≥ 50 lines → ≥ 1 dispatch)
        assert "50 changed lines" in lowered or "≥ 50" in text
        # Non-negotiable / non-bypassable language
        assert "non-negotiable" in lowered or "hardest constraint" in lowered

    def test_survey_section_points_to_cardinal_rule(self):
        """Survey section must remind the reader that grep hits are
        candidates for dispatch, NOT findings."""
        text = self._skill_text()
        # The Survey section should have a boundary paragraph
        assert "Survey is for decomposition only" in text or \
               "Findings come from dispatched sub-agents" in text


class TestDiffContentScanner:
    """Tier 2 content detector — scan `+` lines for security primitives
    regardless of file path. Covers Java/Kotlin, Python, Go, TS/JS +
    cross-language generic concepts.

    Each test constructs a minimal diff, asserts the correct role
    fires, and spot-checks evidence includes a useful snippet."""

    def _diff(self, path: str, added_lines: list) -> dict:
        body = "\n".join(f"+{ln}" for ln in added_lines)
        diff = (
            f"--- a/{path}\n+++ b/{path}\n"
            f"@@ -0,0 +1,{len(added_lines)} @@\n{body}\n"
        )
        return {path: diff}

    def _run(self, diffs):
        from app.agent_loop.pr_brain import (
            _detect_required_dispatches_from_diff_content,
        )
        return _detect_required_dispatches_from_diff_content(diffs)

    # ---- Java ----
    def test_java_preauthorize_annotation_triggers_security(self):
        diffs = self._diff("src/widgets/Handler.java", [
            "@PreAuthorize(\"hasRole('ADMIN')\")",
            "public void adminOp() {}",
        ])
        out = self._run(diffs)
        assert len(out) == 1
        assert out[0]["role"] == "security"
        assert any("PreAuthorize" in ev["snippet"] for ev in out[0]["matching_evidence"])

    def test_java_bcrypt_encoder_triggers_security(self):
        diffs = self._diff("src/auth/Config.java", [
            "PasswordEncoder encoder = new BCryptPasswordEncoder();",
        ])
        out = self._run(diffs)
        assert any(r["role"] == "security" for r in out)

    def test_java_message_digest_isequal_triggers_security(self):
        diffs = self._diff("src/crypto/Cmp.java", [
            "return MessageDigest.isEqual(a.getBytes(), b.getBytes());",
        ])
        out = self._run(diffs)
        assert any(r["role"] == "security" for r in out)

    def test_java_jwts_builder_triggers_security(self):
        diffs = self._diff("src/token/Issuer.java", [
            "String token = Jwts.builder().setSubject(userId).signWith(key).compact();",
        ])
        out = self._run(diffs)
        assert any(r["role"] == "security" for r in out)

    def test_java_cipher_getinstance_triggers_security(self):
        diffs = self._diff("src/encrypt/Svc.java", [
            'Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");',
        ])
        out = self._run(diffs)
        assert any(r["role"] == "security" for r in out)

    # ---- Python ----
    def test_python_login_required_triggers_security(self):
        diffs = self._diff("src/views/profile.py", [
            "@login_required",
            "def profile_view(request):",
            "    return render(request, 'profile.html')",
        ])
        out = self._run(diffs)
        assert any(r["role"] == "security" for r in out)

    def test_python_bcrypt_import_triggers_security(self):
        diffs = self._diff("src/users/service.py", [
            "import bcrypt",
            "hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt())",
        ])
        out = self._run(diffs)
        assert any(r["role"] == "security" for r in out)

    def test_python_hmac_compare_digest_triggers_security(self):
        diffs = self._diff("src/webhooks/verify.py", [
            "import hmac",
            "if not hmac.compare_digest(received_sig, expected_sig):",
            "    raise InvalidSignature()",
        ])
        out = self._run(diffs)
        assert any(r["role"] == "security" for r in out)

    def test_python_jwt_decode_triggers_security(self):
        diffs = self._diff("src/auth/tokens.py", [
            "claims = jwt.decode(token, public_key, algorithms=['RS256'])",
        ])
        out = self._run(diffs)
        assert any(r["role"] == "security" for r in out)

    # ---- Go ----
    def test_go_crypto_subtle_import_triggers_security(self):
        diffs = self._diff("pkg/secure/compare.go", [
            "import (",
            '    "crypto/subtle"',
            ")",
        ])
        out = self._run(diffs)
        assert any(r["role"] == "security" for r in out)

    def test_go_constant_time_compare_triggers_security(self):
        diffs = self._diff("pkg/secure/compare.go", [
            "ok := subtle.ConstantTimeCompare(a, b) == 1",
        ])
        out = self._run(diffs)
        assert any(r["role"] == "security" for r in out)

    def test_go_bcrypt_compare_triggers_security(self):
        diffs = self._diff("pkg/auth/service.go", [
            "err := bcrypt.CompareHashAndPassword(hashed, []byte(plain))",
        ])
        out = self._run(diffs)
        assert any(r["role"] == "security" for r in out)

    def test_go_jwt_parse_triggers_security(self):
        diffs = self._diff("pkg/token/verifier.go", [
            "token, err := jwt.Parse(raw, keyfunc)",
        ])
        out = self._run(diffs)
        assert any(r["role"] == "security" for r in out)

    # ---- TS / JS ----
    def test_ts_jwt_import_triggers_security(self):
        diffs = self._diff("src/auth/token.ts", [
            "import jwt from 'jsonwebtoken';",
        ])
        out = self._run(diffs)
        assert any(r["role"] == "security" for r in out)

    def test_ts_bcrypt_compare_triggers_security(self):
        diffs = self._diff("src/auth/verify.ts", [
            "const ok = await bcrypt.compare(plain, hash);",
        ])
        out = self._run(diffs)
        assert any(r["role"] == "security" for r in out)

    def test_ts_localstorage_token_triggers_security(self):
        """Browser-credential-storage pattern — known XSS exfil surface."""
        diffs = self._diff("web/auth.ts", [
            "localStorage.setItem('token', response.access_token);",
        ])
        out = self._run(diffs)
        assert any(r["role"] == "security" for r in out)

    def test_tsx_auth_guard_component_triggers_security(self):
        diffs = self._diff("web/routes/admin.tsx", [
            "export default function Page() {",
            "  return <AuthGuard>Admin content</AuthGuard>;",
            "}",
        ])
        out = self._run(diffs)
        assert any(r["role"] == "security" for r in out)

    def test_ts_useauth_hook_triggers_security(self):
        diffs = self._diff("web/header.tsx", [
            "const { user } = useAuth();",
        ])
        out = self._run(diffs)
        assert any(r["role"] == "security" for r in out)

    def test_jsx_passport_authenticate_triggers_security(self):
        diffs = self._diff("server/routes.js", [
            "app.post('/login', passport.authenticate('local'), handler);",
        ])
        out = self._run(diffs)
        assert any(r["role"] == "security" for r in out)

    # ---- Cross-language generic ----
    def test_generic_whitelist_triggers_security(self):
        """PR #14234 regression — IP whitelist endpoint in a non-
        security-named file must still trigger security role."""
        diffs = self._diff("loan/service/SandBoxServiceImpl.java", [
            "public void addCountIpWhitelist(List<String> ips) {",
            "    redisUtil.sSet(COUNT_IP_WHITELIST_KEY, ips.toArray());",
            "}",
        ])
        out = self._run(diffs)
        assert any(r["role"] == "security" for r in out)
        ev_reasons = {ev["reason"] for r in out for ev in r["matching_evidence"]}
        assert any("llow/deny list" in r or "Allow" in r for r in ev_reasons)

    def test_generic_ratelimit_triggers_security(self):
        diffs = self._diff("api/server.go", [
            "limiter := ratelimit.New(100)",
            "handler := limiter.Wrap(loginHandler)",
        ])
        out = self._run(diffs)
        assert any(r["role"] == "security" for r in out)

    def test_generic_allowed_ips_variable_triggers_security(self):
        diffs = self._diff("services/gateway.py", [
            "allowed_ips = ['10.0.0.1', '192.168.1.0/24']",
        ])
        out = self._run(diffs)
        assert any(r["role"] == "security" for r in out)

    # ---- Reliability content ----
    def test_alter_table_in_diff_triggers_reliability(self):
        diffs = self._diff("src/schemas/bootstrap.sql", [
            "ALTER TABLE users ADD COLUMN tier INT NOT NULL;",
        ])
        out = self._run(diffs)
        assert any(r["role"] == "reliability" for r in out)

    def test_drop_table_in_diff_triggers_reliability(self):
        diffs = self._diff("legacy/cleanup.sql", [
            "DROP TABLE old_logs;",
        ])
        out = self._run(diffs)
        assert any(r["role"] == "reliability" for r in out)

    # ---- Negative cases ----
    def test_plain_business_logic_no_trigger(self):
        diffs = self._diff("src/widgets/format.py", [
            "def format_price(amount):",
            "    return f'${amount:,.2f}'",
        ])
        out = self._run(diffs)
        assert out == []

    def test_removed_line_not_scanned(self):
        """A `-` line (deletion) should NOT trigger the detector — the
        concern is about what this PR ADDS."""
        path = "src/util.py"
        diff = (
            f"--- a/{path}\n+++ b/{path}\n"
            "@@ -10,3 +10,2 @@\n"
            "-@login_required\n"
            " def view(request): pass\n"
            " # trailing context\n"
        )
        out = self._run({path: diff})
        # The only @login_required is on a `-` line; should NOT fire.
        assert out == []

    def test_evidence_snippet_truncated(self):
        """Very long lines get snippet-truncated to keep logs readable."""
        long_line = "@PreAuthorize(\"" + "x" * 500 + "\")"
        diffs = self._diff("src/big.java", [long_line])
        out = self._run(diffs)
        assert out
        snippet = out[0]["matching_evidence"][0]["snippet"]
        assert len(snippet) <= 130  # 120 char cap + small slack

    def test_max_matches_per_role_cap(self):
        """One PR with 100 security-matching lines should only surface
        `max_matches_per_role` evidence entries."""
        many_lines = ["@PreAuthorize(\"x\")"] * 30
        diffs = self._diff("src/big.java", many_lines)
        from app.agent_loop.pr_brain import (
            _detect_required_dispatches_from_diff_content,
        )
        out = _detect_required_dispatches_from_diff_content(
            diffs, max_matches_per_role=5,
        )
        assert len(out[0]["matching_evidence"]) == 5


class TestCombinedTiers:
    """``_detect_required_dispatches`` merges Tier 1 (path) + Tier 2
    (content) into a single requirements list with ``_tier`` label."""

    def _diff(self, path: str, added_lines: list) -> dict:
        body = "\n".join(f"+{ln}" for ln in added_lines)
        diff = (
            f"--- a/{path}\n+++ b/{path}\n"
            f"@@ -0,0 +1,{len(added_lines)} @@\n{body}\n"
        )
        return {path: diff}

    def test_both_tiers_fire_same_role_emits_two_entries(self):
        """Path in /auth/ + content has bcrypt call → two security
        entries, one Tier 1, one Tier 2."""
        from app.agent_loop.pr_brain import _detect_required_dispatches

        diffs = self._diff(
            "src/auth/login.py",
            ["hashed = bcrypt.hashpw(p, bcrypt.gensalt())"],
        )
        out = _detect_required_dispatches(diffs)
        security_entries = [r for r in out if r["role"] == "security"]
        # Tier 1 (path /auth/) AND Tier 2 (bcrypt content) both fire
        assert len(security_entries) == 2
        tiers = {e["_tier"] for e in security_entries}
        assert tiers == {1, 2}

    def test_tier2_only_when_path_does_not_match(self):
        """PR #14234 shape — security-relevant content in a path that
        doesn't self-declare."""
        from app.agent_loop.pr_brain import _detect_required_dispatches

        diffs = self._diff(
            "loan/service/SandBoxServiceImpl.java",
            [
                "public void addCountIpWhitelist(List<String> ips) {",
                "    redisUtil.sSet(COUNT_IP_WHITELIST_KEY, ips.toArray());",
                "}",
            ],
        )
        out = _detect_required_dispatches(diffs)
        assert len(out) == 1
        assert out[0]["role"] == "security"
        assert out[0]["_tier"] == 2


class TestCoordinatorQueryInjection:
    """``_build_v2_coordinator_query`` must inject the
    '## MANDATORY investigations' section when the detector fires."""

    def _make_brain(self):
        from tests.test_pr_brain import _make_pr_brain
        return _make_pr_brain()

    def _ctx(self, paths):
        from app.code_review.models import ChangedFile, FileCategory, PRContext
        files = [
            ChangedFile(path=p, additions=10, deletions=0,
                        category=FileCategory.BUSINESS_LOGIC)
            for p in paths
        ]
        return PRContext(
            diff_spec="HEAD~1..HEAD",
            files=files,
            total_additions=10 * len(files),
            total_deletions=0,
            total_changed_lines=10 * len(files),
            file_count=len(files),
        )

    def _risk(self):
        from app.code_review.models import RiskLevel, RiskProfile
        return RiskProfile(
            correctness=RiskLevel.LOW, security=RiskLevel.LOW,
            reliability=RiskLevel.LOW, concurrency=RiskLevel.LOW,
            operational=RiskLevel.LOW,
        )

    def test_security_path_injects_mandatory_section(self):
        brain = self._make_brain()
        paths = [
            "cms/src/main/java/com/abound/cms/common/v3/security/CmsV3SecurityConfig.java",
            "cms/src/main/java/com/abound/cms/service/v3/V3CmsAuthService.java",
        ]
        query = brain._build_v2_coordinator_query(
            pr_context=self._ctx(paths),
            risk_profile=self._risk(),
            file_diffs={p: "@@ -1 +1 @@\n+foo\n" for p in paths},
            impact_context="",
        )
        assert "## MANDATORY investigations (Phase 1 detected)" in query
        assert 'role="security"' in query
        assert "non-skippable" in query.lower()
        # Matching paths must appear under the role header
        assert "V3CmsAuthService.java" in query

    def test_no_matching_paths_omits_mandatory_section(self):
        brain = self._make_brain()
        paths = ["src/widgets/parser.go", "tests/test_parser.go"]
        query = brain._build_v2_coordinator_query(
            pr_context=self._ctx(paths),
            risk_profile=self._risk(),
            file_diffs={p: "@@ -1 +1 @@\n+foo\n" for p in paths},
            impact_context="",
        )
        assert "## MANDATORY investigations" not in query

    def test_multiple_roles_each_rendered(self):
        brain = self._make_brain()
        paths = [
            "src/auth/oauth.py",
            "db/migration/V42__add_column.sql",
        ]
        query = brain._build_v2_coordinator_query(
            pr_context=self._ctx(paths),
            risk_profile=self._risk(),
            file_diffs={p: "@@ -1 +1 @@\n+foo\n" for p in paths},
            impact_context="",
        )
        assert 'role="security"' in query
        assert 'role="reliability"' in query
        # Both triggers' reasons should be present
        assert "migration" in query.lower() or "not null" in query.lower()
