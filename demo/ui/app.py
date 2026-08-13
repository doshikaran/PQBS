"""
PQBS Demo UI — minimal FastAPI application showing key system properties.

Screens:
1. GET /          — index with navigation links
2. GET /beliefs   — belief table with visible status (role_auditor view)
3. GET /quarantine — quarantined beliefs with reason_code, signal breakdown
4. GET /temporal  — temporal query control (both mechanisms)
5. GET /recall    — recall search interface
6. GET /health    — system health including MCP connectivity

Run: PYTHONPATH=src uvicorn demo.ui.app:app --reload --port 8080
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

logger = structlog.get_logger(__name__)

app = FastAPI(
    title="PQBS Demo UI",
    description="Poison-Quarantine Belief Store — agentic memory with integrity screening",
    version="0.1.0",
)

# ---------------------------------------------------------------------------
# Minimal inline CSS (no external frameworks)
# ---------------------------------------------------------------------------

_CSS = """
body { font-family: system-ui, sans-serif; margin: 40px; color: #222; }
h1 { color: #c8102e; }
h2 { color: #333; border-bottom: 1px solid #ddd; padding-bottom: 8px; }
nav a { margin-right: 16px; color: #0066cc; text-decoration: none; font-weight: 500; }
nav a:hover { text-decoration: underline; }
table { border-collapse: collapse; width: 100%; margin-top: 16px; font-size: 13px; }
th { background: #f4f4f4; border: 1px solid #ddd; padding: 8px 12px; text-align: left; }
td { border: 1px solid #ddd; padding: 6px 12px; vertical-align: top; }
tr:nth-child(even) { background: #fafafa; }
.status-trusted   { color: #0a7; font-weight: 600; }
.status-quarantined { color: #c00; font-weight: 600; }
.status-pending   { color: #a60; font-weight: 600; }
.status-superseded { color: #888; }
.error { color: #c00; background: #fff0f0; padding: 12px; border-radius: 4px; }
.info  { color: #005; background: #e8f0ff; padding: 12px; border-radius: 4px; }
.form-row { margin: 12px 0; }
label { display: inline-block; width: 140px; font-weight: 500; }
input[type=text], input[type=number], select {
    border: 1px solid #ccc; padding: 6px 10px; border-radius: 4px; width: 300px; }
button { background: #c8102e; color: #fff; border: none; padding: 8px 20px;
         border-radius: 4px; cursor: pointer; font-size: 14px; }
button:hover { background: #a00; }
pre { background: #f6f6f6; border: 1px solid #ddd; padding: 12px; overflow-x: auto;
      border-radius: 4px; font-size: 12px; }
.badge-ok  { background: #0a7; color: #fff; padding: 2px 8px; border-radius: 10px; font-size: 12px; }
.badge-err { background: #c00; color: #fff; padding: 2px 8px; border-radius: 10px; font-size: 12px; }
"""

_NAV = """
<nav>
  <a href="/">Home</a>
  <a href="/beliefs">Beliefs</a>
  <a href="/quarantine">Quarantine</a>
  <a href="/temporal">Temporal Query</a>
  <a href="/recall">Recall Search</a>
  <a href="/health">Health</a>
</nav>
<hr>
"""


def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{title} — PQBS Demo</title>
  <style>{_CSS}</style>
</head>
<body>
  <h1>PQBS Demo</h1>
  {_NAV}
  {body}
</body>
</html>"""


def _get_db() -> Any:
    from pqbs.substrate.connection import get_connection
    return get_connection()


def _status_class(status: str) -> str:
    return {
        "trusted": "status-trusted",
        "quarantined": "status-quarantined",
        "pending": "status-pending",
        "superseded": "status-superseded",
    }.get(status, "")


# ---------------------------------------------------------------------------
# GET /
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    body = """
<h2>PQBS — Poison-Quarantine Belief Store</h2>
<p>An agentic memory system with integrity screening. Every belief is screened
before becoming visible to consumers. Adversarial or anomalous beliefs are
quarantined and cannot reach the recall path.</p>

<h3>Key properties demonstrated</h3>
<ul>
  <li><strong>Security Invariant #1:</strong> No belief enters as <code>trusted</code>. Every write is <code>pending</code>.</li>
  <li><strong>Security Invariant #2:</strong> <code>quarantined</code> and <code>pending</code> beliefs are structurally inaccessible to consumers via role-scoped view.</li>
  <li><strong>Temporal reconstruction:</strong> Two mechanisms — bitemporal (tx_from/tx_to columns, unbounded) and MVCC (AS OF SYSTEM TIME, bounded by GC window).</li>
  <li><strong>Retrieval audit:</strong> Every recall is logged with the returned belief IDs and query digest.</li>
  <li><strong>MCP second layer (TB4):</strong> CockroachDB Cloud MCP server enforces read-only protocol at transport level.</li>
</ul>

<h3>Navigation</h3>
<ul>
  <li><a href="/beliefs">Beliefs</a> — all beliefs with status, subject, predicate, object, trust score</li>
  <li><a href="/quarantine">Quarantine</a> — quarantined beliefs with reason codes and signal breakdown</li>
  <li><a href="/temporal">Temporal Query</a> — reconstruct belief state at any point in time</li>
  <li><a href="/recall">Recall Search</a> — semantic similarity search over trusted beliefs</li>
  <li><a href="/health">Health</a> — system health including DB and MCP connectivity</li>
</ul>
"""
    return _page("Home", body)


# ---------------------------------------------------------------------------
# GET /beliefs
# ---------------------------------------------------------------------------


@app.get("/beliefs", response_class=HTMLResponse)
def beliefs(
    tenant_id: str = Query(default="", description="Tenant UUID filter (optional)"),
    limit: int = Query(default=50, ge=1, le=500),
) -> str:
    try:
        conn = _get_db()
        params: list[Any] = []
        sql = """
SELECT b.belief_id, b.tenant_id, b.subject, b.predicate, b.object,
       b.status, b.trust_score, b.confidence, b.valid_from, b.valid_to, b.tx_from
FROM belief b
"""
        where = []
        if tenant_id:
            where.append("b.tenant_id = %s")
            params.append(tenant_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY b.tx_from DESC LIMIT %s"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        conn.close()
    except Exception as exc:
        logger.error("beliefs_page_error", error=str(exc))
        body = f'<div class="error">Database error: {exc}</div>'
        return _page("Beliefs", body)

    filter_form = f"""
<form method="get">
  <div class="form-row">
    <label>Tenant ID:</label>
    <input type="text" name="tenant_id" value="{tenant_id}" placeholder="UUID or leave blank for all">
  </div>
  <div class="form-row">
    <label>Limit:</label>
    <input type="number" name="limit" value="{limit}" min="1" max="500">
  </div>
  <button type="submit">Filter</button>
</form>
"""

    if not rows:
        body = filter_form + '<div class="info">No beliefs found.</div>'
        return _page("Beliefs", body)

    rows_html = ""
    for r in rows:
        status = r.get("status", "")
        cls = _status_class(status)
        trust = f"{r['trust_score']:.3f}" if r.get("trust_score") is not None else "—"
        rows_html += f"""<tr>
  <td style="font-size:11px;color:#888">{str(r['belief_id'])[:8]}…</td>
  <td>{r['subject']}</td>
  <td><code>{r['predicate']}</code></td>
  <td>{r['object']}</td>
  <td class="{cls}">{status}</td>
  <td>{trust}</td>
  <td>{r['confidence']:.2f}</td>
  <td>{r['valid_from'].strftime('%Y-%m-%d') if r.get('valid_from') else '—'}</td>
  <td>{r['valid_to'].strftime('%Y-%m-%d') if r.get('valid_to') else '∞'}</td>
</tr>"""

    table = f"""
<table>
  <thead>
    <tr>
      <th>ID</th><th>Subject</th><th>Predicate</th><th>Object</th>
      <th>Status</th><th>Trust</th><th>Conf.</th>
      <th>Valid From</th><th>Valid To</th>
    </tr>
  </thead>
  <tbody>{rows_html}</tbody>
</table>
<p style="color:#888;font-size:12px">{len(rows)} row(s) returned</p>
"""
    body = f"<h2>Beliefs ({len(rows)})</h2>{filter_form}{table}"
    return _page("Beliefs", body)


# ---------------------------------------------------------------------------
# GET /quarantine
# ---------------------------------------------------------------------------


@app.get("/quarantine", response_class=HTMLResponse)
def quarantine(
    tenant_id: str = Query(default="", description="Tenant UUID filter"),
    limit: int = Query(default=50, ge=1, le=200),
) -> str:
    try:
        conn = _get_db()
        params: list[Any] = []
        sql = """
SELECT q.quarantine_id, q.belief_id, q.tenant_id, q.reason_code,
       q.quarantined_at, q.disposition, q.reviewed_by,
       b.subject, b.predicate, b.object,
       iv.verdict, iv.trust_score, iv.signal_scores
FROM quarantine q
JOIN belief b ON b.belief_id = q.belief_id AND b.tenant_id = q.tenant_id
LEFT JOIN integrity_verdict iv ON iv.belief_id = q.belief_id AND iv.tenant_id = q.tenant_id
"""
        where = []
        if tenant_id:
            where.append("q.tenant_id = %s")
            params.append(tenant_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY q.quarantined_at DESC LIMIT %s"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        conn.close()
    except Exception as exc:
        logger.error("quarantine_page_error", error=str(exc))
        body = f'<div class="error">Database error: {exc}</div>'
        return _page("Quarantine", body)

    filter_form = f"""
<form method="get">
  <div class="form-row">
    <label>Tenant ID:</label>
    <input type="text" name="tenant_id" value="{tenant_id}" placeholder="UUID or leave blank for all">
  </div>
  <div class="form-row">
    <label>Limit:</label>
    <input type="number" name="limit" value="{limit}" min="1" max="200">
  </div>
  <button type="submit">Filter</button>
</form>
"""

    if not rows:
        body = filter_form + '<div class="info">No quarantined beliefs found.</div>'
        return _page("Quarantine", body)

    rows_html = ""
    for r in rows:
        disp = r.get("disposition", "held")
        trust = f"{r['trust_score']:.3f}" if r.get("trust_score") is not None else "—"
        rows_html += f"""<tr>
  <td>{r['subject']}</td>
  <td><code>{r['predicate']}</code></td>
  <td>{r['object']}</td>
  <td><code>{r.get('reason_code', '—')}</code></td>
  <td>{trust}</td>
  <td>{r.get('verdict', '—')}</td>
  <td>{disp}</td>
  <td style="font-size:11px">{str(r.get('quarantined_at', ''))[:19]}</td>
</tr>"""

    table = f"""
<table>
  <thead>
    <tr>
      <th>Subject</th><th>Predicate</th><th>Object</th>
      <th>Reason</th><th>Trust</th><th>Verdict</th>
      <th>Disposition</th><th>Quarantined At</th>
    </tr>
  </thead>
  <tbody>{rows_html}</tbody>
</table>
<p style="color:#888;font-size:12px">{len(rows)} quarantined belief(s)</p>
"""
    body = f"<h2>Quarantine ({len(rows)})</h2>{filter_form}{table}"
    return _page("Quarantine", body)


# ---------------------------------------------------------------------------
# GET /temporal
# ---------------------------------------------------------------------------


@app.get("/temporal", response_class=HTMLResponse)
def temporal(
    tenant_id: str = Query(default=""),
    as_of: str = Query(default=""),
    mechanism: str = Query(default="bitemporal"),
    subject_filter: str = Query(default=""),
) -> str:
    result_html = ""

    if tenant_id and as_of:
        try:
            from pqbs.audit.engine import AuditEngine
            from pqbs.contracts import TemporalMechanism, TemporalQuery

            as_of_dt = datetime.fromisoformat(as_of)
            if as_of_dt.tzinfo is None:
                as_of_dt = as_of_dt.replace(tzinfo=timezone.utc)

            mech = TemporalMechanism(mechanism) if mechanism in ("bitemporal", "mvcc") else TemporalMechanism.BITEMPORAL

            q = TemporalQuery(
                tenant_id=uuid.UUID(tenant_id),
                as_of=as_of_dt,
                mechanism=mech,
                requesting_agent_id="demo-ui",
                subject_filter=subject_filter or None,
            )

            conn = _get_db()
            engine = AuditEngine()

            if mech == TemporalMechanism.BITEMPORAL:
                rows = engine.query_bitemporal(q, conn)
                used = "bitemporal"
                if rows:
                    rows_html = "".join(
                        f"<tr><td>{r.get('subject')}</td><td><code>{r.get('predicate')}</code></td>"
                        f"<td>{r.get('object')}</td><td class='{_status_class(str(r.get('status','')))}'>"
                        f"{r.get('status')}</td><td>{str(r.get('tx_from',''))[:19]}</td></tr>"
                        for r in rows
                    )
                    result_html = f"""
<h3>Result — {used} — {len(rows)} belief(s) at {as_of_dt.isoformat()[:19]}Z</h3>
<table>
  <thead><tr><th>Subject</th><th>Predicate</th><th>Object</th><th>Status</th><th>tx_from</th></tr></thead>
  <tbody>{rows_html}</tbody>
</table>"""
                else:
                    result_html = f'<div class="info">No beliefs found at {as_of_dt.isoformat()[:19]}Z (bitemporal).</div>'
            else:
                data = engine.query_mvcc(q, conn)
                if "error" in data:
                    result_html = f'<div class="error"><strong>MVCC Error:</strong> {data["error"]}<br><small>{data.get("suggestion","")}</small></div>'
                else:
                    beliefs = data.get("beliefs", [])
                    if beliefs:
                        rows_html = "".join(
                            f"<tr><td>{r.get('subject')}</td><td><code>{r.get('predicate')}</code></td>"
                            f"<td>{r.get('object')}</td><td>{r.get('status')}</td></tr>"
                            for r in beliefs
                        )
                        result_html = f"""
<h3>Result — mvcc — {len(beliefs)} belief(s) at {as_of_dt.isoformat()[:19]}Z</h3>
<table>
  <thead><tr><th>Subject</th><th>Predicate</th><th>Object</th><th>Status</th></tr></thead>
  <tbody>{rows_html}</tbody>
</table>"""
                    else:
                        result_html = f'<div class="info">No beliefs found (MVCC as of {as_of_dt.isoformat()[:19]}Z).</div>'

            conn.close()

        except Exception as exc:
            result_html = f'<div class="error">Error: {exc}</div>'

    now_iso = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    form = f"""
<form method="get">
  <div class="form-row">
    <label>Tenant ID:</label>
    <input type="text" name="tenant_id" value="{tenant_id}" placeholder="UUID">
  </div>
  <div class="form-row">
    <label>As Of (ISO):</label>
    <input type="text" name="as_of" value="{as_of or now_iso}" placeholder="2026-08-01T12:00:00">
  </div>
  <div class="form-row">
    <label>Mechanism:</label>
    <select name="mechanism">
      <option value="bitemporal" {'selected' if mechanism == 'bitemporal' else ''}>Bitemporal (unbounded)</option>
      <option value="mvcc" {'selected' if mechanism == 'mvcc' else ''}>MVCC (AS OF SYSTEM TIME, ~1h window)</option>
    </select>
  </div>
  <div class="form-row">
    <label>Subject Filter:</label>
    <input type="text" name="subject_filter" value="{subject_filter}" placeholder="Optional subject string">
  </div>
  <button type="submit">Query</button>
</form>
<p style="color:#888;font-size:12px">
  <strong>Bitemporal:</strong> uses tx_from/tx_to columns — works for any historical timestamp, no retention limit.<br>
  <strong>MVCC:</strong> uses CockroachDB AS OF SYSTEM TIME — limited to ~1h GC retention on Serverless.
</p>
"""
    body = f"<h2>Temporal Query (A10)</h2>{form}{result_html}"
    return _page("Temporal Query", body)


# ---------------------------------------------------------------------------
# POST /recall  (JSON API)
# ---------------------------------------------------------------------------


@app.post("/recall")
async def recall_api(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    query = body.get("query", "")
    tenant_id_str = body.get("tenant_id", "")
    limit = int(body.get("limit", 10))
    region = body.get("region") or os.environ.get("AWS_REGION")

    if not query or not tenant_id_str:
        return JSONResponse({"error": "query and tenant_id are required"}, status_code=400)

    try:
        from pqbs.agents.consumer.a9_recall import RecallAgent
        from pqbs.contracts import RecallRequest

        agent = RecallAgent()
        req = RecallRequest(
            query=query,
            tenant_id=uuid.UUID(tenant_id_str),
            limit=limit,
        )
        conn = _get_db()
        result = agent.recall(req, conn, region=region)
        conn.close()

        return JSONResponse({
            "retrieval_id": str(result.retrieval_id),
            "request_id": str(result.request_id),
            "count": len(result.beliefs),
            "query_latency_ms": result.query_latency_ms,
            "retrieved_at": result.retrieved_at.isoformat(),
            "beliefs": [
                {
                    "belief_id": str(b.belief_id),
                    "subject": b.subject,
                    "predicate": b.predicate,
                    "object": b.object,
                    "object_normalized": b.object_normalized,
                    "confidence": b.confidence,
                    "trust_score": b.trust_score,
                    "valid_from": b.valid_from.isoformat(),
                    "valid_to": b.valid_to.isoformat() if b.valid_to else None,
                    "author_agent_id": b.author_agent_id,
                }
                for b in result.beliefs
            ],
        })
    except Exception as exc:
        logger.error("recall_api_error", error=str(exc))
        return JSONResponse({"error": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# GET /recall (UI)
# ---------------------------------------------------------------------------


@app.get("/recall", response_class=HTMLResponse)
def recall_page(
    tenant_id: str = Query(default=""),
    query: str = Query(default=""),
    limit: int = Query(default=10, ge=1, le=100),
) -> str:
    result_html = ""

    if tenant_id and query:
        try:
            from pqbs.agents.consumer.a9_recall import RecallAgent
            from pqbs.contracts import RecallRequest

            region = os.environ.get("AWS_REGION")
            agent = RecallAgent()
            req = RecallRequest(
                query=query,
                tenant_id=uuid.UUID(tenant_id),
                limit=limit,
            )
            conn = _get_db()
            result = agent.recall(req, conn, region=region)
            conn.close()

            if result.beliefs:
                rows_html = "".join(
                    f"<tr><td>{b.subject}</td><td><code>{b.predicate}</code></td>"
                    f"<td>{b.object}</td><td>{b.trust_score:.3f}</td></tr>"
                    for b in result.beliefs
                )
                result_html = f"""
<h3>Results — {len(result.beliefs)} belief(s) ({result.query_latency_ms}ms)</h3>
<p style="color:#888;font-size:12px">Retrieval ID: {result.retrieval_id}</p>
<table>
  <thead><tr><th>Subject</th><th>Predicate</th><th>Object</th><th>Trust</th></tr></thead>
  <tbody>{rows_html}</tbody>
</table>"""
            else:
                result_html = '<div class="info">No trusted beliefs matched the query.</div>'
        except Exception as exc:
            logger.error("recall_ui_error", error=str(exc))
            result_html = f'<div class="error">Error: {exc}</div>'

    form = f"""
<form method="get">
  <div class="form-row">
    <label>Tenant ID:</label>
    <input type="text" name="tenant_id" value="{tenant_id}" placeholder="UUID">
  </div>
  <div class="form-row">
    <label>Query:</label>
    <input type="text" name="query" value="{query}" placeholder="What does Alice do?">
  </div>
  <div class="form-row">
    <label>Limit:</label>
    <input type="number" name="limit" value="{limit}" min="1" max="100">
  </div>
  <button type="submit">Search</button>
</form>
<p style="color:#888;font-size:12px">
  Searches over <code>v_trusted_current</code> (status='trusted' AND tx_to IS NULL) using
  vector similarity (<code>&lt;-&gt;</code> operator). Requires <code>AWS_REGION</code>
  and Bedrock credentials for embedding.
</p>
"""
    body = f"<h2>Recall Search (A9)</h2>{form}{result_html}"
    return _page("Recall Search", body)


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> JSONResponse:
    status: dict[str, Any] = {
        "status": "ok",
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "db": "unknown",
        "mcp": "unknown",
    }

    # DB health
    try:
        conn = _get_db()
        row = conn.execute("SELECT 1 AS ping").fetchone()
        conn.close()
        status["db"] = "ok" if row and row.get("ping") == 1 else "error"
    except Exception as exc:
        status["db"] = f"error: {exc}"
        status["status"] = "degraded"

    # MCP health
    try:
        from pqbs.recall.mcp_client import MCPReadClient
        mcp = MCPReadClient()
        status["mcp"] = "ok" if mcp.health_check() else "unreachable"
    except Exception as exc:
        status["mcp"] = f"error: {exc}"

    http_status = 200 if status["status"] == "ok" else 503
    return JSONResponse(status, status_code=http_status)
