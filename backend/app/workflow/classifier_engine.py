"""Generic, config-driven classifier engine.

Replaces hardcoded risk_classifier.py and query_classifier.py keyword logic
with a reusable engine that reads patterns from workflow YAML config.

Two built-in classifier types:
  * risk_pattern   — regex match against file paths → dimension risk levels
  * keyword_pattern — regex/keyword match against query text → best route

When route configs include ``examples``, the engine can build an LLM
classification prompt that follows the "examples over rule lists" design
principle (see CLAUDE.md §Agent Design).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from .models import (
    ClassifierResult,
    RouteConfig,
    ThresholdsConfig,
    WorkflowConfig,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Risk levels (string-based to avoid importing code_review models)
# ---------------------------------------------------------------------------

_LEVEL_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _level_ge(a: str, b: str) -> bool:
    """Check if level a >= level b."""
    return _LEVEL_ORDER.get(a, 0) >= _LEVEL_ORDER.get(b, 0)


# ---------------------------------------------------------------------------
# ClassifierEngine
# ---------------------------------------------------------------------------


class ClassifierEngine:
    """Config-driven classifier that maps input signals to routes.

    Usage::

        engine = ClassifierEngine(workflow)
        result = engine.classify({"file_paths": [...], "changed_lines": 500, ...})
        # result.matched_routes  (for parallel_all_matching)
        # result.best_route      (for first_match)
    """

    def __init__(self, workflow: WorkflowConfig) -> None:
        self._workflow = workflow
        self._classifier_type = workflow.dispatch.classifier.type
        self._thresholds = workflow.dispatch.classifier.thresholds or ThresholdsConfig()
        self._routes = workflow.routes

        # Pre-compile regex patterns for each route
        self._compiled: Dict[str, List[re.Pattern]] = {}
        for route_name, route in self._routes.items():
            patterns = route.file_patterns or route.text_patterns
            self._compiled[route_name] = [
                re.compile(p, re.IGNORECASE) for p in patterns
            ]

    def classify(self, signals: Dict[str, Any]) -> ClassifierResult:
        """Classify input signals and determine which routes to activate.

        Args:
            signals: Input data. Keys depend on classifier type:
                risk_pattern:   file_paths (list[str]), file_categories (dict),
                                changed_lines (int), business_logic_files (int),
                                schema_files (int), config_files (int)
                keyword_pattern: query_text (str)

        Returns:
            ClassifierResult with matched_routes and/or best_route.
        """
        if self._classifier_type == "risk_pattern":
            return self._classify_risk_pattern(signals)
        elif self._classifier_type == "keyword_pattern":
            return self._classify_keyword_pattern(signals)
        else:
            raise ValueError(f"Unknown classifier type: {self._classifier_type}")

    # -----------------------------------------------------------------
    # risk_pattern: match file paths against regex patterns
    # -----------------------------------------------------------------

    def _classify_risk_pattern(self, signals: Dict[str, Any]) -> ClassifierResult:
        """Classify using file path regex patterns.

        Replicates the logic from risk_classifier.py:
          1. Count how many files match each route's patterns
          2. Convert counts to risk levels using thresholds
          3. Apply boost_rules
        """
        file_paths: List[str] = signals.get("file_paths", [])
        total_files = len(file_paths)

        matched_routes: Dict[str, str] = {}
        raw_scores: Dict[str, Any] = {}

        for route_name, route in self._routes.items():
            patterns = self._compiled.get(route_name, [])

            # Count files matching any pattern
            match_count = 0
            for fp in file_paths:
                for pat in patterns:
                    if pat.search(fp):
                        match_count += 1
                        break

            # Convert count to level
            level = self._level_from_count(match_count, total_files)

            # Apply boost rules
            for rule in route.boost_rules:
                if self._evaluate_boost_condition(rule.when, signals):
                    if not _level_ge(level, rule.min_level):
                        level = rule.min_level

            matched_routes[route_name] = level
            raw_scores[route_name] = {
                "match_count": match_count,
                "total_files": total_files,
                "level": level,
            }

        return ClassifierResult(
            matched_routes=matched_routes,
            raw_scores=raw_scores,
        )

    def _level_from_count(self, count: int, total_files: int) -> str:
        """Convert a match count to a risk level string.

        Replicates risk_classifier._level_from_count exactly.
        """
        if count == 0:
            return "low"
        ratio = count / max(total_files, 1)

        high = self._thresholds.high
        medium = self._thresholds.medium

        if count >= high.count or ratio > high.ratio:
            return "high"
        if count >= medium.count or ratio > medium.ratio:
            return "medium"
        return "low"

    @staticmethod
    def _evaluate_boost_condition(condition: str, signals: Dict[str, Any]) -> bool:
        """Evaluate a simple boost rule condition string.

        Supports:
          "business_logic_files >= 10 or changed_lines > 2000"
          "schema_files > 0"
          "config_files >= 3"
        """
        # Build a safe evaluation context from signals
        ctx = {
            "business_logic_files": signals.get("business_logic_files", 0),
            "changed_lines": signals.get("changed_lines", 0),
            "schema_files": signals.get("schema_files", 0),
            "config_files": signals.get("config_files", 0),
            "total_files": len(signals.get("file_paths", [])),
        }

        # Simple expression evaluator (no eval() for safety)
        # Supports: "X op N" and "X op N or Y op M"
        parts = [p.strip() for p in condition.split(" or ")]
        for part in parts:
            if _eval_simple_comparison(part, ctx):
                return True
        return False

    # -----------------------------------------------------------------
    # keyword_pattern: match query text against keyword patterns
    # -----------------------------------------------------------------

    def _classify_keyword_pattern(self, signals: Dict[str, Any]) -> ClassifierResult:
        """Classify using keyword matching against query text.

        Replicates the logic from query_classifier.classify_query:
          1. For each route, count how many text patterns match the query
          2. The route with the highest score wins (first_match)

        text_patterns can be either:
          - Simple keywords: "flow", "process"
          - Regex patterns: "flow|process|how does"
        """
        query_text: str = signals.get("query_text", "").lower()
        if not query_text:
            return ClassifierResult(best_route=None)

        best_route: Optional[str] = None
        best_score = 0
        raw_scores: Dict[str, Any] = {}

        for route_name, route in self._routes.items():
            if route.delegate:
                # Delegate routes use their own patterns for matching
                pass

            patterns = self._compiled.get(route_name, [])
            score = 0
            for pat in patterns:
                if pat.search(query_text):
                    score += 1

            raw_scores[route_name] = score

            if score > best_score:
                best_score = score
                best_route = route_name

        # Default to a lightweight single-agent route when nothing matches.
        # Prefer 'architecture_question' (general-purpose), else first non-multi-agent route.
        if best_route is None and self._routes:
            if "architecture_question" in self._routes:
                best_route = "architecture_question"
            else:
                best_route = next(iter(self._routes))

        return ClassifierResult(
            best_route=best_route,
            raw_scores=raw_scores,
        )

    # -----------------------------------------------------------------
    # LLM classification with examples (design principle: examples > rules)
    # -----------------------------------------------------------------

    def has_examples(self) -> bool:
        """Check if any route has examples configured."""
        return any(r.examples for r in self._routes.values())

    def build_llm_prompt(self, query_text: str) -> str:
        """Build an LLM classification prompt using route examples.

        Follows the 'examples over rule lists' principle: instead of
        describing categories with abstract rules, show 3-5 concrete
        example questions for each category so the model generalises
        from motivation rather than memorising keywords.
        """
        sections: List[str] = []
        for route_name, route in self._routes.items():
            examples = route.examples
            if not examples:
                continue
            example_lines = "\n".join(f"  - \"{ex}\"" for ex in examples)
            sections.append(f"**{route_name}**:\n{example_lines}")

        categories = "\n\n".join(sections)
        route_names = ", ".join(self._routes.keys())

        return (
            "Classify this codebase question into exactly ONE category. "
            "Reply with ONLY a JSON object, no other text.\n\n"
            f"Categories (with example questions):\n\n{categories}\n\n"
            f"Valid category values: {route_names}\n\n"
            f"Question: {query_text[:500]}\n\n"
            'Reply format: {"route": "<category>"}'
        )

    async def classify_with_llm(
        self,
        query_text: str,
        provider: Any,
    ) -> Optional[str]:
        """Classify using an LLM call with example-based prompt.

        Returns the best route name, or None on failure.
        """
        import asyncio

        if not self.has_examples():
            return None

        prompt = self.build_llm_prompt(query_text)
        try:
            response = await asyncio.to_thread(
                provider.chat_with_tools,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                tools=[],
                max_tokens=100,
                system="You are a query classifier. Reply with JSON only.",
            )
            text = (response.text or "").strip()
            if "{" in text:
                json_str = text[text.index("{"):text.rindex("}") + 1]
                data = json.loads(json_str)
                route = data.get("route", "")
                if route in self._routes:
                    return route
                logger.warning("LLM returned unknown route '%s'", route)
        except Exception as exc:
            logger.warning("LLM classifier failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Helper: simple comparison evaluator (no eval())
# ---------------------------------------------------------------------------

_COMPARISON_RE = re.compile(
    r"^\s*(\w+)\s*(>=|<=|>|<|==|!=)\s*(\d+)\s*$"
)


def _eval_simple_comparison(expr: str, ctx: Dict[str, int]) -> bool:
    """Evaluate a single comparison like 'schema_files > 0'."""
    m = _COMPARISON_RE.match(expr)
    if not m:
        logger.warning("Cannot parse boost condition: %s", expr)
        return False

    var_name = m.group(1)
    op = m.group(2)
    threshold = int(m.group(3))
    value = ctx.get(var_name, 0)

    if op == ">=":
        return value >= threshold
    elif op == "<=":
        return value <= threshold
    elif op == ">":
        return value > threshold
    elif op == "<":
        return value < threshold
    elif op == "==":
        return value == threshold
    elif op == "!=":
        return value != threshold
    return False
