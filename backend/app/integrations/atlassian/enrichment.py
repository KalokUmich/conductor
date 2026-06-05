"""Extract Jira tickets + Confluence pages from PR metadata and format as
context blocks the PR Brain coordinator can reason over.

One entry point: ``fetch_pr_atlassian_context()``. Wraps optional readonly
clients — returns empty string when either the client is missing or nothing
is found, so the caller can splice unconditionally.

Design:
  - Ticket keys harvested from branch name + PR title + PR description
    via ``[A-Z]+-\\d+`` regex. De-duped, capped to ``max_tickets``.
  - Confluence URLs harvested from PR description only (branch/title rarely
    contain URLs). De-duped, capped to ``max_pages``.
  - ADF (Jira v3 description) flattened to plain text — ~40% of body size
    is structural JSON overhead that just burns tokens.
  - Confluence storage XHTML → markdown-lite (h1/h2/h3/lists/links/code
    preserved, everything else stripped). Confluence macro bodies (panels,
    expand, status pills) stay as inline text so requirements embedded in
    them aren't lost.
"""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from typing import Any, Iterable, Optional

from ..confluence.readonly_client import ConfluenceReadonlyClient, extract_page_id
from ..jira.readonly_client import JiraReadonlyClient

logger = logging.getLogger(__name__)

_TICKET_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
_CONFLUENCE_URL_RE = re.compile(
    r"https?://[A-Za-z0-9-]+\.atlassian\.net/wiki/[^\s)\]\"']+",
    re.IGNORECASE,
)


def extract_ticket_keys(*sources: str, max_tickets: int = 5) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for text in sources:
        if not text:
            continue
        for m in _TICKET_RE.finditer(text):
            key = m.group(1).upper()
            if key not in seen:
                seen.add(key)
                out.append(key)
                if len(out) >= max_tickets:
                    return out
    return out


def extract_confluence_urls(*sources: str, max_pages: int = 3) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for text in sources:
        if not text:
            continue
        for m in _CONFLUENCE_URL_RE.finditer(text):
            url = m.group(0).rstrip(".,;")
            if url not in seen:
                seen.add(url)
                out.append(url)
                if len(out) >= max_pages:
                    return out
    return out


# ---------------------------------------------------------------------------
# ADF → plain text
# ---------------------------------------------------------------------------

# Mapping node-type → how to wrap its concatenated children.
# Anything not listed here delegates to children; unknown nodes are dropped.
_ADF_BLOCK_NODES = {
    "heading",
    "paragraph",
    "codeBlock",
    "blockquote",
    "rule",
    "hardBreak",
}


def adf_to_text(doc: Any, max_chars: int = 4000) -> str:
    """Flatten an Atlassian Document Format (ADF) node tree into plain text.

    Headings are prefixed with `#` markers so the LLM can still see structure.
    List items get `- ` prefixes. Code blocks get fenced. Mentions/emojis
    collapse to their displayable text.
    """
    if not isinstance(doc, dict):
        return ""
    out: list[str] = []
    _adf_walk(doc, out, depth=0, list_prefix=None)
    text = "".join(out).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[…truncated…]"
    return text


