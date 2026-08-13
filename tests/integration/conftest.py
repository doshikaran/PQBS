"""Integration test fixtures — require a live CockroachDB connection."""
from __future__ import annotations

import os
import uuid
from typing import Generator

from typing import Any

import psycopg
import pytest
from dotenv import load_dotenv
from psycopg.rows import dict_row

load_dotenv()

# Fixed tenant UUID for all integration tests — isolated from demo data.
INTEGRATION_TENANT_ID = uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")


@pytest.fixture(scope="session")
def db_url() -> str:
    url = os.environ.get("COCKROACH_URL")
    if not url:
        pytest.skip("COCKROACH_URL not set — skipping integration tests")
    return url


@pytest.fixture(scope="session")
def db_conn(db_url: str) -> Generator[psycopg.Connection[Any], None, None]:
    """Session-scoped raw psycopg3 connection. Autocommit so each test controls its own transactions."""
    conn = psycopg.connect(db_url, autocommit=True, row_factory=dict_row)  # type: ignore[arg-type]
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def integration_tenant_id() -> uuid.UUID:
    return INTEGRATION_TENANT_ID
