"""Unit tests for Phase 9.9.3 — structured sub-agent note-taking.

Covers:
- FactStore.put_note upsert semantics (same (agent, topic) overwrites)
- iter_notes_by_agent returns notes for one agent ordered latest-first
- iter_all_notes returns cross-agent notes
- update_notes tool reads current_agent_name via contextvar
- update_notes short-circuits when no FactStore is bound
- Validation: topic min-length, content min-length
- UpdateNotesParams pydantic schema validation
- stats() includes notes count
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.code_tools.schemas import UpdateNotesParams
from app.code_tools.tools import update_notes
from app.scratchpad import FactStore, bind_agent_name, bind_factstore


@pytest.fixture
def store(tmp_path):
    """A fresh FactStore bound to the scratchpad contextvar."""
    store = FactStore.open("test-notes", workspace=str(tmp_path))
    with bind_factstore(store):
        yield store
    store.delete()


# ---------------------------------------------------------------------------
# Pydantic schema
# ---------------------------------------------------------------------------


class TestUpdateNotesParams:
    def test_minimum_valid(self):
        p = UpdateNotesParams(topic="auth_flow", content="x" * 20)
        assert p.topic == "auth_flow"
        assert p.file_hint is None

    def test_topic_too_short(self):
        with pytest.raises(ValidationError):
            UpdateNotesParams(topic="a", content="x" * 20)

    def test_topic_too_long(self):
        with pytest.raises(ValidationError):
            UpdateNotesParams(topic="x" * 65, content="x" * 20)

    def test_content_too_short(self):
        with pytest.raises(ValidationError):
            UpdateNotesParams(topic="auth", content="short")

    def test_content_at_cap(self):
        # 4000 chars is the max; exactly 4000 should pass
        UpdateNotesParams(topic="auth", content="x" * 4000)

    def test_content_over_cap(self):
        with pytest.raises(ValidationError):
            UpdateNotesParams(topic="auth", content="x" * 4001)

    def test_file_hint_optional(self):
        p = UpdateNotesParams(
            topic="auth_flow",
            content="x" * 20,
            file_hint="src/auth.py",
        )
        assert p.file_hint == "src/auth.py"


# ---------------------------------------------------------------------------
# FactStore methods
# ---------------------------------------------------------------------------


class TestFactStoreNotes:
    def test_put_and_list_one_note(self, store):
        store.put_note(
            agent="pr_subagent_checks",
            topic="auth_flow",
            content="OAuth redirect handler at src/auth.py:42 doesn't validate state param",
        )
        notes = store.iter_all_notes()
        assert len(notes) == 1
        assert notes[0].agent == "pr_subagent_checks"
        assert notes[0].topic == "auth_flow"
        assert "OAuth redirect" in notes[0].content

    def test_upsert_same_agent_topic(self, store):
        """Writing (agent, topic) twice overwrites, not appends."""
        store.put_note(agent="a1", topic="t1", content="first version of note" * 2)
        store.put_note(agent="a1", topic="t1", content="second version of note" * 2)
        notes = store.iter_all_notes()
        assert len(notes) == 1
        assert "second version" in notes[0].content

    def test_different_agents_same_topic_coexist(self, store):
        """Notes from different agents with same topic are independent."""
        store.put_note(agent="a1", topic="t1", content="from a1 " * 3)
        store.put_note(agent="a2", topic="t1", content="from a2 " * 3)
        notes = store.iter_all_notes()
        assert len(notes) == 2

    def test_iter_notes_by_agent(self, store):
        store.put_note(agent="a1", topic="t1", content="a1 first " * 3)
        store.put_note(agent="a2", topic="t1", content="a2 first " * 3)
        store.put_note(agent="a1", topic="t2", content="a1 second " * 3)
        a1_notes = store.iter_notes_by_agent("a1")
        a2_notes = store.iter_notes_by_agent("a2")
        assert len(a1_notes) == 2
        assert len(a2_notes) == 1
        # Latest-first ordering
        topics = [n.topic for n in a1_notes]
        assert topics[0] == "t2"  # written most recently

    def test_file_hint_round_trips(self, store):
        store.put_note(
            agent="a1",
            topic="t1",
            content="x" * 20,
            file_hint="src/core/auth.py",
        )
        note = store.iter_notes_by_agent("a1")[0]
        assert note.file_hint == "src/core/auth.py"

    def test_content_truncated_at_4000_chars(self, store):
        long = "a" * 6000
        store.put_note(agent="a1", topic="t1", content=long)
        note = store.iter_notes_by_agent("a1")[0]
        assert len(note.content) == 4000

    def test_stats_includes_notes(self, store):
        store.put_note(agent="a1", topic="t1", content="x" * 20)
        store.put_note(agent="a1", topic="t2", content="x" * 20)
        stats = store.stats()
        assert stats["notes"] == 2


# ---------------------------------------------------------------------------
# update_notes tool
# ---------------------------------------------------------------------------


class TestUpdateNotesTool:
    def test_no_factstore_bound_returns_error(self, tmp_path):
        """Called outside a PR review → fail-soft with explanation."""
        result = update_notes(
            workspace=str(tmp_path),
            topic="test",
            content="some observation content that's long enough",
        )
        assert not result.success
        assert "No active scratchpad" in result.error

    def test_happy_path_writes_note(self, store, tmp_path):
        with bind_agent_name("pr_subagent_checks"):
            result = update_notes(
                workspace=str(tmp_path),
                topic="auth_mapping",
                content="Traced OAuth flow through callback at src/auth.py:42. " "State param missing validation.",
            )
        assert result.success
        assert result.data["agent"] == "pr_subagent_checks"
        assert result.data["topic"] == "auth_mapping"
        # Note actually landed in the store
        notes = store.iter_notes_by_agent("pr_subagent_checks")
        assert len(notes) == 1

    def test_missing_agent_name_falls_back(self, store, tmp_path):
        """No agent contextvar bound → unknown_agent label."""
        result = update_notes(
            workspace=str(tmp_path),
            topic="some_topic",
            content="content that is long enough to pass the validator",
        )
        assert result.success
        assert result.data["agent"] == "unknown_agent"

    def test_topic_too_short_rejected(self, store, tmp_path):
        result = update_notes(
            workspace=str(tmp_path),
            topic="a",  # 1 char
            content="x" * 20,
        )
        assert not result.success
        assert "topic" in result.error

    def test_content_too_short_rejected(self, store, tmp_path):
        result = update_notes(
            workspace=str(tmp_path),
            topic="good_topic",
            content="short",
        )
        assert not result.success
        assert "content" in result.error

    def test_file_hint_passes_through(self, store, tmp_path):
        with bind_agent_name("pr_subagent_checks"):
            result = update_notes(
                workspace=str(tmp_path),
                topic="auth_flow",
                content="observation content here " * 3,
                file_hint="src/auth.py",
            )
        assert result.success
        assert result.data["file_hint"] == "src/auth.py"
        notes = store.iter_notes_by_agent("pr_subagent_checks")
        assert notes[0].file_hint == "src/auth.py"

    def test_upsert_via_tool(self, store, tmp_path):
        """Two writes same (agent, topic) via tool API → single latest row."""
        with bind_agent_name("pr_subagent_checks"):
            update_notes(
                workspace=str(tmp_path),
                topic="auth_flow",
                content="first observation placeholder " * 2,
            )
            update_notes(
                workspace=str(tmp_path),
                topic="auth_flow",
                content="refined observation placeholder " * 2,
            )
        notes = store.iter_notes_by_agent("pr_subagent_checks")
        assert len(notes) == 1
        assert "refined" in notes[0].content
