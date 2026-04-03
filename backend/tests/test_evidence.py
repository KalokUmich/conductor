"""Tests for the Evidence Evaluator."""

from app.agent_loop.evidence import check_evidence


class TestCheckEvidence:
    """Tests for evidence quality checking."""

    def test_passes_with_file_refs(self):
        answer = "The auth flow starts in app/auth/router.py:42 and calls AuthService.login at app/auth/service.py:88."
        ev = check_evidence(answer, tool_calls_made=5, files_accessed=3, remaining_iterations=5)
        assert ev.passed is True
        assert ev.file_refs >= 2
        assert ev.guidance == ""

    def test_passes_with_code_blocks(self):
        answer = (
            "The function works as follows:\n"
            "```python\ndef authenticate(user):\n    return check_password(user)\n```\n"
            "This is defined in the auth module and handles all logins."
        )
        ev = check_evidence(answer, tool_calls_made=3, files_accessed=2, remaining_iterations=5)
        assert ev.passed is True
        assert ev.code_blocks >= 1

    def test_passes_with_line_refs(self):
        answer = (
            "The handler is defined at L42 in router.py and delegates to L88 in service.py for processing. "
            "Additional context: the service layer validates input before calling the repository."
        )
        ev = check_evidence(answer, tool_calls_made=3, files_accessed=2, remaining_iterations=5)
        assert ev.passed is True
        assert ev.file_refs >= 2

    def test_passes_with_line_word_refs(self):
        answer = (
            "The error occurs at line 42 in the authentication module. It calls the database at lines 88-95. "
            "The root cause is a missing null check in the password validation logic."
        )
        ev = check_evidence(answer, tool_calls_made=3, files_accessed=2, remaining_iterations=5)
        assert ev.passed is True
        assert ev.file_refs >= 2

    def test_fails_no_refs_no_code(self):
        answer = (
            "The authentication system works by checking user credentials against "
            "the database. It uses a service layer pattern where the controller "
            "delegates to a service which calls the repository."
        )
        ev = check_evidence(answer, tool_calls_made=5, files_accessed=3, remaining_iterations=5)
        assert ev.passed is False
        assert "file:line" in ev.guidance

    def test_fails_too_few_tool_calls(self):
        answer = (
            "The auth is in auth/router.py:42 and it works by checking passwords "
            "against the database with bcrypt hashing and salt rotation."
        )
        ev = check_evidence(answer, tool_calls_made=1, files_accessed=1, remaining_iterations=5)
        assert ev.passed is False
        assert "tool call" in ev.guidance

    def test_fails_no_files_accessed(self):
        answer = (
            "The auth is in auth/router.py:42 and it works by checking passwords "
            "against the database with bcrypt hashing and salt rotation."
        )
        ev = check_evidence(answer, tool_calls_made=3, files_accessed=0, remaining_iterations=5)
        assert ev.passed is False
        assert "not accessed any files" in ev.guidance

    def test_short_answer_always_passes(self):
        """Very short answers (yes/no/one-liner) skip the check."""
        ev = check_evidence(
            "No, that function doesn't exist.", tool_calls_made=0, files_accessed=0, remaining_iterations=10
        )
        assert ev.passed is True

    def test_passes_when_no_budget_remaining(self):
        """If < 2 iterations remain, pass even with weak evidence."""
        answer = (
            "The authentication system works by checking user credentials against "
            "the database. It uses a service layer pattern."
        )
        ev = check_evidence(answer, tool_calls_made=1, files_accessed=0, remaining_iterations=1)
        assert ev.passed is True  # no budget to retry

    def test_passes_when_zero_remaining(self):
        answer = "Vague answer without any evidence at all and it is long enough to trigger the check."
        ev = check_evidence(answer, tool_calls_made=0, files_accessed=0, remaining_iterations=0)
        assert ev.passed is True

    def test_guidance_contains_all_problems(self):
        answer = (
            "The system works by doing stuff with things and connecting "
            "services together through middleware and configurations."
        )
        ev = check_evidence(answer, tool_calls_made=1, files_accessed=0, remaining_iterations=10)
        assert ev.passed is False
        assert "file:line" in ev.guidance
        assert "tool call" in ev.guidance
        assert "not accessed any files" in ev.guidance

    def test_file_colon_line_pattern(self):
        answer = "Found in src/controllers/auth_controller.ts:155 which calls services/auth.ts:42 for token validation."
        ev = check_evidence(answer, tool_calls_made=5, files_accessed=3, remaining_iterations=5)
        assert ev.passed is True
        assert ev.file_refs >= 2

    def test_exact_threshold_100_chars(self):
        """Answer exactly at 100 chars should NOT be skipped."""
        answer = "x" * 100
        ev = check_evidence(answer, tool_calls_made=0, files_accessed=0, remaining_iterations=10)
        assert ev.passed is False

    def test_under_threshold_99_chars(self):
        """Answer at 99 chars should be skipped."""
        answer = "x" * 99
        ev = check_evidence(answer, tool_calls_made=0, files_accessed=0, remaining_iterations=10)
        assert ev.passed is True

    # ---- Qwen-style citation patterns ----

    def test_qwen_bold_line_ref(self):
        """Qwen-style **Line:** 175 citation."""
        answer = (
            "The method has a potential null pointer issue.\n"
            "**File:** ShareCodeCheckerServiceImpl.java\n"
            "**Line:** 175\n"
            "The code calls request.isValid() without a null check."
        )
        ev = check_evidence(answer, tool_calls_made=5, files_accessed=3, remaining_iterations=5)
        assert ev.passed is True
        assert ev.file_refs >= 1

    def test_qwen_bold_line_range(self):
        """Qwen-style **Line:** 173-180 citation."""
        answer = (
            "Missing input validation in the callback handler.\n"
            "**File:** ShareCodeCheckerServiceImpl.java\n"
            "**Line:** 173-180\n"
            "The method does not validate the request object before use."
        )
        ev = check_evidence(answer, tool_calls_made=5, files_accessed=3, remaining_iterations=5)
        assert ev.passed is True
        assert ev.file_refs >= 1

    def test_qwen_bracket_line_ref(self):
        """Qwen-style [Line 42] citation."""
        answer = (
            "The error occurs because the share code is not validated [Line 174] "
            "before being passed to updateShareCodeStatus [Line 177]. This is a "
            "potential null pointer exception that needs to be addressed."
        )
        ev = check_evidence(answer, tool_calls_made=5, files_accessed=3, remaining_iterations=5)
        assert ev.passed is True
        assert ev.file_refs >= 2

    def test_qwen_bracket_lines_range(self):
        """Qwen-style [Lines 42-50] citation."""
        answer = (
            "The processLambdaCallback method [Lines 173-181] does not handle "
            "the case where request.getShareCode() returns null, which could cause "
            "downstream errors in the updateShareCodeStatus helper method."
        )
        ev = check_evidence(answer, tool_calls_made=5, files_accessed=3, remaining_iterations=5)
        assert ev.passed is True
        assert ev.file_refs >= 1

    def test_qwen_plain_line_colon(self):
        """Qwen-style Line: 175 (no bold) citation."""
        answer = (
            "The null check is missing.\n"
            "File: ShareCodeCheckerServiceImpl.java\n"
            "Line: 175\n"
            "The request parameter is used directly without validation."
        )
        ev = check_evidence(answer, tool_calls_made=5, files_accessed=3, remaining_iterations=5)
        assert ev.passed is True
        assert ev.file_refs >= 1
