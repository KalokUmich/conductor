"""BudgetAnalyzer — self-optimization for BudgetEconomics from telemetry (P3).

The ``task`` table records a per-task ``cost_usd`` (Phase 1f). Over time those
rows are the ground truth for what sub-agents actually cost — far better than the
hand-tuned policy constants in :mod:`budget_economics`. This module reads that
history, computes per-``agent_name`` cost percentiles (p50/p80/p95), and exposes a
:class:`BudgetEconomics`-compatible ``history`` provider so ``estimate()`` blends
its loose policy default toward the measured p80.

Design:
  * **Read-only + best-effort.** Never raises into the Brain; on any DB error or
    no-DB (tests / local mode) the provider returns ``None`` for every key, which
    makes ``BudgetEconomics`` fall back to pure policy — identical behaviour to
    "no history". Self-optimization is an enhancement, never a dependency.
  * **Cached snapshot.** Percentiles are computed once into an in-memory snapshot
    (``refresh()``) rather than per-estimate, so the hot path is a dict lookup.
    A periodic caller (e.g. the ``on_task_end`` Brain hook, or a scheduled job)
    calls ``refresh()`` to fold in new rows.
  * **Keyed by agent_name.** That is the per-leaf signal the cap actually bounds.
    ``query_class`` is accepted for signature-compatibility but unused for now
    (PR/business/generic isn't recorded on the row yet); when it is, add it to the
    GROUP BY without changing the provider contract.

Usage::

    analyzer = BudgetAnalyzer()
    await analyzer.refresh()                       # fold telemetry into a snapshot
    econ = BudgetEconomics(history=analyzer.history_provider())
    plan = econ.estimate("pr", TaskSignals(diff_lines=2000))
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)

#: Ignore agents with fewer than this many completed rows — too little data to
#: trust a percentile (one fluke run would skew the estimate).
_MIN_SAMPLES = 5


class AgentCostStats:
    """p50/p80/p95 cost (USD) + sample count for one agent_name."""

    __slots__ = ("agent_name", "p50", "p80", "p95", "samples")

    def __init__(self, agent_name: str, p50: float, p80: float, p95: float, samples: int):
        self.agent_name = agent_name
        self.p50 = p50
        self.p80 = p80
        self.p95 = p95
        self.samples = samples

    def to_dict(self) -> Dict[str, object]:
        return {
            "agent_name": self.agent_name,
            "p50": round(self.p50, 4),
            "p80": round(self.p80, 4),
            "p95": round(self.p95, 4),
            "samples": self.samples,
        }


def _percentile(sorted_vals: list, q: float) -> float:
    """Linear-interpolation percentile (q in [0,1]). Empty → 0.0."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    idx = q * (len(sorted_vals) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return float(sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac)


def compute_stats(rows_by_agent: Dict[str, list]) -> Dict[str, AgentCostStats]:
    """Pure: {agent_name: [cost_usd, ...]} → {agent_name: AgentCostStats}.

    Agents with < ``_MIN_SAMPLES`` positive-cost rows are dropped (insufficient
    data). Zero/negative costs are filtered (in-house rows that didn't record a
    dollar figure, or bad data) so they don't drag the percentile to 0.
    """
    out: Dict[str, AgentCostStats] = {}
    for agent, costs in rows_by_agent.items():
        vals = sorted(c for c in costs if c and c > 0)
        if len(vals) < _MIN_SAMPLES:
            continue
        out[agent] = AgentCostStats(
            agent_name=agent,
            p50=_percentile(vals, 0.50),
            p80=_percentile(vals, 0.80),
            p95=_percentile(vals, 0.95),
            samples=len(vals),
        )
    return out


