"""
V5 — Serializable Retry Determinism Spike
==========================================
Question  : Can we reliably force an observable serializable retry on demand?
Method    : Two concurrent transactions read the same row, then both try to write
            it with different values. One commits; the other must receive SQLSTATE
            40001 (SerializationFailure) because its read set is now stale.
Instrument: Catch the exact exception class and pgcode; record timing window.
Runs      : 25 (configurable via --runs flag)
Pass      : SQLSTATE 40001 fires on ≥90% of runs with the scripted timing pattern.

This is a throwaway spike. None of this code moves into the product.
Output    : stdout summary + appended section in docs/VERIFICATIONS.md.
"""

import argparse
import os
import re
import random
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import psycopg.errors
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

COCKROACH_URL: str = os.environ.get("COCKROACH_URL", "")
if not COCKROACH_URL:
    sys.exit("ERROR: COCKROACH_URL is not set in .env")

PASS_THRESHOLD = 0.90          # ≥90% conflict rate to pass
DEFAULT_RUNS = 25
# Jitter window (seconds) added between barrier and write to force overlap
WRITE_JITTER_MIN = 0.001
WRITE_JITTER_MAX = 0.008
CONNECT_TIMEOUT = 10  # seconds; prevents hanging on network issues
STATEMENT_TIMEOUT = "15s"


# ---------------------------------------------------------------------------
# Schema helpers  (table name passed explicitly — avoids global mutable state)
# ---------------------------------------------------------------------------

