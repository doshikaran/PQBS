"""Unit tests for MCPReadClient.

No network calls — all HTTP is mocked or tested via pre-flight guards.
"""
from __future__ import annotations

import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from pqbs.recall.mcp_client import (
    MCPAuthError,
    MCPConnectionError,
    MCPProtocolError,
    MCPReadClient,
)


def _client() -> MCPReadClient:
    return MCPReadClient(
        endpoint="https://cockroachlabs.cloud/mcp",
        cluster_id="71b13406-ccdb-481e-b0dc-f4aa75718234",
        oauth_token="test-token",
    )


# ---------------------------------------------------------------------------
# Write verb pre-flight tests (no HTTP required)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_write_sql_raises_mcp_protocol_error() -> None:
    """execute_read with INSERT SQL raises MCPProtocolError before any HTTP call."""
    client = _client()
    with pytest.raises(MCPProtocolError, match="Write verb detected"):
        client.execute_read("INSERT INTO belief (subject) VALUES ('Alice')")


@pytest.mark.unit
def test_delete_sql_raises_mcp_protocol_error() -> None:
    """execute_read with DELETE SQL raises MCPProtocolError before any HTTP call."""
    client = _client()
    with pytest.raises(MCPProtocolError, match="Write verb detected"):
        client.execute_read("DELETE FROM belief WHERE belief_id = '123'")


@pytest.mark.unit
def test_update_sql_raises_mcp_protocol_error() -> None:
    """execute_read with UPDATE SQL raises MCPProtocolError before any HTTP call."""
    client = _client()
    with pytest.raises(MCPProtocolError, match="Write verb detected"):
        client.execute_read("UPDATE belief SET status = 'trusted' WHERE belief_id = '1'")


@pytest.mark.unit
def test_create_sql_raises_mcp_protocol_error() -> None:
    """execute_read with CREATE SQL raises MCPProtocolError before any HTTP call."""
    client = _client()
    with pytest.raises(MCPProtocolError, match="Write verb detected"):
        client.execute_read("CREATE TABLE foo (id UUID)")


@pytest.mark.unit
def test_drop_sql_raises_mcp_protocol_error() -> None:
    """execute_read with DROP SQL raises MCPProtocolError before any HTTP call."""
    client = _client()
    with pytest.raises(MCPProtocolError, match="Write verb detected"):
        client.execute_read("DROP TABLE belief")


# ---------------------------------------------------------------------------
# SELECT is allowed (protocol guard does not block it)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_select_sql_allowed() -> None:
    """SELECT SQL passes the pre-flight guard (may still fail on HTTP but not on protocol guard)."""
    client = _client()

    # Mock the HTTP call to avoid real network access
    with patch.object(client, "_call_mcp_tool") as mock_call:
        mock_call.return_value = []
        # Should not raise MCPProtocolError
        result = client.execute_read("SELECT * FROM v_trusted_current WHERE tenant_id = '1'")
        assert isinstance(result, list)
        mock_call.assert_called_once()


# ---------------------------------------------------------------------------
# health_check tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_health_check_returns_false_on_connection_error() -> None:
    """health_check returns False when the endpoint is unreachable."""
    client = _client()

    with patch.object(client, "_call_mcp_tool") as mock_call:
        mock_call.side_effect = MCPConnectionError("Connection refused")
        result = client.health_check()

    assert result is False


@pytest.mark.unit
def test_health_check_returns_false_on_unexpected_error() -> None:
    """health_check returns False on any unexpected error."""
    client = _client()

    with patch.object(client, "_call_mcp_tool") as mock_call:
        mock_call.side_effect = RuntimeError("unexpected")
        result = client.health_check()

    assert result is False


@pytest.mark.unit
def test_health_check_returns_false_on_auth_error() -> None:
    """health_check returns False (not exception) on auth error."""
    client = _client()

    with patch.object(client, "_call_mcp_tool") as mock_call:
        mock_call.side_effect = MCPAuthError("401 Unauthorized")
        result = client.health_check()

    # Auth error means endpoint is reachable but creds are wrong — returns False
    assert result is False


@pytest.mark.unit
def test_health_check_returns_true_on_protocol_response() -> None:
    """health_check returns True when the endpoint responds (even with protocol error)."""
    client = _client()

    with patch.object(client, "_call_mcp_tool") as mock_call:
        mock_call.side_effect = MCPProtocolError("unknown tool: health")
        result = client.health_check()

    # Protocol-level response means endpoint is reachable
    assert result is True


# ---------------------------------------------------------------------------
# _call_mcp_tool error mapping tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_call_mcp_tool_maps_401_to_auth_error() -> None:
    """HTTP 401 from MCP endpoint raises MCPAuthError."""
    client = _client()

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://cockroachlabs.cloud/mcp/tools/call",
            code=401,
            msg="Unauthorized",
            hdrs=MagicMock(),  # type: ignore[arg-type]
            fp=None,
        )
        with pytest.raises(MCPAuthError):
            client._call_mcp_tool("execute_sql", {"sql": "SELECT 1"})


@pytest.mark.unit
def test_call_mcp_tool_maps_url_error_to_connection_error() -> None:
    """urllib URLError raises MCPConnectionError."""
    client = _client()

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        with pytest.raises(MCPConnectionError):
            client._call_mcp_tool("execute_sql", {"sql": "SELECT 1"})


@pytest.mark.unit
def test_call_mcp_tool_maps_405_to_protocol_error() -> None:
    """HTTP 405 Method Not Allowed raises MCPProtocolError."""
    client = _client()

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://cockroachlabs.cloud/mcp/tools/call",
            code=405,
            msg="Method Not Allowed",
            hdrs=MagicMock(),  # type: ignore[arg-type]
            fp=None,
        )
        with pytest.raises(MCPProtocolError):
            client._call_mcp_tool("execute_sql", {"sql": "SELECT 1"})
