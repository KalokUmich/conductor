#!/usr/bin/env python3
"""Phase 14 A/B reporter: quality + token usage + $ per case per brain model.

Reads `baselines/ab_<label>/` dirs produced by run_ab_severity.sh:
  * `<case>.json`  → quality (severity_accuracy, recommendation_score, composite, catch_rate)
  * `run.log`      → token usage, attributed per case via "===CASE <c> (<label>)===" markers,
                     summed from coordinator "converse DONE ..." lines AND the per-leaf
                     "[sdk_worker usage] ..." lines (Step 14.0).

Applies a Bedrock eu-west-2 price table → $ per case + totals, and $/composite-point so the
Sonnet-vs-Opus-4.8 decision is cost-per-quality, not quality alone.

Usage:  python ab_report.py [label_a label_b]   (default: sonnet opus48)
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys

# --- Bedrock eu-west-2 prices, $ per 1M tokens: (input, output, cache_read, cache_write) ---
# VERIFY against current AWS pricing before quoting externally. Standard Anthropic tiers:
PRICES = {
    # (input, output, cache_read, cache_write_5m) per 1M tokens — Anthropic list prices.
    "haiku": (1.00, 5.00, 0.10, 1.25),
    "sonnet": (3.00, 15.00, 0.30, 3.75),
    "opus": (5.00, 25.00, 0.50, 6.25),  # Opus 4.8 (was wrongly Opus-3 $15/$75)
}
BASE = os.path.join(os.path.dirname(__file__), "baselines")

_CONV = re.compile(r"converse DONE model=(\S+).*?in=(\d+) out=(\d+) cache_read=(\d+) cache_write=(\d+)")
_LEAF = re.compile(
    r"\[sdk_worker usage\] model=(\S+) in=(\d+) out=(\d+) cache_read=(\d+) cache_creation=(\d+)"
)


def _tier(model_id: str) -> str:
    m = model_id.lower()
    return "opus" if "opus" in m else "sonnet" if "sonnet" in m else "haiku"


def _cost(model_id: str, i: int, o: int, cr: int, cw: int) -> float:
    pi, po, pcr, pcw = PRICES[_tier(model_id)]
    return (i * pi + o * po + cr * pcr + cw * pcw) / 1_000_000.0


def _usage_by_case(log_path: str) -> dict:
    """Return {case_id: {'in','out','cache_read','cache_write','cost'}} from a run.log."""
    out: dict = {}
    if not os.path.exists(log_path):
        return out
    cur = None
    with open(log_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            mk = re.search(r"===CASE (\S+) \(", line)
            if mk:
                cur = mk.group(1)
                out.setdefault(cur, {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0})
                continue
            if cur is None:
                continue
            for rx in (_CONV, _LEAF):
                m = rx.search(line)
                if not m:
                    continue
                mid = m.group(1)
                i, o, cr, cw = (int(m.group(k)) for k in range(2, 6))
                d = out[cur]
                d["in"] += i
                d["out"] += o
                d["cache_read"] += cr
                d["cache_write"] += cw
                d["cost"] += _cost(mid, i, o, cr, cw)
    return out


def _quality_by_case(label_dir: str) -> dict:
    """Return {case_id: case_scores} from the per-case baseline JSONs."""
    out: dict = {}
    for f in glob.glob(os.path.join(label_dir, "*.json")):
        try:
            with open(f) as fh:
                d = json.load(fh)
            for cs in d.get("case_scores", []):
                out[cs["case_id"]] = cs
        except Exception:
            pass
    return out


def _load(label: str) -> tuple:
    d = os.path.join(BASE, f"ab_{label}")
    return _quality_by_case(d), _usage_by_case(os.path.join(d, "run.log"))


def _fmt(d: dict, k: str) -> str:
    v = d.get(k)
    return f"{v:.2f}" if isinstance(v, (int, float)) else "  -"


def main() -> None:
    a, b = (sys.argv[1], sys.argv[2]) if len(sys.argv) >= 3 else ("sonnet", "opus48")
    qa, ua = _load(a)
    qb, ub = _load(b)
    cases = sorted(set(qa) | set(qb))
    if not cases:
        print(f"No A/B data under {BASE}/ab_{{{a},{b}}}/ — run run_ab_severity.sh first.")
        return

    print(f"\nPhase 14 A/B — brain={a}  vs  brain={b}   (explorer=Haiku; subset)\n")
    hdr = (
        f"{'case':<24} {'sev ' + a:>9} {'sev ' + b:>9} | "
        f"{'comp ' + a:>9} {'comp ' + b:>9} | {'$ ' + a:>8} {'$ ' + b:>8}"
    )
    print(hdr)
    print("-" * len(hdr))
    tot = {a: {"sev": [], "comp": [], "cost": 0.0}, b: {"sev": [], "comp": [], "cost": 0.0}}
    for c in cases:
        sa = qa.get(c, {})
        sb = qb.get(c, {})
        ca = ua.get(c, {}).get("cost", 0.0)
        cb = ub.get(c, {}).get("cost", 0.0)
        print(
            f"{c:<24} {_fmt(sa, 'severity_accuracy'):>9} {_fmt(sb, 'severity_accuracy'):>9} | "
            f"{_fmt(sa, 'composite'):>9} {_fmt(sb, 'composite'):>9} | {ca:>8.4f} {cb:>8.4f}"
        )
        for lbl, sc, cost in ((a, sa, ca), (b, sb, cb)):
            if isinstance(sc.get("severity_accuracy"), (int, float)):
                tot[lbl]["sev"].append(sc["severity_accuracy"])
            if isinstance(sc.get("composite"), (int, float)):
                tot[lbl]["comp"].append(sc["composite"])
            tot[lbl]["cost"] += cost

    print("-" * len(hdr))
    for lbl in (a, b):
        t = tot[lbl]
        sev = sum(t["sev"]) / len(t["sev"]) if t["sev"] else 0.0
        comp = sum(t["comp"]) / len(t["comp"]) if t["comp"] else 0.0
        comp_sum = sum(t["comp"])
        per_pt = (t["cost"] / comp_sum) if comp_sum else 0.0
        print(
            f"TOTAL {lbl:<10} avg_sev={sev:.3f}  avg_comp={comp:.3f}  "
            f"$={t['cost']:.4f}  $/composite-pt={per_pt:.5f}"
        )
    print("\n(Prices are standard Anthropic tier rates — VERIFY vs current AWS Bedrock eu-west-2.)")


if __name__ == "__main__":
    main()
