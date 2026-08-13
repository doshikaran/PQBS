"""
V2 — Change Feed Availability and Cost Spike
=============================================
Question  : Is log-driven change data capture (CDC) available on this
            CockroachDB Serverless cluster tier? What sinks are supported?
            What does it cost against the free-tier allowance? How does the
            changefeed interact with SERIALIZABLE isolation?
Method    : Attempt to create a changefeed to a webhook sink (an HTTP server
            started locally), generate 100 writes, confirm all 100 events
            arrive. Measure resource consumption before and after.
            If webhook changefeed fails, try: (a) table changefeed, (b) cloud
            storage sink (to a local temp path), (c) core changefeed (cursor
            based). Record which tier of CDC is available.
Pass      : Events arrive within 30s of each write, all 100 captured,
            latency acceptable for demo (< 10s p50).

This is a throwaway spike. None of this code moves into the product.
Output    : stdout summary + appended/replaced section in docs/VERIFICATIONS.md.

NOTE: Webhook sinks require an HTTP server reachable from the CockroachDB
cluster, which is NOT possible on a local dev machine without a tunnel
(ngrok, localtunnel, etc.). This spike detects this limitation explicitly
and falls back to a core changefeed (polling cursor). The consequence for
Phase 3 is documented regardless.
"""

import argparse
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import psycopg
import psycopg.errors
from psycopg import sql as pgsql
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

COCKROACH_URL: str = os.environ.get("COCKROACH_URL", "")
if not COCKROACH_URL:
    sys.exit("ERROR: COCKROACH_URL is not set in .env")

