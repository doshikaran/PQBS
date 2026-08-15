"""
RC Fork Diagnostic v3 — simple concurrent race, clean subjects.

Pre-seeds B0 as trusted, then releases N_WRITERS threads simultaneously.
Threads race to supersede B0 and promote their own belief.

Under SERIALIZABLE: losers get 40001. At most 1 thread commits trusted.
Under READ COMMITTED: no 40001. Multiple threads may each promote to trusted
after failing to see B0 as still trusted (B0 already superseded by winner),
producing trusted_count > 1 — a FORK.

Subjects are unique per run (timestamp-based RUN_ID) to prevent contamination.
"""
from __future__ import annotations
import os, sys, threading, time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4, UUID

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
from dotenv import load_dotenv
load_dotenv(_REPO_ROOT / ".env")

import psycopg, psycopg.errors
from psycopg.rows import dict_row
from psycopg import sql as pgsql

URL = os.environ["COCKROACH_URL"]
TENANT = UUID("eeee0000-0000-0000-0000-000000000003")
N_WRITERS = 12
N_TRIALS = 5
RUN_ID = str(int(time.time()))[-6:]  # last 6 digits of epoch — unique per run
ZERO_DIGEST = "0" * 64


def aconn():
    return psycopg.connect(URL, autocommit=True, row_factory=dict_row)


def seed(subject: str, predicate: str) -> str:
    """Insert B0 as trusted. Verify no prior trusted beliefs exist first."""
    conn = aconn()
    prior = conn.execute(
        "SELECT COUNT(*) AS n FROM belief WHERE tenant_id=%s AND subject=%s AND predicate=%s AND status='trusted'",
        (str(TENANT), subject, predicate)
    ).fetchone()["n"]
    if prior > 0:
        conn.close()
        raise RuntimeError(
            f"Subject {subject} already has {prior} trusted beliefs — contamination."
        )

    b0 = str(uuid4())
    p0 = str(uuid4())
    ep0 = str(uuid4())
    now = datetime.now(tz=timezone.utc)
    conn.execute(
        """INSERT INTO provenance
            (provenance_id, tenant_id, source_type, source_uri,
             source_digest, episode_id, derived_from, ingested_at,
             source_trust_tier, ingestion_agent_id)
           VALUES (%s, %s, 'user_statement', 'rc:/seed',
                   %s, %s, '[]', %s, 'authoritative', 'diag')""",
        (p0, str(TENANT), ZERO_DIGEST, ep0, now),
    )
    conn.execute(
        """INSERT INTO belief
            (tenant_id, belief_id, subject, predicate, object, confidence,
             valid_from, status, author_agent_id, provenance_id, sensitivity)
           VALUES (%s, %s, %s, %s, 'B0_seed', 0.99, %s,
                   'trusted'::belief_status, 'diag', %s, 'normal'::sensitivity)""",
        (str(TENANT), b0, subject, predicate, now, p0),
    )
    conn.close()
    return b0


def count_trusted(subject: str, predicate: str) -> int:
    conn = aconn()
    rows = conn.execute(
        "SELECT belief_id, status FROM belief WHERE tenant_id=%s AND subject=%s AND predicate=%s",
        (str(TENANT), subject, predicate),
    ).fetchall()
    conn.close()
    return sum(1 for r in rows if r["status"] == "trusted")


