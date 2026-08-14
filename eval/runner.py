"""Phase 8 evaluation runner — runs A15 RedTeamAgent against live CockroachDB.

Usage:
    python eval/runner.py [--out eval/results/metrics_live.json]

Requires COCKROACH_URL and TENANT_ID_EVAL in .env.
Seeds eval tenant before running if agent_identity rows are missing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from dotenv import load_dotenv
load_dotenv(_REPO_ROOT / ".env")

import psycopg
from psycopg.rows import dict_row

from pqbs.agents.integrity.a15_redteam import RedTeamAgent, _EVAL_TENANT_ID

_DEFAULT_OUT = _REPO_ROOT / "eval" / "results" / "metrics_live.json"


def _get_conn(url: str) -> psycopg.Connection:
    return psycopg.connect(url, autocommit=False, row_factory=dict_row)  # type: ignore[call-overload]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Phase 8 red-team evaluation.")
    parser.add_argument("--out", default=str(_DEFAULT_OUT), help="Output JSON path")
    args = parser.parse_args()

    url = os.environ.get("COCKROACH_URL")
    if not url:
        print("ERROR: COCKROACH_URL not set", file=sys.stderr)
        sys.exit(1)

    eval_tid = _EVAL_TENANT_ID
    print(f"PQBS Phase 8 Evaluation Runner")
    print(f"  Eval tenant : {eval_tid}")
    print(f"  Corpus dir  : {_REPO_ROOT / 'eval' / 'corpus'}")
    print(f"  Output      : {args.out}")
    print()

    conn = _get_conn(url)
    try:
        agent = RedTeamAgent(eval_tenant_id=eval_tid)
        t0 = time.perf_counter()
        print("Running evaluation (350 entries)...")
        report = agent.run_evaluation(conn)
        elapsed = time.perf_counter() - t0

        m = report.metrics
        print(f"\n{'='*60}")
        print(f"Evaluation complete in {elapsed:.1f}s")
        print(f"  detection_rate_overall  : {m.detection_rate_overall:.3f}")
        print(f"  false_positive_rate     : {m.false_positive_rate:.3f}")
        print(f"  evasion_resistance      : {m.evasion_resistance:.3f}")
        print(f"  cascade_completeness    : {m.cascade_completeness:.3f}")
        print(f"  time_to_quarantine_p50  : {m.time_to_quarantine_p50_ms:.1f} ms")
        print(f"  time_to_quarantine_p99  : {m.time_to_quarantine_p99_ms:.1f} ms")
        if m.detection_rate_by_class:
            print(f"  detection by class:")
            for cls, rate in sorted(m.detection_rate_by_class.items()):
                print(f"    {cls}: {rate:.3f}")
        if m.signal_marginal_contributions:
            print(f"  signal marginal contributions (top 3):")
            top = sorted(m.signal_marginal_contributions.items(), key=lambda x: -x[1])[:3]
            for sig, mc in top:
                print(f"    {sig}: {mc:.3f}")
        print(f"{'='*60}")

        agent.save_report(report, args.out)
        print(f"\nReport saved to: {args.out}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
