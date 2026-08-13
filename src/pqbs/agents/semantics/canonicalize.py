"""A11 Canonicalization agent.

Fetches the predicate_policy for (tenant_id, predicate) and applies the
configured normalization_rule to the belief's object value. If normalization
is ambiguous (e.g., a non-numeric string for 'numeric' rule), sensitivity is
upgraded to ELEVATED.

If the policy declares is_sensitive=True, sensitivity is always ELEVATED
regardless of normalization outcome.
"""
from __future__ import annotations

from typing import Any

import psycopg
import structlog

from pqbs.contracts import (
    CandidateBelief,
    NormalizedBelief,
    Sensitivity,
)

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Date parsing — dateutil is optional; fall back to manual format list.
# ---------------------------------------------------------------------------

_DATE_FORMATS = [
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%d-%m-%Y",
    "%B %d %Y",
    "%b %d %Y",
]


def _parse_date(value: str) -> str | None:
    """Parse a date string into ISO 8601 format. Returns None on failure."""
    try:
        from dateutil import parser as dateutil_parser  # type: ignore[import-untyped]
        dt = dateutil_parser.parse(value)
        return dt.strftime("%Y-%m-%d")
    except ImportError:
        pass
    except Exception:
        return None

    # Manual fallback
    from datetime import datetime
    stripped = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(stripped, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Tier and carrier lookup maps
# ---------------------------------------------------------------------------

_TIER_MAP: dict[str, str] = {
    "gold": "gold",
    "gold tier": "gold",
    "gold-tier": "gold",
    "Gold": "gold",
    "GOLD": "gold",
    "Gold Tier": "gold",
    "silver": "silver",
    "silver tier": "silver",
    "silver-tier": "silver",
    "Silver": "silver",
    "SILVER": "silver",
    "Silver Tier": "silver",
    "bronze": "bronze",
    "bronze tier": "bronze",
    "bronze-tier": "bronze",
    "Bronze": "bronze",
    "BRONZE": "bronze",
    "Bronze Tier": "bronze",
    "platinum": "platinum",
    "platinum tier": "platinum",
    "platinum-tier": "platinum",
    "Platinum": "platinum",
    "PLATINUM": "platinum",
    "Platinum Tier": "platinum",
}

_CARRIER_MAP: dict[str, str] = {
    "FedEx": "fedex",
    "fedex": "fedex",
    "FEDEX": "fedex",
    "Federal Express": "fedex",
    "UPS": "ups",
    "ups": "ups",
    "DHL": "dhl",
    "dhl": "dhl",
    "USPS": "usps",
    "usps": "usps",
    "us postal": "usps",
    "US Postal": "usps",
    "US POSTAL": "usps",
}


# ---------------------------------------------------------------------------
# Per-rule normalization
# ---------------------------------------------------------------------------

def _apply_rule(
    rule: str,
    value: str,
) -> tuple[str, bool]:
    """Apply normalization rule to value.

    Returns (normalized_value, is_ambiguous).
    is_ambiguous=True triggers ELEVATED sensitivity.
    """
    if rule == "none":
        return value, False

    if rule == "lowercase":
        return value.strip().lower(), False

    if rule == "uppercase":
        return value.strip().upper(), False

    if rule == "title_case":
        return value.strip().title(), False

    if rule == "email":
        return value.strip().lower(), False

    if rule == "numeric":
        cleaned = value.strip().replace("$", "").replace(",", "").replace(" ", "")
        try:
            float_val = float(cleaned)
            return f"{float_val:.2f}", False
        except (ValueError, TypeError):
            logger.warning("numeric_parse_failed", value=value)
            return value, True  # ambiguous

    if rule == "date_iso":
        parsed = _parse_date(value.strip())
        if parsed is None:
            logger.warning("date_iso_parse_failed", value=value)
            return value, True  # ambiguous
        return parsed, False

    if rule == "boolean":
        lower = value.strip().lower()
        if lower in {"yes", "true", "1", "active", "on"}:
            return "true", False
        if lower in {"no", "false", "0", "inactive", "off"}:
            return "false", False
        logger.warning("boolean_parse_failed", value=value)
        return value, True  # ambiguous

    if rule == "tier":
        stripped = value.strip()
        canonical = _TIER_MAP.get(stripped)
        if canonical is None:
            logger.warning("tier_unrecognized", value=value)
            return value, True  # ambiguous
        return canonical, False

    if rule == "carrier":
        stripped = value.strip()
        canonical = _CARRIER_MAP.get(stripped)
        if canonical is None:
            # Free-form carrier names are valid — not ambiguous
            return stripped.lower(), False
        return canonical, False

    # Unknown rule — log and treat as ambiguous
    logger.warning("unknown_normalization_rule", rule=rule)
    return value, True


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

def canonicalize(
    conn: psycopg.Connection[Any],
    candidate: CandidateBelief,
) -> NormalizedBelief:
    """Normalize candidate.object according to the predicate_policy for
    (tenant_id, predicate).

    Fetches the policy row from predicate_policy. If none exists, uses
    normalization_rule='none' and is_sensitive=False.

    Returns a NormalizedBelief with:
    - object_normalized: the canonical form of the object
    - sensitivity: ELEVATED if normalization is ambiguous OR is_sensitive=True
    """
    row = conn.execute(
        """
        SELECT normalization_rule, is_sensitive
        FROM predicate_policy
        WHERE tenant_id = %s AND predicate = %s
        """,
        (str(candidate.tenant_id), candidate.predicate),
    ).fetchone()

    if row is None:
        normalization_rule: str = "none"
        is_sensitive: bool = False
        logger.debug(
            "no_predicate_policy",
            tenant_id=str(candidate.tenant_id),
            predicate=candidate.predicate,
        )
    else:
        normalization_rule = row["normalization_rule"] or "none"
        is_sensitive = bool(row["is_sensitive"])

    normalized_value, is_ambiguous = _apply_rule(normalization_rule, candidate.object)

    # Sensitivity resolution:
    # - Policy declares is_sensitive=True → always ELEVATED
    # - Normalization was ambiguous → ELEVATED
    # - Otherwise → inherit from candidate
    if is_sensitive or is_ambiguous:
        sensitivity = Sensitivity.ELEVATED
    else:
        sensitivity = candidate.sensitivity

    logger.debug(
        "canonicalize_done",
        tenant_id=str(candidate.tenant_id),
        predicate=candidate.predicate,
        rule=normalization_rule,
        is_sensitive=is_sensitive,
        is_ambiguous=is_ambiguous,
        sensitivity=sensitivity.value,
    )

    return NormalizedBelief(
        candidate=candidate,
        object_normalized=normalized_value,
        sensitivity=sensitivity,
    )
