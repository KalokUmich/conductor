"""ContextEnricher — orchestrates context gathering and LLM explanation.

This class is the central coordinator for the /context/explain endpoint.
It uses CodebaseSkills to fill in any context the extension did not provide,
then builds the prompt and calls the configured AI provider.

Future use: the same enricher can be wired into the CGP (Code Generation
Prompt) pipeline so code-prompt generation also benefits from rich context.
"""
import logging
from typing import Optional

from .schemas import ExplainRequest, ExplainResponse
from .skills import (
    build_explanation_prompt,
    extract_context_window,
    extract_imports,
    find_containing_function,
)

logger = logging.getLogger(__name__)

_BORDER = "=" * 60


def _log_code_block(label: str, content: str, max_lines: int = 50) -> None:
    """Format *content* with visual borders and emit at DEBUG level."""
    lines = content.splitlines()
    n_lines = len(lines)
    n_chars = len(content)
    truncated = lines[:max_lines]
    body = "\n".join(f"  | {l}" for l in truncated)
    suffix = f"\n  … ({n_lines - max_lines} more lines)" if n_lines > max_lines else ""
    logger.debug(
        "\n%s\n%s (%d lines, %d chars)\n%s\n%s%s\n%s",
        _BORDER, label, n_lines, n_chars, _BORDER, body, suffix, _BORDER,
    )


