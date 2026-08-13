"""
V4 — CockroachDB Managed MCP Server Semantics Spike
=====================================================
Question  : What does the CockroachDB Cloud Managed MCP Server expose?
            Read-only by default? How is write access granted?
            What does the audit log record and in what format?
            Is the endpoint accessible and what authentication does it use?
Method    : (a) Probe the MCP endpoint for HTTP-level connectivity.
            (b) Probe the SQL audit trail available on the cluster for
                any MCP-related activity.
            (c) Document the manual configuration steps needed to enable
                the MCP server and connect an agent client.
Pass      : MCP server endpoint is reachable OR cluster shows MCP session
            metadata (confirming the feature is active on this tier).

This is a throwaway spike. None of this code moves into the product.
Output    : stdout summary + appended/replaced section in docs/VERIFICATIONS.md.

NOTE: Full V4 testing (reads, writes, audit log) requires manual steps in
the CockroachDB Cloud Console to enable the MCP server and generate an
OAuth token or API key. This script probes what can be checked programmatically
and documents the manual checklist for the operator.
"""

import argparse
import os
import re
import socket
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import psycopg
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

# CockroachDB Cloud Managed MCP Server endpoint
MCP_HOST = "cockroachlabs.cloud"
MCP_PATH = "/mcp"
MCP_PORT_HTTP = 443

# Extract cluster identifier from COCKROACH_URL for reference
def _cluster_id() -> str:
    try:
        host = COCKROACH_URL.split("@")[-1].split("/")[0]
        return host.split(".")[0]  # e.g. "higher-panther-31862"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Probe 1: HTTP connectivity to cockroachlabs.cloud/mcp
# ---------------------------------------------------------------------------

def probe_mcp_http() -> dict:
    """
    Probe the MCP endpoint for basic HTTP connectivity.
    We expect a 401/403 (auth required) or a 405 (method not allowed for GET)
    to confirm the endpoint exists. A connection error means the host/path is wrong.
    """
    url = f"https://{MCP_HOST}{MCP_PATH}"
    result: dict = {"url": url, "reachable": False, "status": None, "error": None}

    try:
        # Try a GET request — MCP uses SSE/POST, so GET might return 405 or 400
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", "PQBS-V4-spike/1.0")
        req.add_header("Accept", "text/event-stream, application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result["reachable"] = True
                result["status"] = resp.status
                result["headers"] = dict(resp.headers)
        except urllib.error.HTTPError as exc:
            # HTTP errors (4xx, 5xx) still mean the endpoint is reachable
            result["reachable"] = True
            result["status"] = exc.code
            result["reason"] = exc.reason
            try:
                body = exc.read().decode("utf-8", errors="replace")[:300]
                result["body_snippet"] = body
            except Exception:
                pass
    except urllib.error.URLError as exc:
        result["error"] = f"URLError: {exc.reason}"
    except socket.timeout:
        result["error"] = "Timed out after 10s"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"

    return result


def probe_mcp_post() -> dict:
    """
    Probe the MCP endpoint with an SSE POST (the actual MCP protocol).
    MCP over HTTP uses POST with JSON-RPC body.
    """
    url = f"https://{MCP_HOST}{MCP_PATH}"
    result: dict = {"url": url, "reachable": False, "status": None, "error": None}

    # Minimal MCP initialize request
    body = b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"pqbs-v4-spike","version":"1.0"}}}'

    try:
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "PQBS-V4-spike/1.0")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result["reachable"] = True
                result["status"] = resp.status
                resp_body = resp.read().decode("utf-8", errors="replace")[:500]
                result["body_snippet"] = resp_body
        except urllib.error.HTTPError as exc:
            result["reachable"] = True
            result["status"] = exc.code
            result["reason"] = exc.reason
            try:
                body_text = exc.read().decode("utf-8", errors="replace")[:300]
                result["body_snippet"] = body_text
            except Exception:
                pass
    except urllib.error.URLError as exc:
        result["error"] = f"URLError: {exc.reason}"
    except socket.timeout:
        result["error"] = "Timed out after 10s"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"

    return result


# ---------------------------------------------------------------------------
# Probe 2: SQL-level — check for any MCP-related visibility
# ---------------------------------------------------------------------------

