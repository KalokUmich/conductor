"""High-level extraction service wrapping langextract + Bedrock provider.

Provides a simple async interface for extracting structured information
from documents (text, PDF, HTML, etc.) using any Bedrock model as the
LLM backend.

Usage::

    from app.langextract.service import LangExtractService

    svc = LangExtractService(model_id="claude-sonnet-4-20250514", region="eu-west-2")
    result = await svc.extract_from_text(
        text="Meeting notes: Project deadline moved to March 15...",
        prompt="Extract all dates, people, and action items.",
    )
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional, Sequence

if TYPE_CHECKING:
    from app.langextract.catalog import BedrockCatalog, BedrockModelInfo

logger = logging.getLogger(__name__)


@dataclass
class ExtractionResult:
    """Result of a langextract extraction."""

    success: bool = True
    documents: list[Any] = field(default_factory=list)
    raw_text: str = ""
    error: Optional[str] = None


class LangExtractService:
    """Thin async wrapper around ``lx.extract()`` with Bedrock provider."""

    def __init__(
        self,
        model_id: str = "claude-sonnet-4-20250514",
        use_bedrock: bool | None = None,
        temperature: float | None = None,
        region: str | None = None,
        catalog: BedrockCatalog | None = None,
    ):
        self.model_id = model_id
        self.use_bedrock = use_bedrock
        self.temperature = temperature
        self.region = region
        self._catalog = catalog

    def list_available_models(self) -> dict[str, list[BedrockModelInfo]]:
        """Return available models grouped by vendor from the catalog.

        Returns an empty dict if no catalog is attached.
        """
        if self._catalog is None:
            return {}
        return self._catalog.list_models()

    async def extract_from_text(
        self,
        text: str,
        prompt: str,
        examples: Sequence[Any] | None = None,
        format_type: Any = None,
        max_char_buffer: int = 1000,
    ) -> ExtractionResult:
        """Extract structured information from text using an LLM.

        Parameters
        ----------
        text : str
            The source text to extract from.
        prompt : str
            Description of what to extract.
        examples : Sequence
            ExampleData objects to guide extraction. **Required** by langextract.
        format_type : optional
            Pydantic model or dataclass for structured output.
        max_char_buffer : int
            Overlap buffer between chunks (default 1000 chars).
        """
        if not examples:
            return ExtractionResult(
                success=False,
                error="examples are required — provide at least one langextract ExampleData.",
            )
        return await asyncio.to_thread(
            self._extract_sync,
            text=text,
            prompt=prompt,
            examples=examples,
            format_type=format_type,
            max_char_buffer=max_char_buffer,
        )

    def _extract_sync(
        self,
        text: str,
        prompt: str,
        examples: Sequence[Any] | None = None,
        format_type: Any = None,
        max_char_buffer: int = 1000,
    ) -> ExtractionResult:
        """Synchronous extraction — called via asyncio.to_thread."""
        try:
            import langextract as lx

            # Ensure our provider is registered
            import app.langextract.provider  # noqa: F401
            from app.langextract.provider import BedrockLanguageModel

            model = BedrockLanguageModel(
                model_id=self.model_id,
                use_bedrock=self.use_bedrock,
                temperature=self.temperature,
                region=self.region,
                catalog=self._catalog,
            )

            kwargs: dict[str, Any] = {
                "prompt_description": prompt,
                "model": model,
                "max_char_buffer": max_char_buffer,
                "show_progress": False,
                "fetch_urls": False,
            }
            if examples:
                kwargs["examples"] = examples
            if format_type:
                kwargs["format_type"] = format_type

            result = lx.extract(text, **kwargs)

            # Normalise to list
            docs = result if isinstance(result, list) else [result]

            return ExtractionResult(
                success=True,
                documents=docs,
                raw_text=str(docs),
            )
        except Exception as exc:
            logger.exception("LangExtract extraction failed")
            return ExtractionResult(success=False, error=str(exc))
