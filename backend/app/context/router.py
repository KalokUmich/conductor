"""Context enrichment router — POST /context/explain."""
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

# System instruction used for the /explain-rich endpoint.
# Keeps the user-prompt (XML context) clean and puts all behavioural
# guidance here so the model knows its role and expected output format.
_EXPLAIN_SYSTEM = (
    "You are a senior software engineer reviewing code for your team. "
    "You will receive code context inside XML tags (<context>, <file>, <question>). "
    "Answer the question directly and concisely. Always cover these points where relevant:\n"
    "• Purpose — what the code does and why it exists\n"
    "• Inputs / parameters — what data it receives and what constraints apply\n"
    "• Outputs / return values — what it returns or the side-effects it produces\n"
    "• Business context — the real-world scenario or domain this code operates in\n"
    "• Key dependencies — external services, injected objects, or patterns relied upon\n"
    "• Gotchas — error paths, edge cases, or non-obvious behaviour worth knowing\n\n"
    "Be specific, not generic. Avoid restating the code verbatim. "
    "Write in plain English using short paragraphs or a tight bullet list. "
    "You may use **bold** for emphasis, `backticks` for inline code references, "
    "and - bullet lists. Do NOT use markdown headers (#). "
    "Aim for 5–10 sentences total."
)

from app.ai_provider.resolver import get_resolver
from app.rag.router import get_indexer

from .enricher import ContextEnricher
from .schemas import ExplainRequest, ExplainRichRequest, ExplainResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/context", tags=["context"])


@router.post("/explain", response_model=ExplainResponse)
async def explain_code(request: ExplainRequest) -> ExplainResponse:
    """Explain a code snippet using AI with workspace context enrichment.

    The extension sends the selected code plus optional context gathered by
    ``ContextGatherer`` (file content, surrounding lines, imports, LSP data).
    The backend fills in any missing fields and calls the active AI provider.

    Args:
        request: ExplainRequest with snippet, file path, and optional context.

    Returns:
        ExplainResponse with the AI explanation.

    Example::

        POST /context/explain
        {
            "room_id": "abc-123",
            "snippet": "def process(data):\\n    return data.split(',')",
            "file_path": "app/processor.py",
            "line_start": 10,
            "line_end": 11,
            "language": "python",
            "surrounding_code": "9: # Process raw input\\n10: def process...",
            "imports": ["import re", "from typing import List"],
            "containing_function": "def process(data):"
        }
    """
    logger.info(
        "[context/explain] Received request: file=%s lines=%d-%d lang=%s "
        "file_content=%s imports=%d related_files=%d",
        request.file_path, request.line_start, request.line_end, request.language,
        f"{len(request.file_content)} chars" if request.file_content else "NONE",
        len(request.imports),
        len(request.related_files),
    )
    logger.debug(
        "[context/explain] Snippet:\n%s",
        request.snippet,
    )

    resolver = get_resolver()
    if resolver is None:
        logger.warning("[context/explain] AI resolver is None — no providers configured")
        return JSONResponse(
            {"error": "AI provider not available"},
            status_code=503,
        )

    # Prefer the cached active provider; only re-resolve as a fallback.
    provider = resolver.get_active_provider() or resolver.resolve()
    if provider is None:
        logger.warning("[context/explain] No healthy AI provider")
        return JSONResponse(
            {"error": "No healthy AI provider found"},
            status_code=503,
        )

    logger.info("[context/explain] Using provider: %s model: %s", type(provider).__name__, getattr(provider, "model_id", "unknown"))

    try:
        enricher = ContextEnricher(provider=provider, rag_indexer=get_indexer())
        response = enricher.explain(request)
        logger.info(
            "[context/explain] Success: %s lines %d-%d via %s, explanation=%d chars",
            request.file_path, request.line_start, request.line_end,
            getattr(provider, "model_id", "unknown"),
            len(response.explanation),
        )
        return response
    except Exception as exc:
        logger.exception("[context/explain] Explanation failed: %s", exc)
        return JSONResponse(
            {"error": f"Explanation failed: {exc}"},
            status_code=500,
        )


@router.post("/explain-rich", response_model=ExplainResponse)
async def explain_rich(request: ExplainRichRequest) -> ExplainResponse:
    """Forward a pre-assembled prompt directly to the LLM.

    The extension's 8-stage pipeline (LSP, semantic search, ranked files,
    XML assembly) has already built the complete prompt.  This endpoint
    simply resolves a healthy AI provider and forwards the prompt.

    Args:
        request: ExplainRichRequest with the assembled XML prompt.

    Returns:
        ExplainResponse with the AI explanation.
    """
    logger.info(
        "[context/explain-rich] Received request: file=%s lines=%d-%d lang=%s prompt_len=%d",
        request.file_path, request.line_start, request.line_end,
        request.language, len(request.assembled_prompt),
    )
    # Log first 500 chars of the assembled prompt for debugging
    logger.debug(
        "[context/explain-rich] Prompt preview (first 500 chars):\n%s",
        request.assembled_prompt[:500],
    )

    resolver = get_resolver()
    if resolver is None:
        logger.warning("[context/explain-rich] AI resolver is None — no providers configured")
        return JSONResponse(
            {"error": "AI provider not available"},
            status_code=503,
        )

    provider = resolver.get_active_provider() or resolver.resolve()
    if provider is None:
        logger.warning("[context/explain-rich] No healthy AI provider")
        return JSONResponse(
            {"error": "No healthy AI provider found"},
            status_code=503,
        )

    model_id = getattr(provider, "model_id", "unknown")
    logger.info("[context/explain-rich] Using provider: %s model: %s", type(provider).__name__, model_id)

    try:
        explanation = provider.call_model(
            request.assembled_prompt,
            max_tokens=4096,
            system=_EXPLAIN_SYSTEM,
        )
        logger.info(
            "[context/explain-rich] Success: file=%s lines=%d-%d model=%s explanation_len=%d",
            request.file_path, request.line_start, request.line_end,
            model_id, len(explanation),
        )
        logger.debug(
            "[context/explain-rich] Full LLM response:\n%s",
            explanation,
        )
        return ExplainResponse(
            explanation=explanation.strip(),
            model=model_id,
            language=request.language,
            file_path=request.file_path,
            line_start=request.line_start,
            line_end=request.line_end,
        )
    except Exception as exc:
        logger.exception("[context/explain-rich] Explanation failed: %s", exc)
        return JSONResponse(
            {"error": f"Explanation failed: {exc}"},
            status_code=500,
        )