def create_spike_table(conn: psycopg.Connection, table: str) -> None:
    conn.autocommit = True  # DDL auto-commits; no open txn to block on
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {table} (
            id          INT PRIMARY KEY,
            value       TEXT NOT NULL,
            updated_at  TIMESTAMPTZ DEFAULT now()
        )
    """)
    conn.execute(f"""
        INSERT INTO {table} (id, value)
        VALUES (1, 'initial')
        ON CONFLICT (id) DO UPDATE
            SET value      = 'initial',
                updated_at = now()
    """)
    conn.autocommit = False


def drop_spike_table(conn: psycopg.Connection, table: str) -> None:
    conn.autocommit = True
    conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.autocommit = False


def reset_row(conn: psycopg.Connection, table: str) -> None:
    conn.execute(
        f"UPDATE {table} SET value = 'initial', updated_at = now() WHERE id = 1"
    )
    conn.commit()


def read_final_value(conn: psycopg.Connection, table: str) -> str:
    row = conn.execute(f"SELECT value FROM {table} WHERE id = 1").fetchone()
    return row[0] if row else "<missing>"


# ---------------------------------------------------------------------------
# Core contention pair
# ---------------------------------------------------------------------------

def run_one_pair(run_number: int, table: str) -> dict:
    """
    Spawn two threads. Each opens a SERIALIZABLE transaction, reads row id=1,
    waits at a barrier (so both have read before either writes), then writes a
    distinct value and commits.

    Plain SELECT (no FOR UPDATE): conflict is detected at COMMIT time under
    serializable isolation. FOR UPDATE would make one thread block on the lock
    instead of both reading and then conflicting at commit — causing a hang.

    Returns a result dict:
      conflict   : bool   — True if SQLSTATE 40001 was observed on at least one thread
      sqlstate   : str    — the pgcode observed ('40001' or None)
      winner     : str    — which thread committed first ('thread_a' or 'thread_b')
      final_value: str    — the value in the row after both threads complete
      errors     : list   — unexpected errors (neither success nor 40001)
      duration_ms: float  — wall-clock time for the pair
    """
    barrier = threading.Barrier(2, timeout=10)

    slots: dict = {
        "thread_a": {"committed": False, "conflict": False, "sqlstate": None, "error": None},
        "thread_b": {"committed": False, "conflict": False, "sqlstate": None, "error": None},
    }

    def worker(name: str, write_value: str) -> None:
        try:
            with psycopg.connect(COCKROACH_URL, connect_timeout=CONNECT_TIMEOUT) as conn:
                conn.autocommit = False
                conn.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")

                # ── Step 1: READ ──────────────────────────────────────────
                conn.execute(f"SELECT value FROM {table} WHERE id = 1").fetchone()

                # ── Step 2: SYNC — both threads have read; now both write ─
                barrier.wait()

                # Tiny jitter so commits land close but not identical in time
                time.sleep(random.uniform(WRITE_JITTER_MIN, WRITE_JITTER_MAX))

                # ── Step 3: WRITE ─────────────────────────────────────────
                conn.execute(
                    f"UPDATE {table} SET value = %s, updated_at = now() WHERE id = 1",
                    [write_value],
                )

                # ── Step 4: COMMIT ────────────────────────────────────────
                conn.commit()
                slots[name]["committed"] = True

        except psycopg.errors.SerializationFailure as exc:
            slots[name]["conflict"] = True
            slots[name]["sqlstate"] = getattr(exc, "pgcode", None) or "40001"

        except psycopg.Error as exc:
            pgcode = getattr(exc, "pgcode", None)
            if pgcode == "40001":
                slots[name]["conflict"] = True
                slots[name]["sqlstate"] = pgcode
            else:
                slots[name]["error"] = f"[{pgcode}] {type(exc).__name__}: {exc}"

        except threading.BrokenBarrierError:
            slots[name]["error"] = "BrokenBarrierError — peer thread timed out at barrier"

        except Exception as exc:
            slots[name]["error"] = f"{type(exc).__name__}: {exc}"

    t_a = threading.Thread(
        target=worker, args=("thread_a", f"writer_A_run{run_number}"), daemon=True
    )
    t_b = threading.Thread(
        target=worker, args=("thread_b", f"writer_B_run{run_number}"), daemon=True
    )

    start = time.perf_counter()
    t_a.start()
    t_b.start()
    t_a.join(timeout=15)
    t_b.join(timeout=15)
    duration_ms = (time.perf_counter() - start) * 1000

    conflict = slots["thread_a"]["conflict"] or slots["thread_b"]["conflict"]
    sqlstate = slots["thread_a"]["sqlstate"] or slots["thread_b"]["sqlstate"]
    winner = (
        "thread_a" if slots["thread_a"]["committed"]
        else "thread_b" if slots["thread_b"]["committed"]
        else "none"
    )
    errors = [s["error"] for s in slots.values() if s["error"]]

    # Read the actual committed value back
    try:
        with psycopg.connect(COCKROACH_URL, connect_timeout=CONNECT_TIMEOUT) as conn:
            final_value = read_final_value(conn, table)
    except Exception:
        final_value = "<read error>"

    return {
        "run": run_number,
        "conflict": conflict,
        "sqlstate": sqlstate,
        "winner": winner,
        "final_value": final_value,
        "errors": errors,
        "duration_ms": duration_ms,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_run_result(r: dict) -> None:
    icon = "✅" if r["conflict"] else ("⚠️" if not r["errors"] else "❌")
    conflict_str = f"SQLSTATE {r['sqlstate']}" if r["conflict"] else "no conflict"
    error_str = f"  ERRORS: {r['errors']}" if r["errors"] else ""
    print(
        f"  Run {r['run']:>2} | {icon} {conflict_str:<20} | "
        f"winner={r['winner']:<8} | value={r['final_value']:<30} | "
        f"{r['duration_ms']:.0f}ms{error_str}"
    )


def print_summary(results: list[dict], pass_threshold: float) -> float:
    total = len(results)
    conflicts = sum(1 for r in results if r["conflict"])
    no_conflicts = sum(1 for r in results if not r["conflict"] and not r["errors"])
    errors = sum(1 for r in results if r["errors"])
    pass_rate = conflicts / total if total > 0 else 0.0

    durations = [r["duration_ms"] for r in results]
    avg_ms = sum(durations) / len(durations)
    max_ms = max(durations)

    all_sqlstates = sorted({r["sqlstate"] for r in results if r["sqlstate"]})

    print("\n" + "=" * 70)
    print("V5 SUMMARY")
    print("=" * 70)
    print(f"  Total runs          : {total}")
    print(f"  Conflicts (40001)   : {conflicts}  ({pass_rate * 100:.1f}%)")
    print(f"  No conflict         : {no_conflicts}")
    print(f"  Unexpected errors   : {errors}")
    print(f"  SQLSTATEs observed  : {', '.join(all_sqlstates) if all_sqlstates else 'none'}")
    print(f"  Avg pair duration   : {avg_ms:.0f}ms")
    print(f"  Max pair duration   : {max_ms:.0f}ms")
    print(f"  Pass threshold      : ≥{pass_threshold * 100:.0f}%")
    print()

    if pass_rate >= pass_threshold:
        print(f"  RESULT: ✅ PASSED ({pass_rate * 100:.1f}% ≥ {pass_threshold * 100:.0f}%)")
        print("  Serializable retries fire reliably. Proceed to Phase 1.")
    elif pass_rate > 0:
        print(f"  RESULT: ❌ FAILED ({pass_rate * 100:.1f}% < {pass_threshold * 100:.0f}%)")
        print("  Retries fire inconsistently. Investigate before proceeding.")
        print("  See BUILD-PLAN.md §2.7 for the fallback narrative.")
    else:
        print("  RESULT: ❌ FAILED — zero serializable conflicts observed.")
        print("  Either isolation level is not SERIALIZABLE, or the conflict")
        print("  pattern is not generating overlapping read/write sets.")
        print("  See BUILD-PLAN.md §2.7 — re-plan demo narrative.")

    print("=" * 70)
    return pass_rate


def write_verifications_report(results: list[dict], pass_rate: float) -> None:
    report_path = Path("docs/VERIFICATIONS.md")
    report_path.parent.mkdir(parents=True, exist_ok=True)

    existing = report_path.read_text() if report_path.exists() else ""

    conflicts = sum(1 for r in results if r["conflict"])
    total = len(results)
    all_sqlstates = sorted({r["sqlstate"] for r in results if r["sqlstate"]})
    durations = [r["duration_ms"] for r in results]
    avg_ms = sum(durations) / len(durations)

    passed_str = "✅ PASSED" if pass_rate >= PASS_THRESHOLD else "❌ FAILED"
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if pass_rate >= PASS_THRESHOLD:
        fallback = (
            "None — V5 passed. Proceeding with serializable concurrency demo as the "
            "central demonstration (§26.7)."
        )
    else:
        fallback = (
            "V5 FAILED. Demo narrative must shift to quarantine + temporal "
            "reconstruction as the central moments. Document this decision before "
            "proceeding to Phase 1. See BUILD-PLAN.md §2.7."
        )

    entry = f"""## V5 — Serializable Retry Determinism