def _adf_walk(node: dict, out: list[str], depth: int, list_prefix: Optional[str]) -> None:
    ntype = node.get("type")
    children = node.get("content") or []
    attrs = node.get("attrs") or {}

    if ntype == "doc":
        for ch in children:
            _adf_walk(ch, out, depth, None)
        return

    if ntype == "heading":
        level = attrs.get("level", 2)
        out.append("\n" + "#" * max(1, min(level, 6)) + " ")
        for ch in children:
            _adf_walk(ch, out, depth, None)
        out.append("\n")
        return

    if ntype == "paragraph":
        for ch in children:
            _adf_walk(ch, out, depth, None)
        out.append("\n")
        return

    if ntype in ("bulletList", "orderedList"):
        ordered = ntype == "orderedList"
        for i, ch in enumerate(children, start=1):
            prefix = f"{i}. " if ordered else "- "
            _adf_walk(ch, out, depth + 1, prefix)
        return

    if ntype == "listItem":
        out.append(("  " * (depth - 1)) + (list_prefix or "- "))
        for ch in children:
            _adf_walk(ch, out, depth, None)
        # listItem contents already terminate with \n from paragraph
        return

    if ntype == "codeBlock":
        lang = attrs.get("language", "")
        out.append(f"\n```{lang}\n")
        for ch in children:
            _adf_walk(ch, out, depth, None)
        out.append("\n```\n")
        return

    if ntype == "blockquote":
        out.append("> ")
        for ch in children:
            _adf_walk(ch, out, depth, None)
        return

    if ntype == "hardBreak":
        out.append("\n")
        return

    if ntype == "rule":
        out.append("\n---\n")
        return

    if ntype == "text":
        out.append(node.get("text", ""))
        return

    if ntype == "mention":
        out.append("@" + (attrs.get("text") or attrs.get("displayName") or "user"))
        return

    if ntype == "emoji":
        out.append(attrs.get("shortName") or attrs.get("text") or "")
        return

    if ntype == "inlineCard" or ntype == "link":
        out.append(attrs.get("url") or "")
        return

    # Unknown node — recurse anyway, don't lose descendant text
    for ch in children:
        _adf_walk(ch, out, depth, list_prefix)


# ---------------------------------------------------------------------------
# Confluence storage XHTML → plain text
# ---------------------------------------------------------------------------