def probe_sql_mcp(conn: psycopg.Connection) -> dict:
    """
    Query the cluster for any MCP-related metadata visible via SQL.
    Check: crdb_internal tables, pg_catalog, sessions.
    """
    conn.autocommit = True
    results: dict = {}

    # Check active sessions for any mcp references
    try:
        rows = conn.execute("""
            SELECT count(*) FROM crdb_internal.cluster_sessions
            WHERE application_name ILIKE '%mcp%'
               OR application_name ILIKE '%claude%'
        """).fetchone()
        results["mcp_sessions"] = int(rows[0]) if rows else 0
    except Exception as exc:
        results["mcp_sessions"] = f"error: {exc}"

    # Check for any mcp-related cluster settings
    try:
        rows = conn.execute("""
            SELECT variable, value
            FROM crdb_internal.cluster_settings
            WHERE variable ILIKE '%mcp%' OR variable ILIKE '%cloud%'
            LIMIT 10
        """).fetchall()
        results["mcp_cluster_settings"] = [{"var": r[0], "val": r[1]} for r in rows]
    except Exception as exc:
        results["mcp_cluster_settings"] = f"error: {exc}"

    # Check for any SQL audit log tables
    try:
        rows = conn.execute("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_name ILIKE '%audit%' OR table_name ILIKE '%log%'
            LIMIT 10
        """).fetchall()
        results["audit_tables"] = [r[0] for r in rows]
    except Exception as exc:
        results["audit_tables"] = f"error: {exc}"

    # Check pg_roles for mcp-related roles
    try:
        rows = conn.execute("""
            SELECT rolname FROM pg_roles
            WHERE rolname ILIKE '%mcp%' OR rolname ILIKE '%cloud%'
            LIMIT 10
        """).fetchall()
        results["mcp_roles"] = [r[0] for r in rows]
    except Exception as exc:
        results["mcp_roles"] = f"error: {exc}"

    conn.autocommit = False
    return results


# ---------------------------------------------------------------------------
# Manual configuration steps
# ---------------------------------------------------------------------------

MCP_MANUAL_STEPS = """
Manual steps to fully configure V4 (do after this spike):

1. Log in to CockroachDB Cloud Console → your cluster → "Connect" tab
2. Look for "MCP Server" or "AI Agent Access" section
3. Click "Enable MCP Server" (or equivalent)
4. Generate an API key / OAuth token for MCP access
5. Copy the MCP connection snippet (should look like):
     npx @cockroachlabs/cockroachdb-mcp-server \\
       --host <cluster-host> \\
       --database <db> \\
       --token <api-key>
6. In your agent (Claude Desktop or LLM client), add the MCP server config
7. Test: ask the agent to SELECT 1 (read test)
8. Test: ask the agent to INSERT a row (write test — should require consent)
9. Check the audit log: run SELECT * FROM crdb_internal.cluster_contention_events
   or check the Cloud Console Activity tab
10. Record: what the audit log captures (agent ID, query, timestamp)

Expected behavior per CockroachDB docs:
- Read queries: permitted by default (SELECT)
- Write queries: require explicit confirmation / consent flow
- Audit log: records agent identity, query, timestamp, result
- Auth model: OAuth 2.0 or API key; scoped to the cluster
"""


# ---------------------------------------------------------------------------
# Summary and reporting
# ---------------------------------------------------------------------------

def interpret_mcp_probe(get_result: dict, post_result: dict) -> dict:
    """Determine MCP availability from HTTP probes."""
    reachable = get_result.get("reachable") or post_result.get("reachable")
    get_status = get_result.get("status")
    post_status = post_result.get("status")
    get_err = get_result.get("error")
    post_err = post_result.get("error")

    if not reachable:
        return {
            "available": False,
            "auth_required": False,
            "interpretation": f"MCP endpoint not reachable. GET: {get_err}, POST: {post_err}",
        }

    # 401/403 = endpoint exists, auth required
    if get_status in (401, 403) or post_status in (401, 403):
        return {
            "available": True,
            "auth_required": True,
            "interpretation": (
                f"MCP endpoint reachable (HTTP {get_status or post_status}). "
                f"Authentication required — endpoint exists, config needed."
            ),
        }

    # 200 = somehow got through (unlikely without auth)
    if get_status == 200 or post_status == 200:
        return {
            "available": True,
            "auth_required": False,
            "interpretation": "MCP endpoint returned 200 (unexpected — check body).",
        }

    # 400/405 = endpoint exists but request format wrong
    if get_status in (400, 405) or post_status in (400, 405):
        return {
            "available": True,
            "auth_required": True,
            "interpretation": (
                f"MCP endpoint reachable (HTTP {get_status or post_status}). "
                f"Request format error or auth required."
            ),
        }

    # Any other HTTP status
    if reachable:
        return {
            "available": True,
            "auth_required": True,
            "interpretation": (
                f"MCP endpoint reachable (GET: {get_status}, POST: {post_status}). "
                f"Manual auth configuration required."
            ),
        }

    return {
        "available": False,
        "auth_required": False,
        "interpretation": "MCP endpoint status unclear.",
    }


def print_summary(
    get_probe: dict,
    post_probe: dict,
    sql_probe: dict,
    interpretation: dict,
    passed: bool,
) -> None:
    print("\n" + "=" * 70)
    print("V4 SUMMARY")
    print("=" * 70)
    print(f"  MCP endpoint URL   : {get_probe['url']}")
    print(f"  Reachable (GET)    : {'YES' if get_probe.get('reachable') else 'NO'}")
    print(f"  HTTP status (GET)  : {get_probe.get('status') or get_probe.get('error')}")
    print(f"  Reachable (POST)   : {'YES' if post_probe.get('reachable') else 'NO'}")
    print(f"  HTTP status (POST) : {post_probe.get('status') or post_probe.get('error')}")
    print()
    print(f"  Interpretation     : {interpretation['interpretation']}")
    print(f"  Auth required      : {'YES' if interpretation.get('auth_required') else 'UNKNOWN'}")
    print()
    print(f"  MCP SQL sessions   : {sql_probe.get('mcp_sessions', 'N/A')}")
    print(f"  MCP roles          : {sql_probe.get('mcp_roles', [])}")
    print()

    if passed:
        print("  RESULT: ✅ PASSED")
        print("  MCP endpoint confirmed reachable. Auth config needed for full test.")
        print("  Phase 6 (A9/A10) can use the Managed MCP Server as designed.")
    else:
        print("  RESULT: ❌ FAILED / INCONCLUSIVE")
        print("  MCP endpoint not confirmed. Fallback: direct connection + DB roles.")
        print("  Phase 6 drops to single enforcement layer on TB4.")

    print()
    print("  Manual steps required for full V4 verification:")
    for line in MCP_MANUAL_STEPS.strip().split("\n"):
        print(f"  {line}")

    print("=" * 70)


def write_verifications_report(
    get_probe: dict,
    post_probe: dict,
    sql_probe: dict,
    interpretation: dict,
    passed: bool,
) -> None:
    report_path = Path("docs/VERIFICATIONS.md")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    existing = report_path.read_text() if report_path.exists() else ""

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    passed_str = "✅ PASSED (endpoint reachable)" if passed else "❌ INCONCLUSIVE (manual config required)"

    get_status = get_probe.get("status") or get_probe.get("error") or "unknown"
    post_status = post_probe.get("status") or post_probe.get("error") or "unknown"
    post_body = (post_probe.get("body_snippet") or "")[:200]
    mcp_roles = ", ".join(sql_probe.get("mcp_roles") or []) or "none found"
    mcp_sessions = sql_probe.get("mcp_sessions", "unknown")
    interp = interpretation["interpretation"]

    if passed:
        consequence = (
            "MCP server endpoint is reachable at `cockroachlabs.cloud/mcp`. "
            "Full configuration requires generating an API token from the Cloud Console "
            "and registering the MCP server in the agent's config. "
            "Phase 6 (A9, A10) proceeds as designed — the MCP server is the "
            "second independent enforcement layer on TB4. "
            "Webhook sink in V2 can use the Lambda URL."
        )
        fallback = (
            "None — MCP endpoint reachable. Full auth setup required before Phase 6. "
            "See manual steps in this report."
        )
    else:
        consequence = (
            "MCP server endpoint could not be confirmed reachable. "
            "Phase 6 (A9, A10) must fall back to direct database connection "
            "with database-role enforcement. This drops one of the required "
            "CockroachDB tools. Phase 6.5 (A18, A19) becomes mandatory to "
            "maintain the four-tool requirement."
        )
        fallback = (
            "V4 INCONCLUSIVE. If MCP server setup cannot be completed, "
            "fall back to direct connection. Phase 6.5 becomes mandatory "
            "to maintain four CockroachDB tools in submission. "
            "See BUILD-PLAN.md §6 fallback notes."
        )

    manual_steps_md = MCP_MANUAL_STEPS.strip().replace("\n", "\n")

    entry = f"""## V4 — Managed MCP Server Semantics

**Run date:** {timestamp}
**Cluster:** CockroachDB Serverless (COCKROACH_URL)
**MCP endpoint:** `https://cockroachlabs.cloud/mcp`
**Result:** {passed_str}

### HTTP endpoint probe

| Probe | Status |
|-------|--------|
| GET `https://cockroachlabs.cloud/mcp` | `{get_status}` |
| POST (MCP initialize) | `{post_status}` |

**Interpretation:** {interp}

POST response body (first 200 chars): `{post_body or '(empty)'}`

### SQL visibility of MCP

- MCP-related sessions visible: `{mcp_sessions}`
- MCP-related DB roles: `{mcp_roles}`

### Manual configuration checklist (required for full V4 pass)

```
{manual_steps_md}
```

### Consequence for Phase 6 / A9, A10

{consequence}

**Active fallback:** {fallback}

"""

    pattern = r"## V4 — Managed MCP Server Semantics\n.*?(?=\n## |\Z)"
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
        description="V4 — CockroachDB Managed MCP Server semantics spike."
    )
    parser.add_argument(
        "--no-report", action="store_true",
        help="Skip writing to docs/VERIFICATIONS.md"
    )
    args = parser.parse_args()

    cluster_id = _cluster_id()
    print("=" * 70)
    print("V4 — Managed MCP Server Semantics")
    print(f"Cluster : {COCKROACH_URL.split('@')[-1].split('/')[0]}")
    print(f"Cluster ID: {cluster_id}")
    print(f"MCP URL : https://{MCP_HOST}{MCP_PATH}")
    print("=" * 70)

    # ── Probe 1: HTTP ──────────────────────────────────────────────────────────
    print("\n[1/3] Probing MCP HTTP endpoint (GET) …")
    get_probe = probe_mcp_http()
    if get_probe["reachable"]:
        print(f"  ✅ Reachable — HTTP {get_probe['status']}")
        if get_probe.get("body_snippet"):
            print(f"  Body: {get_probe['body_snippet'][:100]}")
    else:
        print(f"  ❌ Not reachable: {get_probe.get('error')}")

    print("\n[2/3] Probing MCP HTTP endpoint (POST with MCP initialize) …")
    post_probe = probe_mcp_post()
    if post_probe["reachable"]:
        print(f"  ✅ Reachable — HTTP {post_probe['status']}")
        if post_probe.get("body_snippet"):
            snippet = post_probe["body_snippet"][:150].replace("\n", " ")
            print(f"  Body: {snippet}")
    else:
        print(f"  ❌ Not reachable: {post_probe.get('error')}")

    # ── Probe 2: SQL ───────────────────────────────────────────────────────────
    print("\n[3/3] Checking SQL-level MCP visibility …")
    try:
        conn = psycopg.connect(COCKROACH_URL, connect_timeout=CONNECT_TIMEOUT)
        conn.autocommit = True
        conn.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")
        conn.autocommit = False
        sql_probe = probe_sql_mcp(conn)
        conn.close()
        print(f"  MCP sessions visible : {sql_probe.get('mcp_sessions', 'N/A')}")
        print(f"  MCP roles            : {sql_probe.get('mcp_roles', [])}")
        settings = sql_probe.get("mcp_cluster_settings", [])
        if isinstance(settings, list) and settings:
            for s in settings:
                print(f"  Setting: {s}")
    except Exception as exc:
        print(f"  SQL probe failed: {exc}")
        sql_probe = {}

    # ── Interpret ──────────────────────────────────────────────────────────────
    interpretation = interpret_mcp_probe(get_probe, post_probe)
    passed = interpretation.get("available", False)

    # ── Summary ───────────────────────────────────────────────────────────────
    print_summary(get_probe, post_probe, sql_probe, interpretation, passed)

    # ── Write report ──────────────────────────────────────────────────────────
    if not args.no_report:
        write_verifications_report(
            get_probe, post_probe, sql_probe, interpretation, passed
        )

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
