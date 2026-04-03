"""Tests for the SessionTrace module and TraceWriter backends."""

from __future__ import annotations

import json

import pytest

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
        st.add_iteration(
            IterationTrace(
                iteration=1,
                input_tokens=1000,
                output_tokens=200,
                tool_calls=[ToolCallTrace(tool_name="grep")],
            )
        )
        st.add_iteration(
            IterationTrace(
                iteration=2,
                input_tokens=2000,
                output_tokens=300,
                tool_calls=[
                    ToolCallTrace(tool_name="read_file"),
                    ToolCallTrace(tool_name="find_symbol"),
                ],
            )
        )
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
        st.add_iteration(
            IterationTrace(
                iteration=1,
                input_tokens=5000,
                output_tokens=200,
                tool_calls=[ToolCallTrace(tool_name="grep", latency_ms=15.0)],
            )
        )
        st.finish(answer="Auth uses JWT tokens.", budget_summary={"total": 5200})
        return st

    @pytest.mark.asyncio
    async def test_save_to_db_async(self, db_engine):
        """Test saving a trace to the async database backend."""
        writer = TraceWriter(backend="database", engine=db_engine)
        trace = self._make_trace()
        assert await writer.save_async(trace) is True

        # Verify by querying with SQLAlchemy
        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from app.db.models import SessionTraceRecord

        session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with session_factory() as session:
            result = await session.execute(
                select(SessionTraceRecord).where(SessionTraceRecord.session_id == "db_trace001")
            )
            row = result.scalar_one_or_none()
            assert row is not None
            assert row.total_input_tokens == 5000
            assert row.total_tool_calls == 1
            data = json.loads(row.trace_json)
            assert data["query"] == "how does auth work?"

    @pytest.mark.asyncio
    async def test_save_multiple_sessions(self, db_engine):
        writer = TraceWriter(backend="database", engine=db_engine)
        for sid in ("sess1", "sess2", "sess3"):
            await writer.save_async(self._make_trace(sid))

        from sqlalchemy import func, select
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from app.db.models import SessionTraceRecord

        session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with session_factory() as session:
            result = await session.execute(select(func.count()).select_from(SessionTraceRecord))
            assert result.scalar() == 3

    @pytest.mark.asyncio
    async def test_upsert_on_duplicate_session_id(self, db_engine):
        writer = TraceWriter(backend="database", engine=db_engine)
        await writer.save_async(self._make_trace("dup001"))
        await writer.save_async(self._make_trace("dup001"))

        from sqlalchemy import func, select
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from app.db.models import SessionTraceRecord

        session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with session_factory() as session:
            result = await session.execute(
                select(func.count()).select_from(SessionTraceRecord).where(SessionTraceRecord.session_id == "dup001")
            )
            assert result.scalar() == 1

    def test_disabled_db_returns_false(self, tmp_path):
        writer = TraceWriter(enabled=False, backend="database")
        assert writer.save(self._make_trace()) is False

    @pytest.mark.asyncio
    async def test_query_by_token_usage(self, db_engine):
        """Verify structured columns support analytical queries."""
        writer = TraceWriter(backend="database", engine=db_engine)

        for i, tokens in enumerate([1000, 50000, 200000]):
            t = SessionTrace(session_id=f"q{i}", query=f"query {i}")
            t.begin()
            t.add_iteration(IterationTrace(input_tokens=tokens))
            t.finish(answer="ok")
            await writer.save_async(t)

        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from app.db.models import SessionTraceRecord

        session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with session_factory() as session:
            result = await session.execute(
                select(SessionTraceRecord).order_by(SessionTraceRecord.total_input_tokens.desc()).limit(1)
            )
            row = result.scalar_one()
            assert row.session_id == "q2"
            assert row.total_input_tokens == 200000


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
