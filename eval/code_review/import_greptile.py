"""Convert Greptile-benchmark JSON dumps into our ``cases.yaml`` format.

Reads the per-target JSON files written by ``scrape_greptile.py`` and emits:

  cases/greptile_<target>/cases.yaml         — case metadata
  cases/greptile_<target>/patches/<id>.patch — generated patch files

Per-PR mapping (one case per PR, since each PR has one planted bug at its core):

  case.id            "greptile-{target}-{pr_number:02d}"
  case.patch         "patches/{pr_number:02d}.patch"
  case.difficulty    derived from inferred severity (see _infer_severity)
  case.title         the PR title (the "feature" cover story)
  case.description   PR body + first ``logic:`` greptile bot comment
  case.expected_findings:
    - one entry per ``logic:``-tagged comment from the greptile bot
      (i.e. real bugs flagged by the benchmark's own canonical reviewer)
    - title_pattern: keyword regex extracted from comment body
    - file_pattern:  the comment's file path, regex-escaped
    - line_range:    [comment.line - 5, comment.line + 5]
    - severity:      inferred from the comment text (see _infer_severity)
    - category:      inferred from keywords (security/correctness/...)
    - recommendation: short fix sketch from comment body

Why ``logic:`` comments specifically: greptile-apps[bot] tags every comment
with ``style:``, ``logic:``, ``performance:``, etc. Only ``logic:`` comments
are bugs in the benchmark's sense (the others are nits / style noise that
the DO-NOT-FLAG list explicitly excludes).

The patch file is OPTIONAL — emitted only if the JSON dump has the ``diff``
field populated (controlled by ``scrape_greptile.py --no-diff``). When the
diff is missing the cases.yaml is still written so you can inspect the
expected findings, then re-run ``scrape_greptile.py`` with diffs included
to fill in the patches.

Usage::

    cd backend
    python ../eval/code_review/import_greptile.py
    python ../eval/code_review/import_greptile.py --target sentry
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger("import_greptile")
logger.addHandler(logging.StreamHandler(sys.stdout))
logger.setLevel(logging.INFO)


_THIS_DIR = Path(__file__).resolve().parent
RAW_DIR = _THIS_DIR / "cases" / "greptile_raw"
OUT_BASE = _THIS_DIR / "cases"


# ---------------------------------------------------------------------------
# Comment classification — extract bug findings from greptile bot comments
# ---------------------------------------------------------------------------

# Greptile-apps[bot] prefixes every comment with one of these tags.
# Only the ones in ``_BUG_PREFIXES`` represent actual planted bugs we want
# to flag as expected findings; the rest are style/nit noise.
_BUG_PREFIXES = {"logic", "syntax", "security", "performance"}
_NOISE_PREFIXES = {"style", "nit", "praise"}

# 2026-06-02 scoring audit: re-imports kept emitting title_patterns that were a
# bare stopword (e.g. "this", "missing"), which match almost any finding and
# inflate the deterministic scorer's title-match. Drop these generic tokens when
# extracting keywords and never emit a pattern that is exactly one stopword.
_KEYWORD_STOPWORDS = frozenset(
    {
        "this",
        "that",
        "line",
        "still",
        "uses",
        "used",
        "the",
        "a",
        "an",
        "should",
        "must",
        "consider",
        "using",
        "add",
        "added",
        "test",
        "same",
        "method",
        "missing",
        "double",
        "typo",
        "for",
        "with",
        "string",
        "time",
        "multiple",
        "inconsistent",
        "returning",
        "kind",
        "will",
        "here",
        "when",
        "where",
        "which",
        "code",
        "value",
        "function",
    }
)


def _strip_prefix(body: str) -> Tuple[str, str]:
    """Split ``"logic: foo bar"`` into ``("logic", "foo bar")``.

    Returns ``("", body)`` if no recognised prefix is present.
    """
    m = re.match(r"^([a-z_]+)\s*:\s*(.*)", body, re.DOTALL)
    if not m:
        return "", body.strip()
    return m.group(1).lower(), m.group(2).strip()


# Severity inference from comment language. Greptile bot does not output
# explicit severity tags, so we approximate from common phrasings.
_CRITICAL_RE = re.compile(
    r"\b(crash|error|exception|undefined|null pointer|"
    r"importerror|attributeerror|typeerror|"
    r"sql injection|auth bypass|secret|leak credentials|"
    r"infinite loop|deadlock|race condition|"
    r"will cause|will fail|breaks?|broken|completely)\b",
    re.IGNORECASE,
)

_WARNING_RE = re.compile(
    r"\b(may|might|could|risk|inconsistent|stale|"
    r"swallowed|silent|missing|incomplete|"
    r"if .* then|under .* conditions|edge case)\b",
    re.IGNORECASE,
)

# A ``syntax:``-tagged greptile comment is promoted to gold ONLY when its body
# signals a compile/behavior break. Pure typo / format / naming / doc nits
# ("Succesful", a double space, "santizeAnchors", javadoc "returns true") are NOT
# bugs and were inflating the recall denominator. Route them to NOISE (skip). See
# the 2026-06-03 gold audit (logic:-only filter still over-imported syntax nits).
_SYNTAX_BEHAVIORAL_RE = re.compile(
    r"\b(compil\w*|will not compile|build[- ]?break\w*|cannot (?:find|resolve)|"
    r"undefined|no such (?:method|symbol|field)|missing (?:argument|parameter|import)|"
    r"wrong (?:type|number of arguments)|type mismatch|nameerror|importerror|"
    r"nosuchmethod|does not exist|won'?t build|fails? to compile|signature|"
    r"overload|not defined|wrong locale|traditional|simplified)\b",
    re.IGNORECASE,
)


def _syntax_is_behavioral(body: str) -> bool:
    """True if a ``syntax:``-tagged comment describes a compile/behavior break (a
    real bug), not a cosmetic typo/format/naming/doc nit."""
    return bool(_SYNTAX_BEHAVIORAL_RE.search(body or ""))


def _infer_severity(prefix: str, body: str) -> str:
    """Map a (prefix, body) pair to ``critical|warning|nit``.

    Heuristic: code-provable / definite-failure language → critical;
    conditional / "might" language → warning; everything else → nit.
    """
    if prefix in _NOISE_PREFIXES:
        return "nit"
    if _CRITICAL_RE.search(body):
        return "critical"
    if _WARNING_RE.search(body):
        return "warning"
    return "warning"


def _infer_category(prefix: str, body: str) -> str:
    """Map a comment to one of our FindingCategory values."""
    text = body.lower()
    if prefix == "security" or any(
        kw in text for kw in ("injection", "auth", "secret", "credential", "csrf", "xss", "ssl", "tls")
    ):
        return "security"
    if any(kw in text for kw in ("race", "concurrent", "deadlock", "thread", "atomic", "lock")):
        return "concurrency"
    if prefix == "performance" or any(
        kw in text for kw in ("n+1", "unbounded", "loop", "scaling", "slow query", "memory")
    ):
        return "performance"
    if any(kw in text for kw in ("retry", "timeout", "swallowed", "fallback", "circuit", "shutdown", "leak")):
        return "reliability"
    return "correctness"


def _is_discriminating(tok: str) -> bool:
    """True if ``tok`` is identifier-like enough to anchor a title match.

    Discriminating = has a CamelCase boundary, an underscore, a dot, or is
    length ≥ 6 — and is not a generic stopword (see ``_KEYWORD_STOPWORDS``).
    """
    if tok.lower() in _KEYWORD_STOPWORDS:
        return False
    if "_" in tok or "." in tok:
        return True
    if re.search(r"[a-z][A-Z]", tok):  # CamelCase boundary
        return True
    return len(tok) >= 6


def _extract_keyword_pattern(body: str) -> str:
    """Build a loose regex (3-5 distinctive tokens) for ``title_pattern``.

    Greptile findings have specific symbol names ("OptimizedCursorPaginator")
    or distinctive phrases ("ImportError"). We pull the most concrete ones
    so the deterministic scorer's title-match has something to grab onto.

    2026-06-02 scoring audit: generic stopwords (``_KEYWORD_STOPWORDS``) are
    dropped so we never emit a loose pattern that is a bare stopword.
    """
    # Pull symbol-like tokens (CamelCase or snake_case, ≥4 chars)
    tokens: List[str] = []
    for m in re.finditer(r"\b([A-Z][A-Za-z0-9_]{3,}|[a-z_][a-z0-9_]{4,}_[a-z0-9_]+)\b", body):
        tok = m.group(1)
        if tok in tokens or tok.lower() in {"comment", "function", "method", "parameter"}:
            continue
        if tok.lower() in _KEYWORD_STOPWORDS:
            continue
        tokens.append(tok)
        if len(tokens) >= 4:
            break
    if not tokens:
        # Fallback: prefer discriminating (identifier-like) words, dropping
        # generic stopwords entirely.
        words = re.findall(r"[A-Za-z][A-Za-z0-9_.]{2,}", body)
        tokens = [w for w in words if _is_discriminating(w)][:5]
        if not tokens:
            # Last resort: the most distinctive raw token (longest non-stopword)
            # so we never emit a bare stopword as the whole pattern.
            non_stop = [w for w in words if w.lower() not in _KEYWORD_STOPWORDS]
            if non_stop:
                tokens = [max(non_stop, key=len)]
            else:
                tokens = ["bug"]
    return "|".join(re.escape(t) for t in tokens)


def _summarise_fix(body: str) -> str:
    """Pull the first ~120 chars of the comment as a fix sketch."""
    # Drop the prefix and get a clean first sentence
    _, content = _strip_prefix(body)
    first_sentence = re.split(r"(?<=[.!?])\s+", content, maxsplit=1)[0]
    return first_sentence[:200]


# ---------------------------------------------------------------------------
# Per-PR conversion
# ---------------------------------------------------------------------------


def _difficulty_from_severities(severities: List[str]) -> str:
    """Map the maximum severity in a PR's findings to easy/medium/hard."""
    rank = {"nit": 0, "warning": 1, "critical": 2}
    if not severities:
        return "medium"
    top = max(severities, key=lambda s: rank.get(s, 0))
    return {"nit": "easy", "warning": "medium", "critical": "hard"}.get(top, "medium")