**Run date:** {timestamp}
**Cluster:** CockroachDB Serverless (COCKROACH_URL)
**Runs:** {total}
**Conflicts detected:** {conflicts}/{total} ({pass_rate * 100:.1f}%)
**Pass criterion:** ≥{PASS_THRESHOLD * 100:.0f}% of runs
**Result:** {passed_str}

**SQLSTATE observed:** `{", ".join(all_sqlstates) if all_sqlstates else "none"}`

**Timing window:** Both threads read row id=1 under SERIALIZABLE isolation,
synchronised at a barrier, then each attempted to write a distinct value.
Jitter of {WRITE_JITTER_MIN * 1000:.0f}–{WRITE_JITTER_MAX * 1000:.0f}ms applied between barrier and write.
Average pair duration: {avg_ms:.0f}ms.

**Findings:**
- Serializable isolation {'is' if pass_rate >= PASS_THRESHOLD else 'is NOT'} reliably producing SQLSTATE 40001
  on concurrent read-write conflicts.
- The retry wrapper (Phase 3, E1) must catch `psycopg.errors.SerializationFailure`
  (pgcode `{"`, `".join(all_sqlstates) if all_sqlstates else "40001"}`).
- The Phase 3 / Phase 8 contention harness (`tests/contention/`) will use the same
  timing pattern with 8–16 concurrent writers.