class ContextEnricher:
    """Enrich a code snippet request and produce an AI explanation.

    Usage::

        from app.ai_provider.resolver import get_resolver

        enricher = ContextEnricher(provider=get_resolver().get_provider())
        response = enricher.explain(request)

    The enricher fills in missing context fields using CodebaseSkills, builds
    a focused prompt, calls the LLM, and returns a structured response.
    """

    def __init__(self, provider, rag_indexer=None) -> None:
        """
        Args:
            provider: An AIProvider instance (ClaudeDirectProvider, etc.)
                      with a ``call_model(prompt: str) -> str`` method.
            rag_indexer: Optional RagIndexer for semantic codebase search.
        """
        self._provider = provider
        self._rag_indexer = rag_indexer

    def explain(self, request: ExplainRequest) -> ExplainResponse:
        """Explain the code snippet in *request*.

        Steps:
        1. Fill in any missing context using CodebaseSkills.
        2. Assemble the explanation prompt via ``build_explanation_prompt``.
        3. Call the LLM.
        4. Return a structured ExplainResponse.

        Args:
            request: Validated ExplainRequest from the API.

        Returns:
            ExplainResponse with the explanation text and metadata.

        Raises:
            RuntimeError: If the AI provider call fails.
        """
        logger.info(
            "[ContextEnricher] explain() called: file=%s lines=%d-%d lang=%s",
            request.file_path, request.line_start, request.line_end, request.language,
        )
        logger.info(
            "[ContextEnricher] Context received: "
            "file_content=%s surrounding_code=%s imports=%d "
            "containing_function=%s related_files=%d",
            f"{len(request.file_content)} chars" if request.file_content else "NONE",
            f"{len(request.surrounding_code)} chars" if request.surrounding_code else "NONE",
            len(request.imports),
            "YES" if request.containing_function else "NONE",
            len(request.related_files),
        )

        # Debug: log the actual code snippet
        _log_code_block("TARGET SNIPPET", request.snippet)

        # --- 1. Fill in missing context ---
        file_content = request.file_content or ""
        language = request.language or "text"

        surrounding_code = request.surrounding_code
        if not surrounding_code and file_content:
            surrounding_code = extract_context_window(
                file_content, request.line_start, request.line_end
            )
            logger.info("[ContextEnricher] Filled surrounding_code from file_content: %d chars", len(surrounding_code))
            _log_code_block("SURROUNDING CODE (filled)", surrounding_code)

        imports = request.imports
        if not imports and file_content:
            imports = extract_imports(file_content, language)
            logger.info("[ContextEnricher] Filled imports from file_content: %d imports", len(imports))
            _log_code_block("IMPORTS (filled)", "\n".join(imports))

        containing_function = request.containing_function
        if not containing_function and file_content:
            containing_function = find_containing_function(
                file_content, request.line_start, language
            )
            logger.info("[ContextEnricher] Filled containing_function: %s", containing_function or "NONE")
            if containing_function:
                _log_code_block("CONTAINING FUNCTION (filled)", containing_function)

        # Structured enrichment summary
        logger.info(
            "[ContextEnricher] Enrichment summary for %s:\n"
            "  snippet: %d lines (%d chars)\n"
            "  surrounding_code: %s\n"
            "  imports: %d found%s\n"
            "  containing_function: %s\n"
            "  related_files: %d provided",
            request.file_path,
            len(request.snippet.splitlines()), len(request.snippet),
            f"{len(surrounding_code)} chars (filled by backend)" if surrounding_code and not request.surrounding_code
            else (f"{len(surrounding_code)} chars (from extension)" if surrounding_code else "NONE"),
            len(imports),
            " (filled by backend)" if imports and not request.imports else (" (from extension)" if imports else ""),
            (f"'{containing_function}' (filled by backend)" if containing_function and not request.containing_function
             else (f"'{containing_function}' (from extension)" if containing_function else "NONE")),
            len(request.related_files),
        )

        # --- 1b. RAG context (best-effort) ---
        rag_context = None
        if self._rag_indexer and request.workspace_id:
            rag_context = self._fetch_rag_context(
                workspace_id=request.workspace_id,
                snippet=request.snippet,
                file_path=request.file_path,
            )

        # --- 2. Build prompt ---
        prompt = build_explanation_prompt(
            snippet=request.snippet,
            file_path=request.file_path,
            language=language,
            surrounding_code=surrounding_code,
            imports=imports,
            containing_function=containing_function,
            related_files=[rf.model_dump() for rf in request.related_files],
            rag_context=rag_context,
        )

        logger.info(
            "[ContextEnricher] Prompt built for %s lines %d-%d (%d chars)",
            request.file_path, request.line_start, request.line_end, len(prompt),
        )
        _log_code_block("LLM PROMPT", prompt, max_lines=100)

        # --- 3. Call LLM ---
        explanation = self._provider.call_model(prompt)
        logger.info(
            "[ContextEnricher] LLM returned %d chars for %s",
            len(explanation), request.file_path,
        )
        _log_code_block("LLM RESPONSE", explanation)

        return ExplainResponse(
            explanation=explanation.strip(),
            model=getattr(self._provider, "model_id", "unknown"),
            language=language,
            file_path=request.file_path,
            line_start=request.line_start,
            line_end=request.line_end,
        )

    def _fetch_rag_context(
        self,
        workspace_id: str,
        snippet: str,
        file_path: str,
    ) -> Optional[str]:
        """Query the RAG index for related code chunks.

        Returns an XML-formatted string of related code, or None on failure.
        Errors are logged but never propagated — RAG is best-effort.
        """
        try:
            results = self._rag_indexer.search(
                workspace_id=workspace_id,
                query=snippet,
                top_k=5,
            )
            if not results:
                return None

            # Filter out chunks from the same file
            filtered = [r for r in results if r.file_path != file_path]
            if not filtered:
                return None

            parts: list[str] = []
            for item in filtered[:5]:
                symbol_info = ""
                if item.symbol_name:
                    symbol_info = f' symbol="{item.symbol_name}" type="{item.symbol_type}"'
                parts.append(
                    f'<chunk file="{item.file_path}" '
                    f'lines="{item.start_line}-{item.end_line}" '
                    f'score="{item.score:.3f}"'
                    f'{symbol_info} '
                    f'language="{item.language}">\n'
                    f"(lines {item.start_line}–{item.end_line})\n"
                    f"</chunk>"
                )

            rag_xml = "\n".join(parts)
            logger.info(
                "[ContextEnricher] RAG context: %d chunks for %s",
                len(filtered[:5]), file_path,
            )
            _log_code_block("RAG CONTEXT", rag_xml)
            return rag_xml
        except Exception as exc:
            logger.warning("[ContextEnricher] RAG search failed (non-fatal): %s", exc)
            return None