class _StorageStripper(HTMLParser):
    """Strip Confluence storage-format XHTML down to readable markdown-lite.

    Preserves: h1-h6, p, ul/ol/li, strong/em, a (with url), code, pre.
    Drops: macros (ac:structured-macro wrappers) but keeps their text bodies
    so in-panel requirements survive.
    """

    # ac:parameter is Confluence panel/macro metadata (bgColor, layout, etc.)
    # — not user-visible content, drop the text entirely.
    _SKIP_CONTENT_TAGS = {"ac:parameter", "ri:user", "ri:attachment"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._list_depth = 0
        self._in_li = False
        self._skip_depth = 0  # >0 while inside an ac:parameter etc.

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in self._SKIP_CONTENT_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if t in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.out.append("\n\n" + "#" * int(t[1]) + " ")
        elif t == "p":
            self.out.append("\n\n")
        elif t in ("ul", "ol"):
            self._list_depth += 1
            self.out.append("\n")
        elif t == "li":
            self._in_li = True
            self.out.append("\n" + "  " * max(0, self._list_depth - 1) + "- ")
        elif t == "br":
            self.out.append("\n")
        elif t == "strong" or t == "b":
            self.out.append("**")
        elif t == "em" or t == "i":
            self.out.append("_")
        elif t == "code":
            self.out.append("`")
        elif t == "pre":
            self.out.append("\n```\n")
        elif t == "a":
            href = next((v for k, v in attrs if k == "href"), "")
            if href:
                self.out.append("[")
                self._pending_href = href
            else:
                self._pending_href = None
        elif t == "table":
            self.out.append("\n")
        elif t == "tr":
            self.out.append("\n| ")
        elif t in ("th", "td"):
            self.out.append(" | ")

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in self._SKIP_CONTENT_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if t in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.out.append("\n")
        elif t in ("ul", "ol"):
            self._list_depth = max(0, self._list_depth - 1)
        elif t == "li":
            self._in_li = False
        elif t == "strong" or t == "b":
            self.out.append("**")
        elif t == "em" or t == "i":
            self.out.append("_")
        elif t == "code":
            self.out.append("`")
        elif t == "pre":
            self.out.append("\n```\n")
        elif t == "a":
            href = getattr(self, "_pending_href", None)
            if href:
                self.out.append(f"]({href})")

    def handle_data(self, data):
        if self._skip_depth:
            return
        self.out.append(data)


def confluence_storage_to_text(storage_xhtml: str, max_chars: int = 4000) -> str:
    if not storage_xhtml:
        return ""
    p = _StorageStripper()
    try:
        p.feed(storage_xhtml)
        p.close()
    except Exception as e:  # pragma: no cover — defensive
        logger.warning("confluence_storage_to_text parser error: %s", e)
        return storage_xhtml[:max_chars]
    text = "".join(p.out)
    # Collapse 3+ consecutive newlines and leading/trailing whitespace
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[…truncated…]"
    return text


# ---------------------------------------------------------------------------
# Top-level fetch + format
# ---------------------------------------------------------------------------


async def fetch_pr_atlassian_context(
    *,
    jira: Optional[JiraReadonlyClient],
    confluence: Optional[ConfluenceReadonlyClient],
    source_branch: str = "",
    pr_title: str = "",
    pr_description: str = "",
    max_tickets: int = 5,
    max_pages: int = 3,
    max_chars_per_item: int = 4000,
) -> str:
    """Return a formatted markdown block with ticket + page bodies.

    Empty string if no clients / no refs / all fetches fail — safe to splice
    unconditionally into the coordinator's prompt.
    """
    ticket_keys = extract_ticket_keys(
        source_branch,
        pr_title,
        pr_description,
        max_tickets=max_tickets,
    )
    page_urls = extract_confluence_urls(pr_description, max_pages=max_pages)

    if not ticket_keys and not page_urls:
        return ""

    blocks: list[str] = []

    if jira and jira.configured and ticket_keys:
        for key in ticket_keys:
            try:
                issue = await jira.get_issue(
                    key,
                    fields="summary,description,issuetype,priority,status,labels",
                )
                blocks.append(_format_jira_issue(key, issue, max_chars_per_item))
            except Exception as e:
                logger.warning("Jira fetch %s failed: %s", key, e)
                blocks.append(f"### Jira {key}\n_(fetch failed: {e})_")

    if confluence and confluence.configured and page_urls:
        for url in page_urls:
            try:
                page_id = extract_page_id(url)
                if page_id:
                    page = await confluence.get_page(page_id, body_format="storage")
                else:
                    page = await confluence.get_page_by_url(url, body_format="storage")
                blocks.append(_format_confluence_page(url, page, max_chars_per_item))
            except Exception as e:
                logger.warning("Confluence fetch %s failed: %s", url, e)
                blocks.append(f"### Confluence {url}\n_(fetch failed: {e})_")

    if not blocks:
        return ""
    return "\n\n".join(blocks)


def _format_jira_issue(key: str, issue: dict, max_chars: int) -> str:
    f = issue.get("fields") or {}
    summary = f.get("summary") or "(no summary)"
    itype = (f.get("issuetype") or {}).get("name", "")
    status = (f.get("status") or {}).get("name", "")
    priority = (f.get("priority") or {}).get("name", "")
    labels: Iterable[str] = f.get("labels") or []

    header = f"### Jira {key} — {summary}"
    meta_parts = [x for x in (itype, status, priority and f"priority: {priority}") if x]
    if labels:
        meta_parts.append("labels: " + ", ".join(labels))
    meta = " · ".join(meta_parts)

    desc_raw = f.get("description")
    if isinstance(desc_raw, dict):
        desc = adf_to_text(desc_raw, max_chars=max_chars)
    elif isinstance(desc_raw, str):
        desc = desc_raw.strip()[:max_chars]
    else:
        desc = ""

    lines = [header]
    if meta:
        lines.append(f"_{meta}_")
    if desc:
        lines.append("")
        lines.append(desc)
    return "\n".join(lines)


def _format_confluence_page(url: str, page: dict, max_chars: int) -> str:
    title = page.get("title") or "(untitled)"
    space = page.get("_space") or {}
    space_label = f" ({space.get('key')})" if space.get("key") else ""

    body = (page.get("body") or {}).get("storage") or {}
    xhtml = body.get("value") or ""
    text = confluence_storage_to_text(xhtml, max_chars=max_chars)

    header = f"### Confluence{space_label} — {title}"
    lines = [header, f"_{url}_"]
    if text:
        lines.append("")
        lines.append(text)
    return "\n".join(lines)
