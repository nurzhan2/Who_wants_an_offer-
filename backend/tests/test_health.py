"""Smoke tests for the health endpoint."""

import pytest
from httpx import AsyncClient

from app.core.middleware import REQUEST_ID_HEADER


@pytest.mark.db
async def test_health_ok_with_database(async_client: AsyncClient) -> None:
    """With a live database the service reports itself healthy."""
    response = await async_client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["components"]["database"]["status"] == "ok"
    assert body["components"]["database"]["latency_ms"] >= 0
    assert body["environment"]


async def test_health_degraded_without_database(client_without_db: AsyncClient) -> None:
    """A dead database degrades the service to 503 instead of crashing it."""
    response = await client_without_db.get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["components"]["database"]["status"] == "error"


async def test_health_echoes_request_id(client_without_db: AsyncClient) -> None:
    """An inbound correlation id is reflected back on the response."""
    response = await client_without_db.get("/health", headers={REQUEST_ID_HEADER: "abc-123"})

    assert response.headers[REQUEST_ID_HEADER] == "abc-123"


async def test_health_mints_request_id_when_absent(client_without_db: AsyncClient) -> None:
    """Every response carries a correlation id, even when the client sent none."""
    response = await client_without_db.get("/health")

    assert response.headers[REQUEST_ID_HEADER]
