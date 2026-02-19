"""PromptBuilder for constructing task-focused code generation prompts.

Instead of including all detected workspace languages and always instructing
"add tests" and "error handling", this module infers languages from target
files, detects doc-only changes, and adapts output instructions to a
configurable output mode.
"""
import logging
import os
from typing import List, Optional

from app.agent.style_loader import Language, CodeStyleLoader, _read_builtin_style, _read_universal_style
from .prompts import format_summaries_for_code_prompt

logger = logging.getLogger(__name__)

# Extension-to-Language mapping
_EXT_TO_LANGUAGE = {
    ".py": Language.PYTHON,
    ".js": Language.JAVASCRIPT,
    ".ts": Language.JAVASCRIPT,
    ".tsx": Language.JAVASCRIPT,
    ".jsx": Language.JAVASCRIPT,
    ".mjs": Language.JAVASCRIPT,
    ".java": Language.JAVA,
    ".go": Language.GO,
    ".json": Language.JSON,
}

# Extensions considered documentation
_DOC_EXTENSIONS = {".md", ".rst", ".txt", ".adoc"}

# Path prefixes considered documentation
_DOC_PREFIXES = ("docs/", "doc/")

# Keywords that signal documentation-only work
_DOC_KEYWORDS = {"docstring", "documentation", "readme", "jsdoc", "type hint", "comment"}

# Keywords that signal code work
_CODE_KEYWORDS = {"implement", "refactor", "endpoint", "fix bug"}

# Output format instruction snippets keyed by output_mode
_OUTPUT_INSTRUCTIONS = {
    "unified_diff": (
        "Output ONLY valid unified diff patches compatible with `git apply`.\n"
        "Rules:\n"
        "- Start each file with `--- a/<path>` and `+++ b/<path>` headers.\n"
        "- Use `@@ -start,count +start,count @@` hunk headers.\n"
        "- Include 3 lines of unchanged context around each change.\n"
        "- Do NOT include any prose, explanation, or markdown outside the diff.\n"
        "- If creating a new file, use `--- /dev/null` as the source."
    ),
    "direct_repo_edits": (
        "Implement the changes directly in the repository files.\n"
        "Rules:\n"
        "- Provide the COMPLETE updated file contents for every file that changes.\n"
        "- Prefix each file with a header line: `=== FILE: <path> ===`\n"
        "- After implementing, run the project's test suite and linter to verify.\n"
        "- Report any test failures or lint errors and fix them before finalizing.\n"
        "- Do NOT omit unchanged sections — output the full file each time."
    ),
    "plan_then_diff": (
        "First, provide a SHORT implementation plan as a numbered bullet list\n"
        "(no more than 10 steps). Then provide the changes as unified diff\n"
        "patches compatible with `git apply`.\n"
        "Rules:\n"
        "- Separate the plan from the diff with a `---` divider line.\n"
        "- The plan should cover WHAT changes to make and WHY, not HOW.\n"
        "- The diff section follows the same rules as unified_diff output:\n"
        "  `--- a/<path>`, `+++ b/<path>`, `@@ ... @@` hunk headers, 3 lines context."
    ),
}


def infer_languages_from_components(affected_components: List[str]) -> List[Language]:
    """Infer programming languages from affected component file paths.

    Maps file extensions to the Language enum. Returns a deduplicated list.
    Unknown extensions are ignored.

    Args:
        affected_components: List of file paths or component names.

    Returns:
        Deduplicated list of Language enum values.
    """
    seen = set()
    result: List[Language] = []
    for component in affected_components:
        _, ext = os.path.splitext(component)
        ext = ext.lower()
        lang = _EXT_TO_LANGUAGE.get(ext)
        if lang and lang not in seen:
            seen.add(lang)
            result.append(lang)
    return result