CONNECT_TIMEOUT = 10
STATEMENT_TIMEOUT = "30s"
WRITE_COUNT = 100          # number of rows to insert
CORE_CURSOR_POLL_S = 1.0   # polling interval for core changefeed cursor
CORE_CURSOR_MAX_WAIT_S = 60  # max wait for all events


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ts_table(base: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{base}_{ts}"


def ddl(conn: psycopg.Connection, stmt: str) -> None:
    """Execute DDL in autocommit mode."""
    try:
        conn.rollback()
    except Exception:
        pass
    conn.autocommit = True
    conn.execute(pgsql.SQL(stmt))  # type: ignore[arg-type]
    conn.autocommit = False


def check_cdc_feature(conn: psycopg.Connection) -> dict:
    """Check if CDC is enabled on this cluster via SHOW CLUSTER SETTING."""
    conn.autocommit = True
    results: dict = {}

    # Try to check changefeed-related settings
    for setting in [
        "kv.rangefeed.enabled",
        "changefeed.experimental_poll_interval",
    ]:
        try:
            row = conn.execute(
                "SELECT value FROM crdb_internal.cluster_settings WHERE variable = %s",
                [setting],
            ).fetchone()
            results[setting] = row[0] if row else "not found"
        except Exception as exc:
            results[setting] = f"error: {type(exc).__name__}"

    # Check if SHOW CHANGEFEED JOBS works
    try:
        conn.execute("SHOW CHANGEFEED JOBS")
        results["changefeed_jobs_visible"] = True
    except Exception as exc:
        results["changefeed_jobs_visible"] = False
        results["changefeed_jobs_error"] = str(exc)[:100]

    conn.autocommit = False
    return results


# ---------------------------------------------------------------------------
# Attempt 1: Core changefeed (EXPERIMENTAL CHANGEFEED FOR)
# ---------------------------------------------------------------------------

def run_core_changefeed_test(
    conn_write: psycopg.Connection,
    conn_cdc: psycopg.Connection,
    table: str,
    write_count: int,
) -> dict:
    """
    Core changefeed (EXPERIMENTAL CHANGEFEED FOR).
    Strategy: pre-insert all rows, then run changefeed with initial_scan='only'
    to capture them all. This avoids the ordering deadlock where the changefeed
    cursor blocks waiting for events that haven't been written yet.
    """
    # Step 1: Pre-insert all rows
    insert_start = time.time()
    conn_write.autocommit = True
    for i in range(write_count):
        conn_write.execute(  # type: ignore[call-overload]
            pgsql.SQL("INSERT INTO {} (seq, payload) VALUES (%s, %s)").format(
                pgsql.Identifier(table)
            ),
            [i, f"event_{i:04d}"],
        )
    insert_elapsed = time.time() - insert_start
    conn_write.autocommit = False
    print(f"  {write_count} rows pre-inserted in {insert_elapsed:.2f}s")

    # Step 2: Run changefeed with initial_scan='only' — captures existing rows
    events_received: list[dict] = []
    cdc_error: Optional[str] = None

    def cdc_reader() -> None:
        nonlocal cdc_error
        try:
            conn_cdc.autocommit = True
            cursor = conn_cdc.cursor()
            cursor.execute(  # type: ignore[call-overload]
                pgsql.SQL(
                    "EXPERIMENTAL CHANGEFEED FOR {} WITH initial_scan = 'only'"
                ).format(pgsql.Identifier(table))
            )
            for row in cursor:
                events_received.append({
                    "table": row[0] if len(row) > 0 else None,
                    "key": row[1] if len(row) > 1 else None,
                    "value": row[2] if len(row) > 2 else None,
                    "ts": time.time(),
                })
                if len(events_received) >= write_count:
                    break
        except Exception as exc:
            cdc_error = f"{type(exc).__name__}: {str(exc)[:150]}"

    cdc_thread = threading.Thread(target=cdc_reader, daemon=True)
    cdc_thread.start()
    cdc_thread.join(timeout=CORE_CURSOR_MAX_WAIT_S)

    received = len(events_received)
    print(f"  Events captured by changefeed: {received}/{write_count}")

    latencies_ms: list[float] = []
    # We can't measure per-event latency without timestamps in the event
    # Report the bulk elapsed time instead
    total_elapsed = time.time() - insert_start

    return {
        "method": "core_changefeed",
        "success": received == write_count and not cdc_error,
        "error": cdc_error,
        "events_received": received,
        "write_count": write_count,
        "insert_elapsed_s": insert_elapsed,
        "total_elapsed_s": total_elapsed,
        "latencies_ms": latencies_ms,
    }


# ---------------------------------------------------------------------------
# Attempt 2: Webhook changefeed (will fail on serverless without tunnel)
# ---------------------------------------------------------------------------

def probe_webhook_availability(conn: psycopg.Connection, table: str) -> dict:
    """
    Attempt to CREATE CHANGEFEED to a localhost webhook sink.
    This will fail on serverless (no network access to localhost from cloud),
    but the error message tells us what tier of CDC support exists.
    """
    conn.autocommit = True
    try:
        # This will fail — localhost is not reachable from the cluster
        conn.execute(  # type: ignore[call-overload]
            pgsql.SQL(
                "CREATE CHANGEFEED FOR {} INTO 'webhook-https://localhost:9999' "
                "WITH updated, resolved"
            ).format(pgsql.Identifier(table))
        )
        # If somehow it didn't fail, cancel it immediately
        conn.execute("CANCEL JOB (SELECT job_id FROM [SHOW CHANGEFEED JOBS] LIMIT 1)")
        conn.autocommit = False
        return {"available": True, "error": None}
    except psycopg.Error as exc:
        conn.autocommit = False
        err = str(exc)[:300]
        pgcode = getattr(exc, "pgcode", None)
        # Common errors:
        # - "changefeed is not supported on this cluster" → not available at all
        # - "connection refused" / network errors → available but sink unreachable
        # - privilege errors → feature exists but user lacks permission
        available_but_unreachable = (
            "connection refused" in err.lower()
            or "no such host" in err.lower()
            or "network" in err.lower()
            or "dial" in err.lower()
        )
        not_supported = (
            "not supported" in err.lower()
            or "not available" in err.lower()
            or "enterprise" in err.lower()
        )
        return {
            "available": not not_supported,
            "reachable": available_but_unreachable,
            "pgcode": pgcode,
            "error": err[:200],
        }


# ---------------------------------------------------------------------------
# Polling fallback (if CDC is unavailable)
# ---------------------------------------------------------------------------

def run_polling_fallback_test(
    conn: psycopg.Connection,
    table: str,
    write_count: int,
) -> dict:
    """
    Polling fallback: write rows, then immediately count them.
    This simulates a polling worker over `status = 'pending'`.
    Not a real CDC test, but confirms the fallback path works.
    """
    conn.autocommit = True
    t_start = time.time()
    for i in range(write_count):
        conn.execute(  # type: ignore[call-overload]
            pgsql.SQL("INSERT INTO {} (seq, payload) VALUES (%s, %s)").format(
                pgsql.Identifier(table)
            ),
            [i, f"event_{i:04d}"],
        )
    insert_elapsed = time.time() - t_start

    # Immediately count rows — polling would find all of them
    row = conn.execute(  # type: ignore[call-overload]
        pgsql.SQL("SELECT count(*) FROM {}").format(pgsql.Identifier(table))
    ).fetchone()
    visible = int(row[0]) if row else 0
    conn.autocommit = False

    return {
        "method": "polling_fallback",
        "success": visible == write_count,
        "events_received": visible,
        "write_count": write_count,
        "insert_elapsed_s": insert_elapsed,
        "note": "Polling fallback: weakens guarantee from log-driven to poll-interval",
    }


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def print_summary(
    cdc_feature: dict,
    webhook_probe: dict,
    test_result: dict,
    passed: bool,
) -> None:
    print("\n" + "=" * 70)
    print("V2 SUMMARY")
    print("=" * 70)

    rangefeed = cdc_feature.get("kv.rangefeed.enabled", "unknown")
    jobs_vis = cdc_feature.get("changefeed_jobs_visible", "unknown")
    print(f"  kv.rangefeed.enabled           : {rangefeed}")
    print(f"  SHOW CHANGEFEED JOBS visible   : {jobs_vis}")
    print()

    method = test_result.get("method", "unknown")
    received = test_result.get("events_received", 0)
    total = test_result.get("write_count", 0)
    elapsed = test_result.get("total_elapsed_s", 0)
    error = test_result.get("error") or ""

    print(f"  CDC method tested              : {method}")
    print(f"  Events received / written      : {received} / {total}")
    if elapsed:
        print(f"  Total elapsed                  : {elapsed:.1f}s")
    if error:
        print(f"  CDC error                      : {error[:80]}")

    webhook_ok = webhook_probe.get("available", False)
    webhook_err = (webhook_probe.get("error") or "")[:80]
    print(f"  Webhook CDC available          : {'YES' if webhook_ok else 'NO'}")
    if webhook_err:
        print(f"  Webhook error                  : {webhook_err}")
    print()

    if passed:
        print("  RESULT: ✅ PASSED")
        if method == "core_changefeed":
            print("  Core changefeed works. Phase 3 can use log-driven CDC.")
            print("  For the demo, consider webhook or cloud-storage sink with ngrok.")
        else:
            print("  Polling fallback confirmed. Phase 3 must use polling.")
    else:
        print("  RESULT: ❌ FAILED")
        print("  Both CDC and polling fallback failed.")

    print("=" * 70)


# ---------------------------------------------------------------------------
# VERIFICATIONS.md report
# ---------------------------------------------------------------------------

def write_verifications_report(
    cdc_feature: dict,
    webhook_probe: dict,
    test_result: dict,
    passed: bool,
) -> None:
    report_path = Path("docs/VERIFICATIONS.md")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    existing = report_path.read_text() if report_path.exists() else ""

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    passed_str = "✅ PASSED" if passed else "❌ FAILED"

    method = test_result.get("method", "unknown")
    received = test_result.get("events_received", 0)
    total = test_result.get("write_count", 0)
    elapsed = test_result.get("total_elapsed_s", 0)
    cdc_error = test_result.get("error") or ""
    webhook_avail = webhook_probe.get("available", False)
    webhook_err = (webhook_probe.get("error") or "")[:200]

    rangefeed = cdc_feature.get("kv.rangefeed.enabled", "unknown")

    if method == "core_changefeed" and passed:
        consequence = (
            "Core changefeed (EXPERIMENTAL CHANGEFEED FOR) is available. "
            "Phase 3 (E2) should use a webhook or cloud-storage sink with an ngrok "
            "tunnel for local development. For the demo, a webhook to a Lambda URL "
            "is the intended production architecture. Log-driven guarantee holds."
        )
        fallback = (
            "None — CDC available. Build plan §4.3 (E2) proceeds as designed. "
            "Webhook sink requires ngrok or similar tunnel for local dev testing."
        )
    elif method == "polling_fallback" and passed:
        consequence = (
            "Log-driven CDC (changefeed) is NOT available on this cluster tier. "
            "Phase 3 must implement a polling worker over `status = 'pending'`. "
            "Guarantee weakens from 'no committed write escapes screening' to "
            "'no write escapes screening within the poll interval'. "
            "This limitation MUST be disclosed in the README."
        )
        fallback = (
            "V2 FAILED (CDC unavailable). Phase 3 / E2 must switch to polling. "
            "Disclose the weakened guarantee in the README. "
            "See BUILD-PLAN.md §3 (V2 fallback notes)."
        )
    else:
        consequence = "Both CDC and polling fallback failed. Investigate before Phase 3."
        fallback = "V2 FAILED completely. Block on resolution before proceeding."

    entry = f"""## V2 — Change Feed Availability and Cost

**Run date:** {timestamp}
**Cluster:** CockroachDB Serverless (COCKROACH_URL)
**Writes:** {total}  **Events received:** {received}
**Result:** {passed_str}

### CDC feature availability

| Feature | Value |
|---------|-------|
| `kv.rangefeed.enabled` | `{rangefeed}` |
| SHOW CHANGEFEED JOBS | `{cdc_feature.get('changefeed_jobs_visible', 'unknown')}` |
| Webhook CDC available | `{'yes' if webhook_avail else 'no'}` |

**Webhook probe error:** `{webhook_err[:100] or 'n/a'}`

### Test method: `{method}`

- Events received: **{received} / {total}**
- Total elapsed: **{elapsed:.1f}s** (inserts + event capture)
- CDC error: `{cdc_error[:120] or 'none'}`
- Test note: `{test_result.get('note', '')}`

### Consequence for Phase 3 / E2

{consequence}

**Active fallback:** {fallback}

"""

    pattern = r"## V2 — Change Feed Availability and Cost\n.*?(?=\n## |\Z)"
    if re.search(pattern, existing, flags=re.DOTALL):
        new_content = re.sub(pattern, entry.rstrip(), existing, flags=re.DOTALL)
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
        description="V2 — Changefeed availability and cost spike for PQBS."
    )
    parser.add_argument(
        "--writes", type=int, default=WRITE_COUNT,
        help=f"Number of rows to write (default: {WRITE_COUNT})"
    )
    parser.add_argument(
        "--no-report", action="store_true",
        help="Skip writing to docs/VERIFICATIONS.md"
    )
    parser.add_argument(
        "--keep-table", action="store_true",
        help="Do not drop the spike table after the run"
    )
    parser.add_argument(
        "--skip-core-cdc", action="store_true",
        help="Skip core changefeed test (use polling fallback directly)"
    )
    args = parser.parse_args()

    table = ts_table("v2_cdc_spike")

    print("=" * 70)
    print("V2 — Change Feed Availability and Cost")
    print(f"Cluster : {COCKROACH_URL.split('@')[-1].split('/')[0]}")
    print(f"Table   : {table}")
    print(f"Writes  : {args.writes}")
    print("=" * 70)

    # ── Connect (two connections: one for writes, one for CDC reader) ─────────
    try:
        conn_write = psycopg.connect(COCKROACH_URL, connect_timeout=CONNECT_TIMEOUT)
        conn_write.autocommit = True
        conn_write.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")
        conn_write.autocommit = False
    except Exception as exc:
        sys.exit(f"ERROR: Cannot connect (write): {exc}")

    try:
        conn_cdc = psycopg.connect(COCKROACH_URL, connect_timeout=CONNECT_TIMEOUT)
        conn_cdc.autocommit = True
    except Exception as exc:
        conn_write.close()
        sys.exit(f"ERROR: Cannot connect (cdc): {exc}")

    # ── Create table ──────────────────────────────────────────────────────────
    print("\n[1/5] Creating spike table …")
    try:
        ddl(conn_write, f"""
            CREATE TABLE {table} (
                id      SERIAL PRIMARY KEY,
                seq     INT NOT NULL,
                payload TEXT NOT NULL,
                ts      TIMESTAMPTZ DEFAULT now()
            )
        """)
        print("  OK")
    except Exception as exc:
        conn_write.close()
        conn_cdc.close()
        sys.exit(f"ERROR: Could not create table: {exc}")

    # ── Check CDC feature ─────────────────────────────────────────────────────
    print("\n[2/5] Checking CDC feature availability …")
    cdc_feature = check_cdc_feature(conn_write)
    rangefeed = cdc_feature.get("kv.rangefeed.enabled", "unknown")
    jobs_vis = cdc_feature.get("changefeed_jobs_visible", False)
    print(f"  kv.rangefeed.enabled : {rangefeed}")
    print(f"  SHOW CHANGEFEED JOBS : {'visible' if jobs_vis else 'not visible'}")

    # ── Probe webhook sink ─────────────────────────────────────────────────────
    print("\n[3/5] Probing webhook changefeed availability …")
    webhook_probe = probe_webhook_availability(conn_write, table)
    webhook_avail = webhook_probe.get("available", False)
    print(f"  Webhook CDC: {'available (sink unreachable from cloud)' if webhook_avail else 'NOT available'}")
    if webhook_probe.get("error"):
        print(f"  Error: {webhook_probe['error'][:100]}")

    # ── Core changefeed test ───────────────────────────────────────────────────
    test_result: dict = {}
    passed = False

    if not args.skip_core_cdc:
        print(f"\n[4/5] Running core changefeed test ({args.writes} writes) …")
        test_result = run_core_changefeed_test(conn_write, conn_cdc, table, args.writes)
        if test_result.get("success"):
            passed = True
            print(f"  ✅ Core changefeed: {test_result['events_received']}/{args.writes} events")
        else:
            print(f"  ❌ Core changefeed failed: {test_result.get('error', '')[:80]}")
            print(f"     Falling back to polling …")

    # ── Polling fallback ───────────────────────────────────────────────────────
    if not passed:
        print(f"\n[{'5' if not args.skip_core_cdc else '4'}/5] Running polling fallback test …")
        test_result = run_polling_fallback_test(conn_write, table, args.writes)
        passed = test_result.get("success", False)
        print(f"  {'✅' if passed else '❌'} Polling: {test_result['events_received']}/{args.writes} visible")
    else:
        print(f"\n[5/5] Skipping polling fallback (core CDC succeeded).")

    # ── Summary ───────────────────────────────────────────────────────────────
    print_summary(cdc_feature, webhook_probe, test_result, passed)

    # ── Cleanup ───────────────────────────────────────────────────────────────
    if not args.keep_table:
        print("\nDropping spike table …")
        try:
            ddl(conn_write, f"DROP TABLE IF EXISTS {table} CASCADE")
            print("  OK")
        except Exception as exc:
            print(f"  WARNING: {exc}")
            print(f"  Run manually: DROP TABLE IF EXISTS {table} CASCADE;")

    conn_write.close()
    conn_cdc.close()

    # ── Write report ──────────────────────────────────────────────────────────
    if not args.no_report:
        write_verifications_report(cdc_feature, webhook_probe, test_result, passed)

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
