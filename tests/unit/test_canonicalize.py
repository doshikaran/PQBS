"""Unit tests for pqbs.agents.semantics.canonicalize (A11).

Tests cover every normalization rule, ambiguity/ELEVATED escalation,
is_sensitive policy flag, and unknown rule handling.
The DB connection is mocked with MagicMock.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from pqbs.contracts import CandidateBelief, NormalizedBelief, Sensitivity
from pqbs.contracts.enums import SourceType
from pqbs.contracts.provenance import ProvenanceStub
from pqbs.agents.semantics.canonicalize import canonicalize

pytestmark = pytest.mark.unit

TENANT_ID = uuid4()
EPISODE_ID = uuid4()

NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)


def _stub() -> ProvenanceStub:
    return ProvenanceStub(
        source_type=SourceType.USER_STATEMENT,
        source_uri=None,
        source_digest="a" * 64,
        episode_id=EPISODE_ID,
        ingestion_agent_id="test-agent",
    )


def _candidate(
    predicate: str = "test_pred",
    obj: str = "TestValue",
    sensitivity: Sensitivity = Sensitivity.NORMAL,
) -> CandidateBelief:
    return CandidateBelief(
        belief_id=uuid4(),
        tenant_id=TENANT_ID,
        subject="Alice",
        predicate=predicate,
        object=obj,
        confidence=0.9,
        valid_from=NOW,
        valid_to=None,
        provenance_stub=_stub(),
        author_agent_id="test-agent",
        sensitivity=sensitivity,
    )


def _mock_conn(rule: str, is_sensitive: bool) -> MagicMock:
    """Return a mock connection whose execute().fetchone() returns a policy row."""
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = {
        "normalization_rule": rule,
        "is_sensitive": is_sensitive,
    }
    conn.execute.return_value = cursor
    return conn


def _mock_conn_no_policy() -> MagicMock:
    """Return a mock connection with no policy row (fetchone returns None)."""
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    conn.execute.return_value = cursor
    return conn


# ---------------------------------------------------------------------------
# Rule: none
# ---------------------------------------------------------------------------

class TestRuleNone:
    def test_none_returns_unchanged(self) -> None:
        conn = _mock_conn("none", False)
        candidate = _candidate(obj="  Hello World  ")
        result = canonicalize(conn, candidate)
        assert isinstance(result, NormalizedBelief)
        assert result.object_normalized == "  Hello World  "
        assert result.sensitivity == Sensitivity.NORMAL

    def test_no_policy_defaults_to_none(self) -> None:
        conn = _mock_conn_no_policy()
        candidate = _candidate(obj="RawValue")
        result = canonicalize(conn, candidate)
        assert result.object_normalized == "RawValue"
        assert result.sensitivity == Sensitivity.NORMAL


# ---------------------------------------------------------------------------
# Rule: lowercase
# ---------------------------------------------------------------------------

class TestRuleLowercase:
    def test_lowercase_strips_and_lowercases(self) -> None:
        conn = _mock_conn("lowercase", False)
        candidate = _candidate(obj="  Hello WORLD  ")
        result = canonicalize(conn, candidate)
        assert result.object_normalized == "hello world"
        assert result.sensitivity == Sensitivity.NORMAL

    def test_lowercase_already_lower(self) -> None:
        conn = _mock_conn("lowercase", False)
        candidate = _candidate(obj="already lower")
        result = canonicalize(conn, candidate)
        assert result.object_normalized == "already lower"


# ---------------------------------------------------------------------------
# Rule: uppercase
# ---------------------------------------------------------------------------

class TestRuleUppercase:
    def test_uppercase_strips_and_uppercases(self) -> None:
        conn = _mock_conn("uppercase", False)
        candidate = _candidate(obj="  hello world  ")
        result = canonicalize(conn, candidate)
        assert result.object_normalized == "HELLO WORLD"

    def test_uppercase_already_upper(self) -> None:
        conn = _mock_conn("uppercase", False)
        candidate = _candidate(obj="ALREADY")
        result = canonicalize(conn, candidate)
        assert result.object_normalized == "ALREADY"


# ---------------------------------------------------------------------------
# Rule: title_case
# ---------------------------------------------------------------------------

class TestRuleTitleCase:
    def test_title_case(self) -> None:
        conn = _mock_conn("title_case", False)
        candidate = _candidate(obj="  hello world  ")
        result = canonicalize(conn, candidate)
        assert result.object_normalized == "Hello World"


# ---------------------------------------------------------------------------
# Rule: email
# ---------------------------------------------------------------------------

class TestRuleEmail:
    def test_email_strip_and_lower(self) -> None:
        conn = _mock_conn("email", False)
        candidate = _candidate(obj="  Alice@Example.COM  ")
        result = canonicalize(conn, candidate)
        assert result.object_normalized == "alice@example.com"
        assert result.sensitivity == Sensitivity.NORMAL


# ---------------------------------------------------------------------------
# Rule: numeric
# ---------------------------------------------------------------------------

class TestRuleNumeric:
    def test_numeric_plain(self) -> None:
        conn = _mock_conn("numeric", False)
        candidate = _candidate(obj="42")
        result = canonicalize(conn, candidate)
        assert result.object_normalized == "42.00"
        assert result.sensitivity == Sensitivity.NORMAL

    def test_numeric_with_currency_and_comma(self) -> None:
        conn = _mock_conn("numeric", False)
        candidate = _candidate(obj="$1,234.56")
        result = canonicalize(conn, candidate)
        assert result.object_normalized == "1234.56"

    def test_numeric_with_spaces(self) -> None:
        conn = _mock_conn("numeric", False)
        candidate = _candidate(obj="  1 000  ")
        result = canonicalize(conn, candidate)
        assert result.object_normalized == "1000.00"

    def test_numeric_ambiguous_non_numeric(self) -> None:
        """Non-numeric string with 'numeric' rule → ELEVATED."""
        conn = _mock_conn("numeric", False)
        candidate = _candidate(obj="not-a-number")
        result = canonicalize(conn, candidate)
        assert result.object_normalized == "not-a-number"  # unchanged
        assert result.sensitivity == Sensitivity.ELEVATED

    def test_numeric_negative(self) -> None:
        conn = _mock_conn("numeric", False)
        candidate = _candidate(obj="-99.5")
        result = canonicalize(conn, candidate)
        assert result.object_normalized == "-99.50"


# ---------------------------------------------------------------------------
# Rule: date_iso
# ---------------------------------------------------------------------------

class TestRuleDateIso:
    def test_date_iso_already_iso(self) -> None:
        conn = _mock_conn("date_iso", False)
        candidate = _candidate(obj="2026-08-13")
        result = canonicalize(conn, candidate)
        assert result.object_normalized == "2026-08-13"
        assert result.sensitivity == Sensitivity.NORMAL

    def test_date_iso_slash_format(self) -> None:
        conn = _mock_conn("date_iso", False)
        candidate = _candidate(obj="08/13/2026")
        result = canonicalize(conn, candidate)
        assert result.object_normalized == "2026-08-13"

    def test_date_iso_ambiguous(self) -> None:
        """Unparseable date string → ELEVATED."""
        conn = _mock_conn("date_iso", False)
        candidate = _candidate(obj="not-a-date")
        result = canonicalize(conn, candidate)
        assert result.object_normalized == "not-a-date"
        assert result.sensitivity == Sensitivity.ELEVATED


# ---------------------------------------------------------------------------
# Rule: boolean
# ---------------------------------------------------------------------------

class TestRuleBoolean:
    @pytest.mark.parametrize("val,expected", [
        ("yes", "true"),
        ("YES", "true"),
        ("true", "true"),
        ("True", "true"),
        ("1", "true"),
        ("active", "true"),
        ("on", "true"),
        ("no", "false"),
        ("NO", "false"),
        ("false", "false"),
        ("False", "false"),
        ("0", "false"),
        ("inactive", "false"),
        ("off", "false"),
    ])
    def test_boolean_known_values(self, val: str, expected: str) -> None:
        conn = _mock_conn("boolean", False)
        candidate = _candidate(obj=val)
        result = canonicalize(conn, candidate)
        assert result.object_normalized == expected
        assert result.sensitivity == Sensitivity.NORMAL

    def test_boolean_ambiguous(self) -> None:
        """Unknown boolean value → ELEVATED."""
        conn = _mock_conn("boolean", False)
        candidate = _candidate(obj="maybe")
        result = canonicalize(conn, candidate)
        assert result.object_normalized == "maybe"
        assert result.sensitivity == Sensitivity.ELEVATED


# ---------------------------------------------------------------------------
# Rule: tier
# ---------------------------------------------------------------------------

class TestRuleTier:
    @pytest.mark.parametrize("val,expected", [
        ("gold", "gold"),
        ("Gold", "gold"),
        ("GOLD", "gold"),
        ("Gold Tier", "gold"),
        ("gold-tier", "gold"),
        ("gold tier", "gold"),
        ("silver", "silver"),
        ("Silver Tier", "silver"),
        ("bronze", "bronze"),
        ("Bronze", "bronze"),
        ("platinum", "platinum"),
        ("Platinum Tier", "platinum"),
    ])
    def test_tier_known_values(self, val: str, expected: str) -> None:
        conn = _mock_conn("tier", False)
        candidate = _candidate(obj=val)
        result = canonicalize(conn, candidate)
        assert result.object_normalized == expected
        assert result.sensitivity == Sensitivity.NORMAL

    def test_tier_ambiguous_unrecognized(self) -> None:
        """Unrecognized tier value → ELEVATED."""
        conn = _mock_conn("tier", False)
        candidate = _candidate(obj="diamond")
        result = canonicalize(conn, candidate)
        assert result.object_normalized == "diamond"
        assert result.sensitivity == Sensitivity.ELEVATED


# ---------------------------------------------------------------------------
# Rule: carrier
# ---------------------------------------------------------------------------

class TestRuleCarrier:
    @pytest.mark.parametrize("val,expected", [
        ("FedEx", "fedex"),
        ("fedex", "fedex"),
        ("FEDEX", "fedex"),
        ("Federal Express", "fedex"),
        ("UPS", "ups"),
        ("ups", "ups"),
        ("DHL", "dhl"),
        ("dhl", "dhl"),
        ("USPS", "usps"),
        ("usps", "usps"),
        ("us postal", "usps"),
    ])
    def test_carrier_known_values(self, val: str, expected: str) -> None:
        conn = _mock_conn("carrier", False)
        candidate = _candidate(obj=val)
        result = canonicalize(conn, candidate)
        assert result.object_normalized == expected
        assert result.sensitivity == Sensitivity.NORMAL

    def test_carrier_unknown_not_ambiguous(self) -> None:
        """Unknown carrier → lowercased, NOT ELEVATED (free-form names are valid)."""
        conn = _mock_conn("carrier", False)
        candidate = _candidate(obj="Acme Freight")
        result = canonicalize(conn, candidate)
        assert result.object_normalized == "acme freight"
        assert result.sensitivity == Sensitivity.NORMAL


# ---------------------------------------------------------------------------
# Unknown rule → ELEVATED
# ---------------------------------------------------------------------------

class TestUnknownRule:
    def test_unknown_rule_returns_unchanged_and_elevated(self) -> None:
        conn = _mock_conn("completely_unknown_rule", False)
        candidate = _candidate(obj="SomeValue")
        result = canonicalize(conn, candidate)
        assert result.object_normalized == "SomeValue"
        assert result.sensitivity == Sensitivity.ELEVATED


# ---------------------------------------------------------------------------
# is_sensitive=True → always ELEVATED
# ---------------------------------------------------------------------------

class TestIsSensitive:
    def test_sensitive_policy_upgrades_to_elevated(self) -> None:
        """Even with 'lowercase' rule and unambiguous input, is_sensitive=True → ELEVATED."""
        conn = _mock_conn("lowercase", True)
        candidate = _candidate(obj="plainvalue", sensitivity=Sensitivity.NORMAL)
        result = canonicalize(conn, candidate)
        assert result.object_normalized == "plainvalue"
        assert result.sensitivity == Sensitivity.ELEVATED

    def test_sensitive_policy_with_none_rule(self) -> None:
        conn = _mock_conn("none", True)
        candidate = _candidate(obj="anything")
        result = canonicalize(conn, candidate)
        assert result.sensitivity == Sensitivity.ELEVATED

    def test_non_sensitive_policy_inherits_candidate_sensitivity(self) -> None:
        """Non-sensitive policy, unambiguous → sensitivity inherited from candidate."""
        conn = _mock_conn("lowercase", False)
        candidate = _candidate(obj="hello", sensitivity=Sensitivity.ELEVATED)
        result = canonicalize(conn, candidate)
        # ELEVATED because candidate was ELEVATED (inherited)
        assert result.sensitivity == Sensitivity.ELEVATED


# ---------------------------------------------------------------------------
# NormalizedBelief contract: candidate is preserved
# ---------------------------------------------------------------------------

class TestNormalizedBeliefContract:
    def test_candidate_preserved_in_result(self) -> None:
        conn = _mock_conn("lowercase", False)
        candidate = _candidate(obj="Hello")
        result = canonicalize(conn, candidate)
        assert result.candidate is candidate
        assert result.object_normalized == "hello"