def is_documentation_only(
    affected_components: List[str],
    proposed_solution: str = "",
) -> bool:
    """Detect whether the change is documentation-only.

    Signal 1: ALL affected components have doc extensions or doc path prefixes.
    Signal 2 (when no components): proposed_solution contains doc keywords but
    no code keywords.

    Args:
        affected_components: List of file paths or component names.
        proposed_solution: Text describing the proposed solution.

    Returns:
        True if the change appears to be documentation-only.
    """
    if affected_components:
        for component in affected_components:
            _, ext = os.path.splitext(component)
            ext_is_doc = ext.lower() in _DOC_EXTENSIONS
            path_is_doc = any(component.startswith(p) for p in _DOC_PREFIXES)
            if not ext_is_doc and not path_is_doc:
                return False
        return True

    # No components — check solution text for doc vs code keywords
    solution_lower = proposed_solution.lower()
    has_doc = any(kw in solution_lower for kw in _DOC_KEYWORDS)
    has_code = any(kw in solution_lower for kw in _CODE_KEYWORDS)
    return has_doc and not has_code


class PromptBuilder:
    """Fluent builder for code generation prompts.

    Produces shorter, task-focused prompts by:
    - Inferring languages from affected components instead of using all
      workspace languages.
    - Omitting "tests" and "error handling" requirements for doc-only changes.
    - Adapting the output format section to a configurable output_mode.
    """

    def __init__(
        self,
        problem_statement: str,
        proposed_solution: str,
        affected_components: List[str],
        risk_level: str,
    ):
        self._problem = problem_statement
        self._solution = proposed_solution
        self._components = affected_components
        self._risk = risk_level
        self._context_snippet: Optional[str] = None
        self._context_snippets: Optional[List[dict]] = None
        self._policy_constraints: Optional[str] = None
        self._room_code_style: Optional[str] = None
        self._detected_languages: Optional[List[str]] = None
        self._output_mode: str = "unified_diff"

    # -- fluent setters --

    def with_context_snippet(self, snippet: Optional[str]) -> "PromptBuilder":
        self._context_snippet = snippet
        return self

    def with_context_snippets(self, snippets: Optional[List[dict]]) -> "PromptBuilder":
        """Set multiple file-targeted context snippets.

        Args:
            snippets: List of dicts with "file_path" and "snippet" keys.
                Only non-empty snippets are kept.
        """
        if snippets:
            self._context_snippets = [s for s in snippets if s.get("snippet")]
        else:
            self._context_snippets = None
        return self

    def with_policy_constraints(self, constraints: Optional[str]) -> "PromptBuilder":
        self._policy_constraints = constraints
        return self

    def with_room_code_style(self, style: Optional[str]) -> "PromptBuilder":
        self._room_code_style = style
        return self

    def with_detected_languages(self, languages: Optional[List[str]]) -> "PromptBuilder":
        self._detected_languages = languages
        return self

    def with_output_mode(self, mode: str) -> "PromptBuilder":
        self._output_mode = mode
        return self

    # -- style resolution --

    def _resolve_style_guidelines(self) -> Optional[str]:
        """Resolve style guidelines with priority:

        1. Room-level code style (as-is).
        2. Languages inferred from affected_components.
        3. Fallback: detected_languages (workspace-wide).
        4. Fallback: CodeStyleLoader.
        """
        if self._room_code_style:
            return self._room_code_style

        # Try inferred languages first
        inferred = infer_languages_from_components(self._components)
        if inferred:
            return self._build_style_from_languages(inferred)

        # Fallback to workspace-detected languages
        if self._detected_languages:
            lang_enums: List[Language] = []
            for lang_str in self._detected_languages:
                try:
                    lang_enums.append(Language(lang_str))
                except ValueError:
                    pass
            if lang_enums:
                return self._build_style_from_languages(lang_enums)

        # Final fallback: CodeStyleLoader
        try:
            loader = CodeStyleLoader()
            style, _ = loader.get_style()
            if style:
                return style
        except Exception as e:
            logger.debug(f"Could not load style guidelines: {e}")

        return None

    @staticmethod
    def _build_style_from_languages(languages: List[Language]) -> Optional[str]:
        """Load universal + language-specific style guidelines."""
        try:
            parts = [_read_universal_style()]
            for lang in languages:
                try:
                    parts.append(_read_builtin_style(lang))
                except FileNotFoundError:
                    pass
            if len(parts) > 1:
                return "\n\n---\n\n".join(parts)
            return parts[0]
        except Exception as e:
            logger.debug(f"Could not build style from languages: {e}")
            return None

    # -- build --

    def build(self) -> str:
        """Assemble the final code generation prompt string."""
        doc_only = is_documentation_only(self._components, self._solution)
        style_str = self._resolve_style_guidelines()

        # Components list
        if self._components:
            components_str = "\n".join(f"- {c}" for c in self._components)
        else:
            components_str = "- (No specific components identified)"

        # Optional sections
        context_section = ""
        if self._context_snippets:
            # Multi-file context snippets (preferred over single snippet)
            snippet_blocks = []
            for s in self._context_snippets:
                file_path = s.get("file_path", "unknown")
                snippet_text = s.get("snippet", "")
                snippet_blocks.append(
                    f"### {file_path}\n```\n{snippet_text}\n```"
                )
            snippets_body = "\n\n".join(snippet_blocks)
            context_section = (
                f"<context_snippets>\n"
                f"The following code snippets from target files provide relevant context:\n\n"
                f"{snippets_body}\n"
                f"</context_snippets>\n\n"
            )
        elif self._context_snippet:
            context_section = (
                f"<context>\n"
                f"The following code snippet provides relevant context:\n\n"
                f"```\n{self._context_snippet}\n```\n"
                f"</context>\n\n"
            )

        policy_section = ""
        if self._policy_constraints:
            policy_section = (
                f"<policy_constraints>\n"
                f"{self._policy_constraints}\n"
                f"</policy_constraints>\n\n"
            )

        style_section = ""
        if style_str:
            style_section = (
                f"<code_style>\n"
                f"{style_str}\n"
                f"</code_style>\n\n"
            )

        # Requirements — adapt based on doc-only
        requirements = [
            "1. Follow existing code patterns and conventions in the target components",
        ]
        if not doc_only:
            requirements.append("2. Include appropriate error handling")
            requirements.append("3. Add or update tests if applicable")
            requirements.append(
                f"{len(requirements) + 1}. Ensure backward compatibility where possible"
            )
            requirements.append(
                f"{len(requirements) + 1}. Document any breaking changes"
            )
        else:
            requirements.append("2. Ensure backward compatibility where possible")
            requirements.append("3. Document any breaking changes")
        requirements_str = "\n".join(requirements)

        # Output format
        output_instruction = _OUTPUT_INSTRUCTIONS.get(
            self._output_mode,
            _OUTPUT_INSTRUCTIONS["unified_diff"],
        )

        return (
            f"You are a senior software engineer tasked with implementing code changes.\n\n"
            f"<problem>\n{self._problem or 'No problem statement provided.'}\n</problem>\n\n"
            f"<solution>\n{self._solution or 'No solution proposed.'}\n</solution>\n\n"
            f"<target_components>\n{components_str}\n</target_components>\n\n"
            f"<risk_level>{self._risk or 'unknown'}</risk_level>\n\n"
            f"{context_section}{policy_section}{style_section}"
            f"<instructions>\n"
            f"Based on the above information, implement the necessary code changes.\n\n"
            f"Requirements:\n{requirements_str}\n\n"
            f"Output Format:\n{output_instruction}\n"
            f"</instructions>\n\n"
            f"Begin implementation:"
        )