def writer_fn(
    isolation: str,
    subject: str,
    predicate: str,
    idx: int,
    start_event: threading.Event,
    log: list,
    errors_40001: list,
):
    iso_val = isolation.replace("_", " ")
    conn = psycopg.connect(URL, autocommit=False, row_factory=dict_row)
    conn.execute(
        pgsql.SQL("SET default_transaction_isolation = {}").format(pgsql.Literal(iso_val))
    )
    my_id = str(uuid4())
    p_id = str(uuid4())
    ep_id = str(uuid4())
    now = datetime.now(tz=timezone.utc)

    # Pre-insert pending belief BEFORE race starts (reduces per-thread work during race window)
    with conn.transaction():
        conn.execute(
            """INSERT INTO provenance
                (provenance_id, tenant_id, source_type, source_uri,
                 source_digest, episode_id, derived_from, ingested_at,
                 source_trust_tier, ingestion_agent_id)
               VALUES (%s, %s, 'user_statement', 'rc:/writer',
                       %s, %s, '[]', %s, 'unverified', 'diag')""",
            (p_id, str(TENANT), ZERO_DIGEST, ep_id, now),
        )
        conn.execute(
            """INSERT INTO belief
                (tenant_id, belief_id, subject, predicate, object, confidence,
                 valid_from, status, author_agent_id, provenance_id, sensitivity)
               VALUES (%s, %s, %s, %s, %s, 0.5, %s,
                       'pending'::belief_status, 'diag', %s, 'normal'::sensitivity)""",
            (str(TENANT), my_id, subject, predicate, f"t{idx}_value", now, p_id),
        )

    start_event.wait()  # All threads hold here — released simultaneously

    try:
        with conn.transaction():
            incumbent = conn.execute(
                """SELECT belief_id FROM belief
                   WHERE tenant_id=%s AND subject=%s AND predicate=%s AND status='trusted'
                   ORDER BY tx_from DESC LIMIT 1""",
                (str(TENANT), subject, predicate),
            ).fetchone()
            inc_id = str(incumbent["belief_id"]) if incumbent else None

            if inc_id and inc_id != my_id:
                conn.execute(
                    """UPDATE belief SET valid_to=%s, superseded_by=%s, status='superseded'::belief_status
                       WHERE belief_id=%s AND tenant_id=%s AND status='trusted'""",
                    (now, my_id, inc_id, str(TENANT)),
                )
                conn.execute(
                    "UPDATE belief SET supersedes=%s WHERE belief_id=%s AND tenant_id=%s",
                    (inc_id, my_id, str(TENANT)),
                )

            conn.execute(
                """UPDATE belief SET status='trusted'::belief_status
                   WHERE belief_id=%s AND tenant_id=%s AND status='pending'""",
                (my_id, str(TENANT)),
            )
        log[idx] = {"committed": True, "error": None}
    except psycopg.errors.SerializationFailure:
        errors_40001[0] += 1
        log[idx] = {"committed": False, "error": "40001"}
    except Exception as e:
        log[idx] = {"committed": False, "error": str(e)[:100]}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def run_isolation(isolation: str):
    print(f"\n{'='*60}")
    print(f"Isolation: {isolation.upper()} — {N_TRIALS} trials, {N_WRITERS} writers each")
    all_trusted, total_40001, forks = [], 0, 0

    for trial in range(N_TRIALS):
        subject = f"F{RUN_ID}_{trial:02d}_{isolation[:3].upper()}"
        seed(subject, "customer_tier")

        log: list = [{}] * N_WRITERS
        errors_40001 = [0]
        start_event = threading.Event()

        threads = [
            threading.Thread(
                target=writer_fn,
                args=(isolation, subject, "customer_tier", i, start_event, log, errors_40001),
                daemon=True,
            )
            for i in range(N_WRITERS)
        ]
        for t in threads:
            t.start()
        time.sleep(0.5)  # let all threads reach start_event.wait()
        t0 = time.perf_counter()
        start_event.set()
        for t in threads:
            t.join(timeout=120)
        wall_ms = (time.perf_counter() - t0) * 1000

        tc = count_trusted(subject, "customer_tier")
        all_trusted.append(tc)
        total_40001 += errors_40001[0]
        if tc > 1:
            forks += 1
        committed = sum(1 for lentry in log if lentry.get("committed"))
        fork_flag = " ← FORK" if tc > 1 else ""
        print(
            f"  T{trial+1}: trusted={tc}  committed={committed}/{N_WRITERS}"
            f"  40001={errors_40001[0]}  wall={wall_ms:.0f}ms{fork_flag}"
        )
        if tc > 1:
            for i, lentry in enumerate(log):
                print(f"    t{i}: committed={lentry.get('committed')}, err={lentry.get('error')}")

    print(
        f"\n  SUMMARY: trusted per trial={all_trusted}, "
        f"forks={forks}/{N_TRIALS}, total_40001={total_40001}"
    )
    return forks, all_trusted, total_40001


def main():
    print(f"RC Fork Diagnostic v3 — RUN_ID={RUN_ID}")
    print(f"N_WRITERS={N_WRITERS}, N_TRIALS={N_TRIALS} per isolation")
    print("Threads pre-insert pending beliefs, then race simultaneously")

    ser_forks, ser_t, ser_40001 = run_isolation("serializable")
    rc_forks, rc_t, rc_40001 = run_isolation("read_committed")

    print(f"\n{'='*60}")
    print("VERDICT")
    print(f"  SERIALIZABLE: forks={ser_forks}/{N_TRIALS}, trusted={ser_t}, 40001={ser_40001}")
    print(f"  READ COMMITTED: forks={rc_forks}/{N_TRIALS}, trusted={rc_t}, 40001={rc_40001}")
    if rc_forks > 0:
        print("  RC DOES fork. SERIALIZABLE is required for correctness.")
    else:
        print("  RC did NOT fork. CockroachDB RC write-locking prevents this specific anomaly.")
        print("  SERIALIZABLE still required for: consistent snapshot reads (S1 cluster-mean),")
        print("  explicit retry semantics (40001), and non-repeatable read prevention.")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