**Active fallback:** {fallback}

"""

    v5_pattern = r"## V5 — Serializable Retry Determinism\n.*?(?=\n## |\Z)"
    if re.search(v5_pattern, existing, flags=re.DOTALL):
        new_content = re.sub(v5_pattern, entry.rstrip(), existing, flags=re.DOTALL)
        report_path.write_text(new_content)
    else:
        separator = "\n\n---\n\n" if existing.strip() else ""
        report_path.write_text(existing + separator + entry)

    print(f"\nFindings written → {report_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="V5 — Serializable retry determinism spike for PQBS."
    )
    parser.add_argument(
        "--runs", type=int, default=DEFAULT_RUNS,
        help=f"Number of contention pairs to run (default: {DEFAULT_RUNS})"
    )
    parser.add_argument(
        "--no-report", action="store_true",
        help="Skip writing to docs/VERIFICATIONS.md"
    )
    parser.add_argument(
        "--keep-table", action="store_true",
        help="Do not drop the spike table after the run"
    )
    args = parser.parse_args()

    # Unique table name per run avoids lock contention from previous hung runs
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    spike_table = f"v5_contention_{run_ts}"

    print("=" * 70)
    print("V5 — Serializable Retry Determinism")
    print(f"Cluster : {COCKROACH_URL.split('@')[-1].split('/')[0]}")
    print(f"Table   : {spike_table}")
    print(f"Runs    : {args.runs}")
    print(f"Pass    : ≥{PASS_THRESHOLD * 100:.0f}% conflicts")
    print("=" * 70)

    # ── Setup ────────────────────────────────────────────────────────────────
    try:
        setup_conn = psycopg.connect(COCKROACH_URL, connect_timeout=CONNECT_TIMEOUT)
        setup_conn.autocommit = True
        setup_conn.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")
        setup_conn.autocommit = False
    except Exception as exc:
        sys.exit(f"ERROR: Cannot connect to CockroachDB: {exc}")

    print("\nCreating spike table…")
    try:
        create_spike_table(setup_conn, spike_table)
        print("  OK")
    except Exception as exc:
        setup_conn.close()
        sys.exit(f"ERROR: Could not create spike table: {exc}")

    # ── Runs ─────────────────────────────────────────────────────────────────
    results: list[dict] = []
    print(f"\nRunning {args.runs} contention pairs:\n")

    for i in range(1, args.runs + 1):
        try:
            result = run_one_pair(i, spike_table)
        except Exception as exc:
            result = {
                "run": i,
                "conflict": False,
                "sqlstate": None,
                "winner": "none",
                "final_value": "<error>",
                "errors": [f"Unhandled: {exc}"],
                "duration_ms": 0,
            }

        results.append(result)
        print_run_result(result)

        # Reset the row to 'initial' for the next run
        try:
            reset_row(setup_conn, spike_table)
        except Exception:
            pass  # non-fatal; next run will still conflict

    # ── Summary ──────────────────────────────────────────────────────────────
    pass_rate = print_summary(results, PASS_THRESHOLD)

    # ── Cleanup ──────────────────────────────────────────────────────────────
    if not args.keep_table:
        print("\nDropping spike table…")
        try:
            drop_spike_table(setup_conn, spike_table)
            print("  OK")
        except Exception as exc:
            print(f"  WARNING: Could not drop spike table: {exc}")
            print(f"  Run manually: DROP TABLE IF EXISTS {spike_table};")

    setup_conn.close()

    # ── Write report ─────────────────────────────────────────────────────────
    if not args.no_report:
        write_verifications_report(results, pass_rate)

    # Exit code reflects pass/fail for CI usage
    sys.exit(0 if pass_rate >= PASS_THRESHOLD else 1)


if __name__ == "__main__":
    main()
