"""
MCP read transport for A9/A10.

The CockroachDB Cloud Managed MCP Server (cockroachlabs.cloud/mcp) is the
second enforcement layer on TB4. It provides a read-only protocol surface —
write verbs are not available regardless of application code.

This module wraps HTTP calls to the MCP endpoint to execute recall queries.
It is used instead of a direct DB connection on the consumer read path.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

MCP_ENDPOINT = os.getenv("COCKROACH_MCP_URL", "https://cockroachlabs.cloud/mcp")
MCP_CLUSTER_ID = os.getenv(
    "COCKROACH_MCP_CLUSTER_ID", "71b13406-ccdb-481e-b0dc-f4aa75718234"
)

# SQL keywords that indicate write operations — blocked before sending to MCP.
_WRITE_VERBS = frozenset(
    ["INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "GRANT", "REVOKE", "TRUNCATE", "ALTER"]
)


class MCPProtocolError(Exception):
    """Raised when MCP rejects a request at the protocol layer (e.g., write verb blocked)."""


class MCPConnectionError(Exception):
    """Raised when the MCP endpoint is unreachable."""


class MCPAuthError(Exception):
    """Raised when MCP authentication fails."""


def _contains_write_verb(sql: str) -> bool:
    """Return True if the SQL statement contains a write-tier keyword.

    Checks are case-insensitive and word-boundary sensitive to avoid false
    positives on column names like 'created_at'. This is a pre-flight
    application-level guard — the MCP server is the definitive enforcement.
    """
    normalized = sql.upper()
    for verb in _WRITE_VERBS:
        # Simple token scan: check if verb appears as a word token
        idx = normalized.find(verb)
        while idx != -1:
            before = normalized[idx - 1] if idx > 0 else " "
            after = normalized[idx + len(verb)] if idx + len(verb) < len(normalized) else " "
            if not before.isalpha() and not after.isalpha():
                return True
            idx = normalized.find(verb, idx + 1)
    return False


class MCPReadClient:
    """
    Thin client for read-only queries via the CockroachDB Cloud MCP Server.
    Provides structural read-only enforcement at the protocol layer (TB4 second layer).
    No write verb is available through this client regardless of credentials.
    """

    def __init__(
        self,
        endpoint: str = MCP_ENDPOINT,
        cluster_id: str = MCP_CLUSTER_ID,
        oauth_token: str | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.cluster_id = cluster_id
        self.token = oauth_token or os.getenv("COCKROACH_MCP_TOKEN", "")

    def _call_mcp_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Make an HTTP POST to the MCP tools endpoint using JSON-RPC 2.0.

        Raises:
            MCPProtocolError: If a write statement is attempted or MCP rejects the verb.
            MCPConnectionError: If the endpoint is unreachable.
            MCPAuthError: If authentication fails (401/403).
        """
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        }
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        url = f"{self.endpoint}/tools/call"
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                data = json.loads(raw)
                # JSON-RPC 2.0 success: result field present
                if "result" in data:
                    content = data["result"]
                    if isinstance(content, list):
                        return content
                    return [content]
                # JSON-RPC error
                err = data.get("error", {})
                raise MCPProtocolError(
                    f"MCP tool call error: {err.get('message', str(err))}"
                )
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise MCPAuthError(
                    f"MCP authentication failed (HTTP {exc.code}): "
                    "check COCKROACH_MCP_TOKEN"
                ) from exc
            if exc.code == 405:
                raise MCPProtocolError(
                    "MCP endpoint rejected the request (405 Method Not Allowed). "
                    "Confirm the tool name and endpoint URL."
                ) from exc
            raise MCPConnectionError(
                f"MCP endpoint returned HTTP {exc.code}: {exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            raise MCPConnectionError(
                f"MCP endpoint unreachable: {exc.reason}"
            ) from exc

    def execute_read(
        self,
        sql: str,
        params: list[Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a read-only SQL statement through the MCP endpoint.

        Pre-flight check: raises MCPProtocolError if sql contains write keywords
        (INSERT, UPDATE, DELETE, CREATE, DROP, GRANT, REVOKE, TRUNCATE, ALTER).
        This is a deterministic application-layer guard; the MCP server provides
        the authoritative enforcement.

        Args:
            sql: A SELECT or other read-only SQL statement.
            params: Optional positional parameters (passed to MCP as arguments).

        Returns:
            List of row dicts.

        Raises:
            MCPProtocolError: Write verb detected in sql, or MCP rejects request.
            MCPConnectionError: Endpoint unreachable.
            MCPAuthError: Authentication failure.
        """
        if _contains_write_verb(sql):
            raise MCPProtocolError(
                f"Write verb detected in SQL — MCPReadClient enforces read-only access. "
                f"Offending SQL (first 120 chars): {sql[:120]!r}"
            )

        arguments: dict[str, Any] = {"sql": sql}
        if params:
            arguments["params"] = params

        logger.debug(
            "mcp_execute_read",
            cluster_id=self.cluster_id,
            sql_preview=sql[:80],
        )

        return self._call_mcp_tool("execute_sql", arguments)

    def health_check(self) -> bool:
        """Ping the MCP endpoint. Returns True if reachable and authenticated."""
        try:
            self._call_mcp_tool("health", {})
            return True
        except MCPAuthError:
            # Endpoint is reachable but auth failed — still "reachable" for health purposes.
            logger.warning("mcp_health_auth_error")
            return False
        except MCPProtocolError:
            # A protocol-level response (not a connection failure) — endpoint is reachable.
            return True
        except MCPConnectionError as exc:
            logger.warning("mcp_health_unreachable", error=str(exc))
            return False
        except Exception as exc:
            logger.warning("mcp_health_unexpected_error", error=str(exc))
            return False