def _convert_pr_to_case(pr: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Build one cases.yaml-shaped dict from a scraped PR record.

    Returns ``None`` if the PR has no usable bug comments (would produce
    a case with empty expected_findings).
    """
    target = pr["target"]
    pr_num = pr["pr_number"]

    # Pull just the inline ``logic:``/``security:``/etc. comments from
    # greptile-apps[bot] — those are our ground truth for "what's planted".
    bug_comments: List[Dict[str, Any]] = []
    for c in pr.get("tool_review_comments", []):
        if c.get("kind") != "inline":
            continue
        if c.get("author") != "greptile-apps[bot]":
            continue
        prefix, _ = _strip_prefix(c.get("body", ""))
        if prefix not in _BUG_PREFIXES:
            continue
        # syntax-nit gate: only promote syntax comments that break compile/behavior.
        if prefix == "syntax" and not _syntax_is_behavioral(c.get("body", "")):
            logger.warning(
                "  %s PR#%d dropping cosmetic syntax-nit (no compile/behavior signal): %s",
                target,
                pr_num,
                str(c.get("body", ""))[:80].replace("\n", " "),
            )
            continue
        bug_comments.append(c)

    if not bug_comments:
        logger.warning(
            "  %s PR#%d has no logic-tagged greptile comments — skipping",
            target,
            pr_num,
        )
        return None

    # Build expected_findings list
    expected: List[Dict[str, Any]] = []
    for c in bug_comments:
        body = c.get("body", "")
        prefix, content = _strip_prefix(body)
        line = c.get("line") or 1
        path = c.get("path", "")
        expected.append(
            {
                "title_pattern": _extract_keyword_pattern(content),
                "file_pattern": re.escape(path).replace(r"\/", "/"),
                "line_range": [max(1, line - 5), line + 5],
                "severity": _infer_severity(prefix, content),
                "category": _infer_category(prefix, content),
                "recommendation": _summarise_fix(content),
            }
        )

    # Description = PR body + the first 2 logic comments (so a human can
    # eyeball the case file and see what bug it's testing without re-fetching)
    desc_parts = [pr.get("body", "").strip() or "(no PR description)"]
    desc_parts.append("")
    desc_parts.append("Planted bugs (per greptile-apps[bot] review):")
    for c in bug_comments[:3]:
        body = c.get("body", "")
        path = c.get("path", "")
        line = c.get("line") or 0
        desc_parts.append(f"  - [{path}:{line}] {body[:200]}")

    severities = [e["severity"] for e in expected]
    target_slug = target.replace(".", "_")
    case = {
        "id": f"greptile-{target_slug}-{pr_num:03d}",
        "patch": f"patches/{pr_num:03d}.patch",
        "difficulty": _difficulty_from_severities(severities),
        "title": pr.get("title", f"PR #{pr_num}"),
        "description": "\n".join(desc_parts),
        # Per-case source_dir override — points at the base of THIS PR,
        # materialized via ``materialize_greptile_bases.py``. Each
        # greptile PR is paired (``feature-X-baseline`` -> ``feature-X-impl``)
        # so every case has its own base directory.
        "source_dir": f"repos/greptile_bases/{target_slug}/{pr_num:03d}",
        # Original git refs — base_sha is the SOURCE OF TRUTH for the
        # materializer (branch tips can move after the PR was opened, which
        # makes patches stop applying cleanly). base_ref/head_ref are kept
        # for human traceability only.
        "base_sha": pr.get("base_sha", ""),
        "base_ref": pr.get("base_ref", ""),
        "head_ref": pr.get("head_ref", ""),
        "expected_findings": expected,
    }
    return case


# ---------------------------------------------------------------------------
# Per-target driver
# ---------------------------------------------------------------------------


def _write_patch_file(
    out_dir: Path,
    pr_num: int,
    diff: str,
) -> Optional[Path]:
    """Write the diff to ``patches/{pr_num:03d}.patch`` if non-empty."""
    if not diff.strip():
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    patch_path = out_dir / f"{pr_num:03d}.patch"
    patch_path.write_text(diff, encoding="utf-8")
    return patch_path


def import_target(target: str) -> Tuple[int, int, int]:
    """Convert one ``greptile_raw/{target}.json`` into a cases.yaml.

    Returns ``(cases_written, cases_skipped, patches_written)``.
    """
    target_slug = target.replace(".", "_")
    raw_path = RAW_DIR / f"{target_slug}.json"
    if not raw_path.exists():
        logger.warning("No raw dump for target %s at %s", target, raw_path)
        return 0, 0, 0

    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    out_dir = OUT_BASE / f"greptile_{target_slug}"
    out_dir.mkdir(parents=True, exist_ok=True)
    patches_dir = out_dir / "patches"

    cases: List[Dict[str, Any]] = []
    skipped = 0
    patches = 0
    for pr in raw:
        case = _convert_pr_to_case(pr)
        if case is None:
            skipped += 1
            continue
        cases.append(case)

        # Write patch file if the diff was scraped (otherwise leave a stub)
        diff = pr.get("diff", "")
        if diff:
            if _write_patch_file(patches_dir, pr["pr_number"], diff):
                patches += 1

    # Order cases by id for stable diffs
    cases.sort(key=lambda c: c["id"])
    cases_yaml = {"cases": cases}
    out_path = out_dir / "cases.yaml"
    out_path.write_text(
        yaml.safe_dump(
            cases_yaml,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
            width=120,
        ),
        encoding="utf-8",
    )
    logger.info(
        "  %s -> %s  (%d cases, %d skipped, %d patches)",
        target,
        out_path.name,
        len(cases),
        skipped,
        patches,
    )
    return len(cases), skipped, patches


def main() -> int:
    parser = argparse.ArgumentParser(description="Import Greptile JSON → cases.yaml")
    parser.add_argument(
        "--target",
        help="Convert only this target (default: all)",
    )
    args = parser.parse_args()

    if args.target:
        targets = [args.target]
    else:
        # All targets that have a raw dump
        targets = sorted(
            p.stem.replace("_", ".") if "." not in p.stem and p.stem == "cal_com" else p.stem.replace("_", ".")
            for p in RAW_DIR.glob("*.json")
        )
        # Re-canonicalise: sentry stays sentry, cal_com becomes cal.com
        canonical = {"cal_com": "cal.com"}
        targets = [canonical.get(p.stem, p.stem) for p in RAW_DIR.glob("*.json")]

    total_cases = total_skipped = total_patches = 0
    for target in targets:
        cases, skipped, patches = import_target(target)
        total_cases += cases
        total_skipped += skipped
        total_patches += patches

    logger.info(
        "\nTotal: %d cases written, %d skipped, %d patch files",
        total_cases,
        total_skipped,
        total_patches,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
