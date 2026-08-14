"""CLI runner for contention comparison — §25.3.

Compares SERIALIZABLE vs READ COMMITTED isolation under concurrent belief writes.
Saves results to eval/results/contention.json.

Usage:
    python -m tests.contention.compare [options]

    --isolation serializable|read_committed|both  (default: both)
    --writers N                                   (default: 8)
    --subject SUBJECT                             (default: C999)
    --predicate PREDICATE                         (default: customer_tier)

Requires COCKROACH_URL environment variable.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Ensure src/ is on the path when run as a module
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from dotenv import load_dotenv
load_dotenv(_REPO_ROOT / ".env")

import psycopg
from psycopg.rows import dict_row

from tests.contention.harness import ContentionHarness, ContentionResult

_RESULTS_DIR = _REPO_ROOT / "eval" / "results"
_OUTPUT_PATH = _RESULTS_DIR / "contention.json"

_DEFAULT_TENANT_ID = uuid.UUID("eeee0000-0000-0000-0000-000000000003")


def _get_conn(url: str) -> psycopg.Connection:
    return psycopg.connect(url, autocommit=False, row_factory=dict_row)  # type: ignore[call-overload]


def _result_to_dict(result: ContentionResult) -> dict:
    return {
        "isolation_level": result.isolation_level,
        "n_writers": result.n_writers,
        "subject": result.subject,
        "predicate": result.predicate,
        "tenant_id": str(result.tenant_id),
        "trusted_count": result.trusted_count,
        "chain_is_total_order": result.chain_is_total_order,
        "all_writes_accounted": result.all_writes_accounted,
        "nothing_lost": result.nothing_lost,
        "total_writes": result.total_writes,
        "chain_length": result.chain_length,
        "contradiction_events_count": result.contradiction_events_count,
        "wall_time_ms": round(result.wall_time_ms, 1),
        "passed": result.passed,
        "failure_reason": result.failure_reason,
    }


def run_comparison(
    url: str,
    isolation: str,
    n_writers: int,
    subject: str,
    predicate: str,
    tenant_id: uuid.UUID,
) -> dict:
    harness = ContentionHarness()
    results: dict[str, dict] = {}
    isolations_to_run: list[str] = []

    if isolation == "both":
        isolations_to_run = ["serializable", "read_committed"]
    else:
        isolations_to_run = [isolation]

    for iso in isolations_to_run:
        print(f"\nRunning {n_writers} writers under {iso.upper()} isolation...")
        print(f"  Subject: {subject}, Predicate: {predicate}")

        # Use a fresh subject suffix per isolation to avoid state contamination
        iso_subject = f"{subject}_{iso[:3].upper()}"

        result = harness.run(
            get_conn=lambda: _get_conn(url),
            tenant_id=tenant_id,
            subject=iso_subject,
            predicate=predicate,
            n_writers=n_writers,
            isolation_level=iso,
        )

        status = "PASSED" if result.passed else "FAILED"
        print(f"  Result: {status}")
        print(f"    trusted_count            : {result.trusted_count}")
        print(f"    chain_is_total_order     : {result.chain_is_total_order}")
        print(f"    all_writes_accounted     : {result.all_writes_accounted}")
        print(f"    total_writes / chain_len : {result.total_writes} / {result.chain_length}")
        print(f"    wall_time_ms             : {result.wall_time_ms:.1f}")
        if result.failure_reason:
            print(f"    failure_reason           : {result.failure_reason}")

        results[iso] = _result_to_dict(result)

    output = {
        "test_date": datetime.now(tz=timezone.utc).isoformat(),
        "test_mode": "live",
        "subject": subject,
        "predicate": predicate,
        "n_writers": n_writers,
        "tenant_id": str(tenant_id),
        "results": results,
    }

    if isolation == "both" and "serializable" in results and "read_committed" in results:
        ser_passed = results["serializable"]["passed"]
        rc_passed = results["read_committed"]["passed"]
        output["conclusion"] = (
            "SERIALIZABLE isolation produced a total order "
            f"(passed={ser_passed}). "
            "READ COMMITTED produced forks "
            f"(passed={rc_passed}, as expected). "
            "SERIALIZABLE isolation is load-bearing for PQBS correctness."
        )

    return output


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare SERIALIZABLE vs READ COMMITTED under concurrent writes."
    )
    parser.add_argument(
        "--isolation",
        choices=["serializable", "read_committed", "both"],
        default="both",
        help="Isolation level(s) to test (default: both)",
    )
    parser.add_argument(
        "--writers",
        type=int,
        default=8,
        help="Number of concurrent writer threads (default: 8)",
    )
    parser.add_argument(
        "--subject",
        default="C999",
        help="Subject for the test belief (default: C999)",
    )
    parser.add_argument(
        "--predicate",
        default="customer_tier",
        help="Predicate for the test belief (default: customer_tier)",
    )
    parser.add_argument(
        "--tenant-id",
        default=str(_DEFAULT_TENANT_ID),
        help=f"Tenant UUID (default: {_DEFAULT_TENANT_ID})",
    )
    args = parser.parse_args()

    url = os.environ.get("COCKROACH_URL")
    if not url:
        print("ERROR: COCKROACH_URL not set in environment / .env", file=sys.stderr)
        sys.exit(1)

    tenant_id = uuid.UUID(args.tenant_id)

    print(f"PQBS Contention Comparison Harness")
    print(f"  Isolation  : {args.isolation}")
    print(f"  Writers    : {args.writers}")
    print(f"  Subject    : {args.subject}")
    print(f"  Predicate  : {args.predicate}")
    print(f"  Tenant     : {tenant_id}")

    output = run_comparison(
        url=url,
        isolation=args.isolation,
        n_writers=args.writers,
        subject=args.subject,
        predicate=args.predicate,
        tenant_id=tenant_id,
    )

    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with _OUTPUT_PATH.open("w") as f:
        json.dump(output, f, indent=2)

    print(f"\nResults saved to: {_OUTPUT_PATH}")

    # Exit 1 if serializable test ran and failed
    if "serializable" in output.get("results", {}):
        if not output["results"]["serializable"]["passed"]:
            print(
                "\nERROR: SERIALIZABLE test FAILED — concurrency invariant violated.",
                file=sys.stderr,
            )
            sys.exit(1)


if __name__ == "__main__":
    main()
