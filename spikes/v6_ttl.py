"""
V6 — Row-Level TTL Behavior Spike
==================================
Question  : Does CockroachDB row-level TTL expire rows promptly enough to
            demonstrate in a demo? What is the effective job schedule?
            Which TTL syntax is supported on this cluster tier?
Method    : Create a table with a very short TTL expiration expression
            (rows expire 10 seconds after insert). Insert 20 rows. Poll
            row count every 10 s for up to 4 minutes. Record when rows
            first start disappearing and when the table is empty.
Pass      : At least some rows are deleted within 3 minutes of their
            expiry time.

This is a throwaway spike. None of this code moves into the product.
Output    : stdout summary + appended/replaced section in docs/VERIFICATIONS.md.
"""

import argparse
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

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
STATEMENT_TIMEOUT = "15s"
ROW_COUNT = 20
TTL_EXPIRE_SECONDS = 10        # rows expire 10 s after creation
TTL_JOB_CRON = "* * * * *"    # every minute is the minimum CockroachDB allows
POLL_INTERVAL_S = 10           # seconds between row-count polls
MAX_WAIT_S = 240               # 4 minutes total wait ceiling


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ts_table(base: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{base}_{ts}"


def ddl(conn: psycopg.Connection, stmt: str) -> None:
    """Execute DDL in autocommit mode (CockroachDB DDL is auto-transactional)."""
    try:
        conn.rollback()
    except Exception:
        pass
    conn.autocommit = True
    conn.execute(pgsql.SQL(stmt))  # type: ignore[arg-type]
    conn.autocommit = False


def count_rows(conn: psycopg.Connection, table: str) -> int:
    conn.autocommit = True
    row = conn.execute(  # type: ignore[call-overload]
        pgsql.SQL("SELECT count(*) FROM {}").format(pgsql.Identifier(table))
    ).fetchone()
    conn.autocommit = False
    return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# TTL syntax variations to attempt  (ordered: most preferred first)
# ---------------------------------------------------------------------------

def _ttl_syntaxes(table: str, expire_s: int) -> list[dict]:
    """Build list of (name, sql) dicts to try in order."""
    base_cols = (
        f"CREATE TABLE {table} ("
        "  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),"
        "  content    TEXT NOT NULL,"
        "  created_at TIMESTAMPTZ NOT NULL DEFAULT now()"
        ") WITH ("
    )
    # NOTE: In CockroachDB TTL, ttl_expiration_expression is a SQL expression
    # embedded as a string literal, so the single-quote around the interval
    # literal inside the expression must be doubled (SQL escape).
    return [
        {
            "name": "ttl_expiration_expression_with_cron",
            "sql": (
                base_cols
                + f"  ttl_expiration_expression = 'created_at + INTERVAL ''{expire_s} seconds''',"
                + "  ttl_job_cron = '* * * * *'"
                + ")"
            ),
        },
        {
            "name": "ttl_expiration_expression_no_cron",
            "sql": (
                base_cols
                + f"  ttl_expiration_expression = 'created_at + INTERVAL ''{expire_s} seconds'''"
                + ")"
            ),
        },
        {
            "name": "ttl_expire_after_with_cron",
            "sql": (
                base_cols
                + f"  ttl_expire_after = '00:00:{expire_s:02d}',"
                + "  ttl_job_cron = '* * * * *'"
                + ")"
            ),
        },
        {
            "name": "ttl_expire_after_no_cron",
            "sql": (
                base_cols
                + f"  ttl_expire_after = '00:00:{expire_s:02d}'"
                + ")"
            ),
        },
    ]


def try_create_table_with_ttl(
    conn: psycopg.Connection,
    table: str,
    expire_s: int,
) -> dict:
    """
    Try each known TTL syntax. Return info about which one worked (if any).
    """
    for syntax in _ttl_syntaxes(table, expire_s):
        sql = syntax["sql"]
        try:
            ddl(conn, sql)
            return {"name": syntax["name"], "success": True, "error": None}
        except Exception as exc:
            err = str(exc).split("\n")[0][:120]
            print(f"    ⚠️  {syntax['name']} failed: {err}")
            # try to drop partial table before next attempt
            try:
                ddl(conn, f"DROP TABLE IF EXISTS {table}")
            except Exception:
                pass

    return {"name": None, "success": False, "error": "no TTL syntax succeeded"}


# ---------------------------------------------------------------------------
# Show TTL settings
# ---------------------------------------------------------------------------

def show_ttl_settings(conn: psycopg.Connection, table: str) -> dict:
    """Query crdb_internal to find TTL job settings for the table."""
    conn.autocommit = True
    try:
        rows = conn.execute(
            """
            SELECT attribute, value
            FROM crdb_internal.table_columns
            WHERE descriptor_name = %s
            LIMIT 10
            """,
            [table],
        ).fetchall()
        info = {r[0]: r[1] for r in rows} if rows else {}
    except Exception:
        info = {}

    # Try storage_params from pg_class
    try:
        row = conn.execute(
            """
            SELECT reloptions
            FROM pg_class
            WHERE relname = %s
            """,
            [table],
        ).fetchone()
        reloptions = row[0] if row else None
    except Exception:
        reloptions = None

    conn.autocommit = False
    return {"table_columns": info, "reloptions": reloptions}


# ---------------------------------------------------------------------------
# Poll for expiry
# ---------------------------------------------------------------------------

def poll_until_empty(
    conn: psycopg.Connection,
    table: str,
    expire_s: int,
    max_wait_s: int,
    poll_interval_s: int,
) -> dict:
    """
    Poll row count until empty or timeout. Records timeline.
    Returns timing data and whether TTL ran within the window.
    """
    print(f"\n  Polling every {poll_interval_s}s for up to {max_wait_s}s …")
    print(f"  Rows should be eligible for deletion in {expire_s}s")
    print(f"  TTL job runs every minute — expect first deletions at ~60–70s\n")

    timeline = []
    first_deletion_s = None
    empty_at_s = None
    t_start = time.time()

    initial_count = count_rows(conn, table)
    elapsed = time.time() - t_start
    timeline.append({"elapsed_s": round(elapsed), "count": initial_count})
    print(f"  t={elapsed:5.0f}s  count={initial_count}")

    while True:
        time.sleep(poll_interval_s)
        elapsed = time.time() - t_start
        cnt = count_rows(conn, table)
        timeline.append({"elapsed_s": round(elapsed), "count": cnt})
        print(f"  t={elapsed:5.0f}s  count={cnt}")

        if cnt < initial_count and first_deletion_s is None:
            first_deletion_s = elapsed
            print(f"  *** First rows deleted at t={elapsed:.0f}s "
                  f"({elapsed - expire_s:.0f}s after eligibility) ***")

        if cnt == 0:
            empty_at_s = elapsed
            print(f"  *** Table empty at t={elapsed:.0f}s ***")
            break

        if elapsed >= max_wait_s:
            print(f"  *** Timeout reached ({max_wait_s}s) — {cnt} rows remain ***")
            break

    return {
        "initial_count": initial_count,
        "first_deletion_s": first_deletion_s,
        "empty_at_s": empty_at_s,
        "total_elapsed_s": time.time() - t_start,
        "timeline": timeline,
        "timed_out": empty_at_s is None,
        "expire_s": expire_s,
    }


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def print_summary(
    syntax_result: dict,
    ttl_settings: dict,
    poll_result: dict,
    passed: bool,
) -> None:
    print("\n" + "=" * 70)
    print("V6 SUMMARY")
    print("=" * 70)

    if syntax_result["success"]:
        print(f"  TTL syntax used     : {syntax_result['name']}")
    else:
        print(f"  TTL syntax          : ❌ NONE worked")

    reloptions = ttl_settings.get("reloptions")
    if reloptions:
        print(f"  Table reloptions    : {reloptions}")
    else:
        print(f"  Table reloptions    : (not readable)")

    expire_s = poll_result["expire_s"]
    print(f"  TTL expire_after    : {expire_s}s")
    print(f"  Rows inserted       : {poll_result['initial_count']}")

    fds = poll_result["first_deletion_s"]
    eas = poll_result["empty_at_s"]
    lag = (fds - expire_s) if fds is not None else None

    print(f"  First deletion at   : "
          f"{fds:.0f}s elapsed ({lag:.0f}s after eligibility)" if fds else "  First deletion at   : not observed")
    print(f"  Table empty at      : {eas:.0f}s elapsed" if eas else
          f"  Table empty at      : not observed (rows remain after {poll_result['total_elapsed_s']:.0f}s)")
    print(f"  Timed out           : {'YES' if poll_result['timed_out'] else 'NO'}")
    print()

    if passed:
        print("  RESULT: ✅ PASSED")
        print("  TTL rows are deleted within the demo window.")
        print("  `working_memory` table may safely use row-level TTL.")
    else:
        print("  RESULT: ❌ FAILED")
        print("  TTL deletions not observed within the wait window.")
        print("  Fallback: explicit deletion job. `working_memory` claim")
        print("  weakens from storage-enforced to policy-enforced.")

    print("=" * 70)


# ---------------------------------------------------------------------------
# VERIFICATIONS.md report
# ---------------------------------------------------------------------------

def write_verifications_report(
    syntax_result: dict,
    ttl_settings: dict,
    poll_result: dict,
    passed: bool,
) -> None:
    report_path = Path("docs/VERIFICATIONS.md")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    existing = report_path.read_text() if report_path.exists() else ""

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    passed_str = "✅ PASSED" if passed else "❌ FAILED"

    expire_s = poll_result["expire_s"]
    fds = poll_result["first_deletion_s"]
    eas = poll_result["empty_at_s"]
    lag = (fds - expire_s) if fds is not None else None

    syntax_name = syntax_result["name"] or "none"
    reloptions = ttl_settings.get("reloptions") or "(not readable from information_schema)"

    if passed:
        fallback = "None — V6 passed. `working_memory` will use row-level TTL."
        consequence = (
            f"`working_memory` table (migration `0010_working_memory`) will use "
            f"`{syntax_name}` TTL syntax. Job runs every minute; rows are eligible "
            f"{expire_s}s after creation. First deletions observed ~{fds:.0f}s "
            f"after insert ({lag:.0f}s lag past eligibility)."
        )
    else:
        fallback = (
            "V6 FAILED. `working_memory` TTL must be implemented as an explicit "
            "deletion job (e.g., scheduled Lambda). The 'forgetting' claim weakens "
            "from storage-enforced to policy-enforced. Disclose in README."
        )
        consequence = (
            "Explicit deletion job required. Rows are not automatically evicted "
            f"within {poll_result['total_elapsed_s']:.0f}s."
        )

    timeline_rows = "\n".join(
        f"| {p['elapsed_s']:>5}s | {p['count']:>5} |"
        for p in poll_result["timeline"]
    )

    entry = f"""## V6 — Row-Level TTL Behavior

**Run date:** {timestamp}
**Cluster:** CockroachDB Serverless (COCKROACH_URL)
**TTL expire_after:** {expire_s}s
**TTL job cron:** `{TTL_JOB_CRON}` (every minute)
**Rows inserted:** {poll_result['initial_count']}
**Result:** {passed_str}

### TTL syntax

Working syntax: `{syntax_name}`
Table reloptions: `{reloptions}`

### Expiry timeline

| Elapsed | Row count |
|---------|-----------|
{timeline_rows}

**First deletion observed:** {'t=' + str(int(fds)) + 's (' + str(int(lag)) + 's after eligibility)' if fds is not None and lag is not None else 'not observed'}
**Table empty at:** {'t=' + str(int(eas)) + 's' if eas is not None else 'not observed (rows remain)'}

### Consequence for Phase 2

{consequence}

**Active fallback:** {fallback}

"""

    pattern = r"## V6 — Row-Level TTL Behavior\n.*?(?=\n## |\Z)"
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
        description="V6 — Row-level TTL behavior spike for PQBS."
    )
    parser.add_argument(
        "--expire-s", type=int, default=TTL_EXPIRE_SECONDS,
        help=f"Seconds until rows are eligible for TTL deletion (default: {TTL_EXPIRE_SECONDS})"
    )
    parser.add_argument(
        "--max-wait", type=int, default=MAX_WAIT_S,
        help=f"Maximum seconds to wait for rows to disappear (default: {MAX_WAIT_S})"
    )
    parser.add_argument(
        "--rows", type=int, default=ROW_COUNT,
        help=f"Number of rows to insert (default: {ROW_COUNT})"
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

    table = ts_table("v6_ttl_spike")

    print("=" * 70)
    print("V6 — Row-Level TTL Behavior")
    print(f"Cluster : {COCKROACH_URL.split('@')[-1].split('/')[0]}")
    print(f"Table   : {table}")
    print(f"TTL     : rows expire {args.expire_s}s after created_at")
    print(f"Rows    : {args.rows}")
    print(f"Max wait: {args.max_wait}s")
    print("=" * 70)

    # ── Connect ──────────────────────────────────────────────────────────────
    try:
        conn = psycopg.connect(COCKROACH_URL, connect_timeout=CONNECT_TIMEOUT)
        conn.autocommit = True
        conn.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")
        conn.autocommit = False
    except Exception as exc:
        sys.exit(f"ERROR: Cannot connect to CockroachDB: {exc}")

    # ── Create table with TTL ─────────────────────────────────────────────────
    print("\n[1/4] Creating table with TTL …")
    syntax_result = try_create_table_with_ttl(conn, table, args.expire_s)
    if not syntax_result["success"]:
        print(f"  ❌ No TTL syntax succeeded.")
        conn.close()
        # write failure report
        poll_result = {
            "initial_count": 0,
            "first_deletion_s": None,
            "empty_at_s": None,
            "total_elapsed_s": 0,
            "timeline": [],
            "timed_out": True,
            "expire_s": args.expire_s,
        }
        if not args.no_report:
            write_verifications_report(syntax_result, {}, poll_result, passed=False)
        sys.exit(1)

    print(f"  ✅ Table created using syntax: {syntax_result['name']}")

    # ── Read TTL settings ─────────────────────────────────────────────────────
    print("\n[2/4] Reading TTL settings …")
    ttl_settings = show_ttl_settings(conn, table)
    reloptions = ttl_settings.get("reloptions")
    if reloptions:
        print(f"  reloptions: {reloptions}")
    else:
        print("  reloptions: (not accessible from information schema — normal on serverless)")

    # ── Insert rows ───────────────────────────────────────────────────────────
    print(f"\n[3/4] Inserting {args.rows} rows …")
    conn.autocommit = True
    for i in range(args.rows):
        conn.execute(  # type: ignore[call-overload]
            pgsql.SQL("INSERT INTO {} (content) VALUES (%s)").format(pgsql.Identifier(table)),
            [f"working_memory_entry_{i:04d}"],
        )
    conn.autocommit = False
    initial = count_rows(conn, table)
    print(f"  {initial} rows inserted.")

    # ── Poll until empty ──────────────────────────────────────────────────────
    print(f"\n[4/4] Waiting for TTL job to delete rows …")
    poll_result = poll_until_empty(
        conn, table, args.expire_s, args.max_wait, POLL_INTERVAL_S
    )
    poll_result["initial_count"] = initial

    # ── Pass / Fail ───────────────────────────────────────────────────────────
    passed = poll_result["first_deletion_s"] is not None

    # ── Summary ───────────────────────────────────────────────────────────────
    print_summary(syntax_result, ttl_settings, poll_result, passed)

    # ── Cleanup ───────────────────────────────────────────────────────────────
    if not args.keep_table:
        print("\nDropping spike table …")
        try:
            ddl(conn, f"DROP TABLE IF EXISTS {table} CASCADE")
            print("  OK")
        except Exception as exc:
            print(f"  WARNING: {exc}")
            print(f"  Run manually: DROP TABLE IF EXISTS {table} CASCADE;")

    conn.close()

    # ── Write report ──────────────────────────────────────────────────────────
    if not args.no_report:
        write_verifications_report(syntax_result, ttl_settings, poll_result, passed)

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
