"""Tests for the SessionTrace module and TraceWriter backends."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.agent_loop.trace import (
    IterationTrace,
    SessionTrace,
    ToolCallTrace,
    TraceWriter,
)


class TestToolCallTrace:
    def test_defaults(self):
        tc = ToolCallTrace()
        assert tc.tool_name == ""
        assert tc.success is True
        assert tc.latency_ms == 0.0

    def test_with_values(self):
        tc = ToolCallTrace(
            tool_name="grep",
            params={"pattern": "auth"},
            success=True,
            result_chars=500,
            latency_ms=12.5,
            new_files=0,
            new_symbols=0,
        )
        assert tc.tool_name == "grep"
        assert tc.result_chars == 500


class TestIterationTrace:
    def test_defaults(self):
        it = IterationTrace()
        assert it.iteration == 0
        assert it.budget_signal == "normal"
        assert it.tool_calls == []

    def test_with_tool_calls(self):
        it = IterationTrace(
            iteration=1,
            input_tokens=5000,
            output_tokens=200,
            llm_latency_ms=800.0,
            tool_calls=[
                ToolCallTrace(tool_name="grep", latency_ms=15.0),
                ToolCallTrace(tool_name="read_file", latency_ms=5.0),
            ],
        )
        assert len(it.tool_calls) == 2
        assert it.input_tokens == 5000


class TestSessionTrace:
    def test_defaults(self):
        st = SessionTrace()
        assert st.session_id == ""
        assert st.iterations == []
        assert st.duration_ms == 0.0

    def test_begin_sets_start_time(self):
        st = SessionTrace(session_id="test123")
        st.begin()
        assert st.start_time > 0

    def test_finish_sets_end_time_and_totals(self):
        st = SessionTrace(session_id="test123")
        st.begin()
        st.add_iteration(IterationTrace(
            iteration=1, input_tokens=1000, output_tokens=200,
            tool_calls=[ToolCallTrace(tool_name="grep")],
        ))
        st.add_iteration(IterationTrace(
            iteration=2, input_tokens=2000, output_tokens=300,
            tool_calls=[
                ToolCallTrace(tool_name="read_file"),
                ToolCallTrace(tool_name="find_symbol"),
            ],
        ))
        st.finish(answer="The answer is 42.", budget_summary={"total_tokens": 3500})
        assert st.end_time > st.start_time
        assert st.total_input_tokens == 3000
        assert st.total_output_tokens == 500
        assert st.total_tool_calls == 3
        assert st.final_answer_chars == len("The answer is 42.")
        assert st.error is None
        assert st.budget_summary == {"total_tokens": 3500}

    def test_finish_with_error(self):
        st = SessionTrace(session_id="test123")
        st.begin()
        st.finish(error="Max iterations reached")
        assert st.error == "Max iterations reached"

    def test_duration_ms(self):
        st = SessionTrace()
        st.start_time = 100.0
        st.end_time = 100.5
        assert st.duration_ms == 500.0

    def test_duration_ms_no_end(self):
        st = SessionTrace()
        st.start_time = 100.0
        assert st.duration_ms == 0.0

    def test_to_dict(self):
        st = SessionTrace(session_id="abc", query="how does auth work?")
        st.start_time = 100.0
        st.end_time = 101.0
        d = st.to_dict()
        assert d["session_id"] == "abc"
        assert d["query"] == "how does auth work?"
        assert d["duration_ms"] == 1000.0
        assert "iterations" in d

    def test_add_iteration(self):
        st = SessionTrace()
        assert len(st.iterations) == 0
        st.add_iteration(IterationTrace(iteration=1))
        st.add_iteration(IterationTrace(iteration=2))
        assert len(st.iterations) == 2


# ---------------------------------------------------------------------------
# TraceWriter — local backend
# ---------------------------------------------------------------------------


class TestTraceWriterLocal:
    def _make_trace(self, session_id="trace001"):
        st = SessionTrace(session_id=session_id, query="test query")
        st.begin()
        st.add_iteration(IterationTrace(iteration=1, input_tokens=100))
        st.finish(answer="done")
        return st

    def test_save_creates_file(self, tmp_path):
        writer = TraceWriter(local_path=str(tmp_path))
        trace = self._make_trace()
        assert writer.save(trace) is True
        path = tmp_path / "trace001.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["session_id"] == "trace001"
        assert data["total_input_tokens"] == 100

    def test_save_creates_directory(self, tmp_path):
        nested = tmp_path / "deep" / "dir"
        writer = TraceWriter(local_path=str(nested))
        assert writer.save(self._make_trace()) is True
        assert nested.exists()

    def test_disabled_returns_false(self, tmp_path):
        writer = TraceWriter(enabled=False, local_path=str(tmp_path))
        assert writer.save(self._make_trace()) is False

    def test_no_session_id_returns_false(self, tmp_path):
        writer = TraceWriter(local_path=str(tmp_path))
        trace = SessionTrace()
        trace.begin()
        trace.finish()
        assert writer.save(trace) is False


# ---------------------------------------------------------------------------
# TraceWriter — database backend
# ---------------------------------------------------------------------------


class TestTraceWriterDatabase:
    def _make_trace(self, session_id="db_trace001"):
        st = SessionTrace(session_id=session_id, query="how does auth work?")
        st.begin()
        st.add_iteration(IterationTrace(
            iteration=1, input_tokens=5000, output_tokens=200,
            tool_calls=[ToolCallTrace(tool_name="grep", latency_ms=15.0)],
        ))
        st.finish(answer="Auth uses JWT tokens.", budget_summary={"total": 5200})
        return st

    def test_save_to_sqlite(self, tmp_path):
        db_path = tmp_path / "traces.db"
        writer = TraceWriter(backend="database", database_url=f"sqlite:///{db_path}")
        trace = self._make_trace()
        assert writer.save(trace) is True

        # Verify the row was written
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT session_id, total_input_tokens, total_tool_calls, trace_json "
            "FROM session_traces WHERE session_id = ?",
            ("db_trace001",),
        ).fetchone()
        conn.close()

        assert row is not None
        assert row[0] == "db_trace001"
        assert row[1] == 5000
        assert row[2] == 1
        # trace_json should be valid JSON
        data = json.loads(row[3])
        assert data["query"] == "how does auth work?"

    def test_save_multiple_sessions(self, tmp_path):
        db_path = tmp_path / "traces.db"
        writer = TraceWriter(backend="database", database_url=f"sqlite:///{db_path}")
        writer.save(self._make_trace("sess1"))
        writer.save(self._make_trace("sess2"))
        writer.save(self._make_trace("sess3"))

        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM session_traces").fetchone()[0]
        conn.close()
        assert count == 3

    def test_upsert_on_duplicate_session_id(self, tmp_path):
        db_path = tmp_path / "traces.db"
        writer = TraceWriter(backend="database", database_url=f"sqlite:///{db_path}")
        writer.save(self._make_trace("dup001"))
        writer.save(self._make_trace("dup001"))

        conn = sqlite3.connect(str(db_path))
        count = conn.execute(
            "SELECT COUNT(*) FROM session_traces WHERE session_id = 'dup001'"
        ).fetchone()[0]
        conn.close()
        assert count == 1

    def test_bare_path_treated_as_sqlite(self, tmp_path):
        db_path = tmp_path / "bare.db"
        writer = TraceWriter(backend="database", database_url=str(db_path))
        assert writer.save(self._make_trace("bare001")) is True

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT session_id FROM session_traces WHERE session_id = 'bare001'"
        ).fetchone()
        conn.close()
        assert row is not None

    def test_db_creates_parent_directory(self, tmp_path):
        db_path = tmp_path / "deep" / "nested" / "traces.db"
        writer = TraceWriter(backend="database", database_url=f"sqlite:///{db_path}")
        assert writer.save(self._make_trace()) is True
        assert db_path.exists()

    def test_disabled_db_returns_false(self, tmp_path):
        writer = TraceWriter(
            enabled=False, backend="database",
            database_url=f"sqlite:///{tmp_path / 'x.db'}",
        )
        assert writer.save(self._make_trace()) is False

    def test_query_by_token_usage(self, tmp_path):
        """Verify structured columns support analytical queries."""
        db_path = tmp_path / "analysis.db"
        writer = TraceWriter(backend="database", database_url=f"sqlite:///{db_path}")

        # Save traces with different token usage
        for i, tokens in enumerate([1000, 50000, 200000]):
            t = SessionTrace(session_id=f"q{i}", query=f"query {i}")
            t.begin()
            t.add_iteration(IterationTrace(input_tokens=tokens))
            t.finish(answer="ok")
            writer.save(t)

        conn = sqlite3.connect(str(db_path))
        # Find the most expensive query
        row = conn.execute(
            "SELECT session_id, total_input_tokens FROM session_traces "
            "ORDER BY total_input_tokens DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert row[0] == "q2"
        assert row[1] == 200000


# ---------------------------------------------------------------------------
# TraceWriter.from_settings
# ---------------------------------------------------------------------------


class TestTraceWriterFromSettings:
    def test_from_settings_local(self):
        class FakeSettings:
            enabled = True
            backend = "local"
            local_path = "/tmp/traces"
            database_url = ""

        writer = TraceWriter.from_settings(FakeSettings())
        assert writer.enabled is True
        assert writer.backend == "local"
        assert writer.local_path == "/tmp/traces"

    def test_from_settings_database(self):
        class FakeSettings:
            enabled = True
            backend = "database"
            local_path = ""
            database_url = "sqlite:///traces.db"

        writer = TraceWriter.from_settings(FakeSettings())
        assert writer.backend == "database"
        assert writer.database_url == "sqlite:///traces.db"
