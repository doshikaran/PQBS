"""Shared configuration and type aliases for all contracts."""
from __future__ import annotations

from pydantic import ConfigDict

# The embedding dimension used by amazon.titan-embed-text-v2:0 (confirmed V1)
EMBEDDING_DIM: int = 1024

# All contracts share this config: frozen (immutable), strict validation, no extras
CONTRACT_CONFIG = ConfigDict(
    frozen=True,
    strict=False,           # allow coercion for datetime/UUID from strings
    extra="forbid",         # reject unknown fields — contracts are closed
    populate_by_name=True,
)