def build_selective_prompt(
    primary_focus: str,
    impact_scope: str,
    summaries: list,
    context_snippet: Optional[str] = None,
    room_code_style: Optional[str] = None,
    detected_languages: Optional[List[str]] = None,
    output_mode: str = "unified_diff",
) -> str:
    """Build a selective code prompt from multi-type summaries.

    Collects affected_components from all summaries for language inference,
    resolves targeted style guidelines, and adapts output instructions.

    Args:
        primary_focus: Primary focus area of the implementation.
        impact_scope: Scope of impact.
        summaries: List of code-relevant summary dicts/objects.
        context_snippet: Optional code context.
        room_code_style: Optional room-level style override.
        detected_languages: Optional workspace-detected languages.
        output_mode: Output format mode.

    Returns:
        Complete selective code prompt string.
    """
    # Collect all affected_components from summaries for language inference
    all_components: List[str] = []
    for s in summaries:
        if hasattr(s, "affected_components"):
            all_components.extend(s.affected_components or [])
        else:
            all_components.extend(s.get("affected_components", []))

    # Resolve style using a temporary builder (reuses the priority logic)
    builder = PromptBuilder(
        problem_statement="",
        proposed_solution="",
        affected_components=all_components,
        risk_level="low",
    )
    builder.with_room_code_style(room_code_style)
    builder.with_detected_languages(detected_languages)
    style_str = builder._resolve_style_guidelines()

    # Format summaries
    summaries_section = format_summaries_for_code_prompt(summaries)

    # Optional sections
    context_section = ""
    if context_snippet:
        context_section = (
            f"<context>\n"
            f"The following code snippet provides relevant context:\n\n"
            f"```\n{context_snippet}\n```\n"
            f"</context>\n\n"
        )

    policy_section = ""
    try:
        from app.config import get_config
        from app.policy.auto_apply import FORBIDDEN_PATHS
        from .prompts import format_policy_constraints

        config = get_config()
        limits = config.change_limits
        policy_str = format_policy_constraints(
            max_files=limits.max_files_per_request,
            max_lines_changed=limits.max_total_lines,
            forbidden_paths=FORBIDDEN_PATHS,
        )
        policy_section = (
            f"<policy_constraints>\n{policy_str}\n</policy_constraints>\n\n"
        )
    except Exception as e:
        logger.debug(f"Could not load policy constraints: {e}")

    style_section = ""
    if style_str:
        style_section = (
            f"<code_style>\n{style_str}\n</code_style>\n\n"
        )

    output_instruction = _OUTPUT_INSTRUCTIONS.get(
        output_mode,
        _OUTPUT_INSTRUCTIONS["unified_diff"],
    )

    return (
        f"You are a senior software engineer tasked with implementing changes "
        f"based on structured engineering decisions.\n\n"
        f"You will receive:\n"
        f"- Only code-relevant discussion summaries\n"
        f"- Primary focus\n"
        f"- Impact scope\n\n"
        f"{policy_section}{style_section}"
        f"<primary_focus>{primary_focus or 'No primary focus specified'}</primary_focus>\n\n"
        f"<impact_scope>{impact_scope or 'local'}</impact_scope>\n\n"
        f"<summaries>\n{summaries_section}\n</summaries>\n\n"
        f"{context_section}"
        f"<instructions>\n"
        f"Based on the above engineering decisions, provide a structured implementation plan.\n\n"
        f"Requirements:\n"
        f"1. Analyze all code-relevant summaries and identify overlapping concerns\n"
        f"2. Consolidate affected components across all summaries\n"
        f"3. Specify file-level changes with clear descriptions\n"
        f"4. Determine if tests are required for the changes\n"
        f"5. Assess if any migrations (database, config, etc.) are needed\n"
        f"6. Provide an overall risk assessment\n\n"
        f"Output Format:\n"
        f"{output_instruction}\n\n"
        f"Provide your response as valid JSON matching this schema:\n\n"
        f"{{\n"
        f'  "implementation_plan": {{\n'
        f'    "affected_components": ["list of affected modules/files"],\n'
        f'    "file_level_changes": [\n'
        f"      {{\n"
        f'        "file": "path/to/file.py",\n'
        f'        "change_type": "modify|create|delete",\n'
        f'        "description": "Description of what changes to make"\n'
        f"      }}\n"
        f"    ],\n"
        f'    "tests_required": true,\n'
        f'    "migration_required": false,\n'
        f'    "risk_level": "low|medium|high"\n'
        f"  }}\n"
        f"}}\n\n"
        f"Output only the JSON object.\n"
        f"</instructions>"
    )