class BudgetAnalyzer:
    """Reads ``task`` cost history → per-agent percentiles → history provider."""

    def __init__(self) -> None:
        self._stats: Dict[str, AgentCostStats] = {}

    @property
    def stats(self) -> Dict[str, AgentCostStats]:
        return self._stats

    async def refresh(self) -> int:
        """Recompute the snapshot from the ``task`` table. Returns #agents kept.

        Best-effort: no telemetry service / DB error → snapshot left unchanged
        and 0 returned. Never raises.
        """
        try:
            from .task_telemetry import TaskTelemetryService

            svc = TaskTelemetryService.instance_or_none()
            if svc is None:
                return 0
            rows_by_agent = await self._fetch_costs(svc)
        except Exception as exc:
            logger.debug("[budget_analyzer] refresh skipped: %s", exc)
            return 0

        self._stats = compute_stats(rows_by_agent)
        logger.info(
            "[budget_analyzer] refreshed: %d agent(s) with ≥%d samples",
            len(self._stats),
            _MIN_SAMPLES,
        )
        return len(self._stats)

    async def _fetch_costs(self, svc) -> Dict[str, list]:
        """Pull (agent_name, cost_usd) for completed sub-agent rows."""
        from sqlalchemy import select

        from .task_telemetry import TaskRecord

        rows_by_agent: Dict[str, list] = {}
        async with svc._session_factory() as session:
            stmt = select(TaskRecord.agent_name, TaskRecord.cost_usd).where(
                TaskRecord.agent_name.isnot(None),
                TaskRecord.cost_usd > 0,
            )
            for agent_name, cost in (await session.execute(stmt)).all():
                rows_by_agent.setdefault(agent_name, []).append(float(cost or 0.0))
        return rows_by_agent

    def history_provider(self):
        """Return a :data:`budget_economics.HistoryProvider` over the snapshot.

        Signature: ``(query_class, agent_name) -> Optional[float]`` returning the
        measured **p80** cost for the agent, or ``None`` when unknown (→ policy).
        """

        def provider(query_class: str, agent_name: Optional[str]) -> Optional[float]:
            if not agent_name:
                return None
            st = self._stats.get(agent_name)
            return st.p80 if st is not None else None

        return provider


# ---------------------------------------------------------------------------
# Self-optimization wiring — close the loop in production (P3, deferred-item d)
# ---------------------------------------------------------------------------

#: Process-wide analyzer backing the BudgetEconomics singleton's history.
_analyzer: Optional[BudgetAnalyzer] = None
#: Throttle: only one in-flight refresh at a time (refresh reads the whole task
#: table; firing it on every single task end would hammer the DB pointlessly).
_refresh_inflight = False
#: Hold a strong reference to the in-flight refresh task so it isn't GC'd mid-run.
_refresh_task = None


def install_self_optimization() -> BudgetAnalyzer:
    """Wire the analyzer into the BudgetEconomics singleton + the on_task_end hook.

    Idempotent. After this call:
      * ``get_budget_economics()`` blends its estimates toward measured p80, and
      * each ``on_task_end`` schedules a throttled ``refresh()`` so new task rows
        fold into the snapshot over time.

    Best-effort: hook firing is sync and swallows exceptions, and refresh is a
    fire-and-forget asyncio task — neither can crash the Brain. Call once at
    startup (e.g. main.py lifespan) when telemetry is configured.
    """
    global _analyzer
    if _analyzer is None:
        _analyzer = BudgetAnalyzer()
        from .budget_economics import get_budget_economics

        get_budget_economics().set_history(_analyzer.history_provider())
        from .lifecycle import register_hook

        register_hook("on_task_end", _on_task_end_refresh)
        logger.info("[budget_analyzer] self-optimization installed (history + on_task_end)")
    return _analyzer


def _on_task_end_refresh(ctx) -> None:
    """on_task_end callback: schedule a throttled snapshot refresh (fire-and-forget)."""
    global _refresh_inflight, _refresh_task
    if _analyzer is None or _refresh_inflight:
        return
    try:
        import asyncio

        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no running loop (sync context) — skip; next task end will catch up

    _refresh_inflight = True

    async def _run() -> None:
        global _refresh_inflight
        try:
            await _analyzer.refresh()
        finally:
            _refresh_inflight = False

    # Keep a strong reference so the fire-and-forget task isn't GC'd mid-run.
    _refresh_task = loop.create_task(_run())
