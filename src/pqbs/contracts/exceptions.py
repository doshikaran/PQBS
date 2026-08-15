"""Domain-specific exceptions for the PQBS system.

All exceptions inherit from PQBSError so callers can catch the whole
domain without importing every subclass.
"""
from __future__ import annotations


class PQBSError(Exception):
    """Base class for all PQBS domain errors."""


class BeliefValidationError(PQBSError):
    """Raised when a belief fails structural or semantic validation."""


class ContractionError(PQBSError):
    """Raised when a contradiction is detected between beliefs during resolution."""


class QuarantineError(PQBSError):
    """Raised when a quarantine operation cannot be completed."""


class ScreeningError(PQBSError):
    """Raised when the screening gate encounters an unrecoverable error."""


class RetryExhaustedError(PQBSError):
    """Raised when a serializable transaction exhausts its retry budget.

    Per Security Invariant 4, isolation is never downgraded — we fail
    explicitly with this error rather than retrying with a weaker level.
    """


class TenantIsolationError(PQBSError):
    """Raised when an operation would cross tenant boundaries."""


class AuditSinkError(PQBSError):
    """Raised when the WORM audit sink is unavailable or rejects a write."""


class EmbeddingError(PQBSError):
    """Raised when the embedding service fails or returns a malformed result."""


class InsufficientTrustError(PQBSError):
    """Raised when a recall request asks for beliefs below the minimum trust threshold."""


class AdmissionRejectedError(PQBSError):
    """Raised when A13 rejects a write because the per-agent quota is exhausted.

    The caller must surface this to the producer — unlike throttle, rejected
    writes will not succeed on immediate retry.
    """


class AdmissionThrottledError(PQBSError):
    """Raised when A13 throttles a write because the screening queue is too deep.

    The caller should retry after the delay indicated in the error message.
    Unlike rejection, throttle is transient — queue depth will decrease as the
    gate processes pending beliefs.
    """


class ConsolidationError(PQBSError):
    """Raised when the A8 consolidation agent encounters an unrecoverable error."""
