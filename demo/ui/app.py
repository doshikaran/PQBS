"""
PQBS Demo UI — FastAPI JSON API backend + React SPA frontend.

API endpoints (all under /api/):
  GET  /api/health       — system health
  GET  /api/beliefs      — belief table (auditor view)
  GET  /api/quarantine   — quarantined beliefs with signal breakdown
  GET  /api/temporal     — temporal reconstruction
  POST /api/recall       — semantic similarity search
  GET  /api/metrics      — live metrics snapshot

Frontend:
  Served from demo/ui/frontend/dist/ (built with `npm run build`).
  Dev: run `npm run dev` in demo/ui/frontend/ (proxies /api → port 8080).

Run backend: PYTHONPATH=src uvicorn demo.ui.app:app --reload --port 8080
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

logger = structlog.get_logger(__name__)

app = FastAPI(
    title="PQBS Demo API",
    description="Poison-Quarantine Belief Store — agentic memory with integrity screening",
    version="0.1.0",
)

# CORS — allow local Vite dev server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _get_db() -> Any:
    from pqbs.substrate.connection import get_connection
    return get_connection()


# ---------------------------------------------------------------------------
# GET /api/health
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health() -> JSONResponse:
    status: dict[str, Any] = {
        "status": "ok",
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "db": "unknown",
        "mcp": "unknown",
    }

    try:
        conn = _get_db()
        row = conn.execute("SELECT 1 AS ping").fetchone()
        conn.close()
        status["db"] = "ok" if row and row.get("ping") == 1 else "error"
    except Exception as exc:
        status["db"] = f"error: {exc}"
        status["status"] = "degraded"

    try:
        from pqbs.recall.mcp_client import MCPReadClient
        mcp = MCPReadClient()
        status["mcp"] = "ok" if mcp.health_check() else "unreachable"
    except Exception as exc:
        status["mcp"] = f"error: {exc}"

    http_status = 200 if status["status"] == "ok" else 503
    return JSONResponse(status, status_code=http_status)


# ---------------------------------------------------------------------------
# GET /api/beliefs
# ---------------------------------------------------------------------------

@app.get("/api/beliefs")
def beliefs(
    tenant_id: str = Query(default="", description="Tenant UUID filter (optional)"),
    limit: int = Query(default=50, ge=1, le=500),
) -> JSONResponse:
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
        logger.error("beliefs_api_error", error=str(exc))
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse({
        "count": len(rows),
        "beliefs": [
            {
                "belief_id": str(r["belief_id"]),
                "tenant_id": str(r["tenant_id"]),
                "subject": r["subject"],
                "predicate": r["predicate"],
                "object": r["object"],
                "status": r["status"],
                "trust_score": float(r["trust_score"]) if r.get("trust_score") is not None else None,
                "confidence": float(r["confidence"]),
                "valid_from": r["valid_from"].isoformat() if r.get("valid_from") else None,
                "valid_to": r["valid_to"].isoformat() if r.get("valid_to") else None,
                "tx_from": r["tx_from"].isoformat() if r.get("tx_from") else None,
            }
            for r in rows
        ],
    })


# ---------------------------------------------------------------------------
# GET /api/quarantine
# ---------------------------------------------------------------------------

@app.get("/api/quarantine")
def quarantine(
    tenant_id: str = Query(default="", description="Tenant UUID filter"),
    limit: int = Query(default=50, ge=1, le=200),
) -> JSONResponse:
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
        logger.error("quarantine_api_error", error=str(exc))
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse({
        "count": len(rows),
        "quarantine": [
            {
                "quarantine_id": str(r["quarantine_id"]),
                "belief_id": str(r["belief_id"]),
                "tenant_id": str(r["tenant_id"]),
                "reason_code": r.get("reason_code"),
                "quarantined_at": r["quarantined_at"].isoformat() if r.get("quarantined_at") else None,
                "disposition": r.get("disposition"),
                "reviewed_by": r.get("reviewed_by"),
                "subject": r["subject"],
                "predicate": r["predicate"],
                "object": r["object"],
                "verdict": r.get("verdict"),
                "trust_score": float(r["trust_score"]) if r.get("trust_score") is not None else None,
                "signal_scores": r.get("signal_scores"),
            }
            for r in rows
        ],
    })


# ---------------------------------------------------------------------------
# GET /api/temporal
# ---------------------------------------------------------------------------

@app.get("/api/temporal")
def temporal(
    tenant_id: str = Query(default=""),
    as_of: str = Query(default=""),
    mechanism: str = Query(default="bitemporal"),
    subject_filter: str = Query(default=""),
) -> JSONResponse:
    if not tenant_id or not as_of:
        return JSONResponse(
            {"error": "tenant_id and as_of are required"},
            status_code=400,
        )

    try:
        from pqbs.audit.engine import AuditEngine
        from pqbs.contracts import TemporalMechanism, TemporalQuery

        as_of_dt = datetime.fromisoformat(as_of)
        if as_of_dt.tzinfo is None:
            as_of_dt = as_of_dt.replace(tzinfo=timezone.utc)

        mech = (
            TemporalMechanism(mechanism)
            if mechanism in ("bitemporal", "mvcc")
            else TemporalMechanism.BITEMPORAL
        )

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
            conn.close()
            return JSONResponse({
                "mechanism_used": "bitemporal",
                "as_of": as_of_dt.isoformat(),
                "count": len(rows),
                "beliefs": [
                    {
                        "subject": r.get("subject"),
                        "predicate": r.get("predicate"),
                        "object": r.get("object"),
                        "status": r.get("status"),
                        "tx_from": str(r.get("tx_from", ""))[:19],
                    }
                    for r in rows
                ],
            })
        else:
            data = engine.query_mvcc(q, conn)
            conn.close()
            if "error" in data:
                return JSONResponse({
                    "mechanism_used": "mvcc",
                    "error": data["error"],
                    "suggestion": data.get("suggestion", ""),
                    "as_of": as_of_dt.isoformat(),
                }, status_code=422)
            beliefs = data.get("beliefs", [])
            return JSONResponse({
                "mechanism_used": "mvcc",
                "as_of": as_of_dt.isoformat(),
                "count": len(beliefs),
                "beliefs": [
                    {
                        "subject": r.get("subject"),
                        "predicate": r.get("predicate"),
                        "object": r.get("object"),
                        "status": r.get("status"),
                    }
                    for r in beliefs
                ],
            })

    except Exception as exc:
        logger.error("temporal_api_error", error=str(exc))
        return JSONResponse({"error": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# POST /api/recall
# ---------------------------------------------------------------------------

@app.post("/api/recall")
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
# GET /api/metrics
# ---------------------------------------------------------------------------

@app.get("/api/metrics")
def metrics(
    tenant_id: str = Query(default="", description="Tenant UUID for DB-loaded metrics"),
) -> JSONResponse:
    from pqbs.telemetry.metrics import MetricsCollector

    collector = MetricsCollector()

    if tenant_id:
        try:
            conn = _get_db()
            collector.load_from_db(conn, uuid.UUID(tenant_id))
            conn.close()
        except Exception as exc:
            logger.warning("metrics_db_load_failed", error=str(exc))

    return JSONResponse(collector.snapshot())


# ---------------------------------------------------------------------------
# Serve the built React SPA (conditional on dist/ existing)
# ---------------------------------------------------------------------------

_FRONTEND_DIST = Path(__file__).parent / "frontend" / "dist"

if _FRONTEND_DIST.exists():
    # Mount assets directory
    _ASSETS = _FRONTEND_DIST / "assets"
    if _ASSETS.exists():
        app.mount("/assets", StaticFiles(directory=str(_ASSETS)), name="assets")

    # Catch-all: serve index.html for any non-API path (SPA routing)
    from fastapi.responses import FileResponse

    @app.get("/")
    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str = "") -> FileResponse:
        index = _FRONTEND_DIST / "index.html"
        return FileResponse(str(index))
