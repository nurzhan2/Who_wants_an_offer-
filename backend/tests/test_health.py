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


async def test_health_names_the_code_it_was_started_from(client_without_db: AsyncClient) -> None:
    """The package version is the same for every build; this is not.

    ``wwao up`` compares it with the checkout it runs from — see
    ``wwao/tests/test_fingerprint.py`` for the two sides agreeing.
    """
    from app.services.health import code_fingerprint

    body = (await client_without_db.get("/health")).json()

    assert body["code_fingerprint"] == code_fingerprint()
    assert len(body["code_fingerprint"]) == 64


def test_the_launcher_computes_the_same_digest_from_disk() -> None:
    """Otherwise ``wwao up`` would refuse every perfectly current server.

    ``wwao`` is loaded by path: it is not importable from this test directory,
    and it must not import ``app`` itself.
    """
    import importlib.util
    from pathlib import Path

    from app.services.health import code_fingerprint

    path = Path(__file__).resolve().parents[2] / "wwao" / "fingerprint.py"
    spec = importlib.util.spec_from_file_location("wwao_fingerprint", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.code_fingerprint() == code_fingerprint()
