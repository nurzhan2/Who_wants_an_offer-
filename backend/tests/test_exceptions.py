"""Domain errors render as RFC 7807 problem documents."""

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.core.exceptions import (
    PROBLEM_JSON,
    AppError,
    LLMError,
    ParsingError,
    RateLimitError,
    SourceError,
    register_exception_handlers,
)
from app.core.middleware import REQUEST_ID_HEADER, RequestIDMiddleware


@pytest.fixture
def problem_app() -> FastAPI:
    """Tiny app whose only job is to raise, one route per failure mode."""
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)
    register_exception_handlers(app)

    @app.get("/source")
    async def _source() -> None:
        raise SourceError("hh.ru returned 500", source_slug="hh")

    @app.get("/rate-limit")
    async def _rate_limit() -> None:
        raise RateLimitError("slow down", source_slug="adzuna", retry_after=12.5)

    @app.get("/parsing")
    async def _parsing() -> None:
        raise ParsingError("resume has no extractable text")

    @app.get("/llm")
    async def _llm() -> None:
        raise LLMError("model returned invalid JSON twice")

    @app.get("/http")
    async def _http() -> None:
        raise HTTPException(status_code=404, detail="Vacancy not found")

    @app.get("/validation")
    async def _validation(score_min: int) -> int:
        return score_min

    @app.get("/boom")
    async def _boom() -> None:
        raise RuntimeError("something nobody predicted")

    return app


@pytest_asyncio.fixture
async def problem_client(problem_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Client that returns the 500 response instead of re-raising it."""
    transport = ASGITransport(app=problem_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def test_to_problem_has_the_rfc_7807_shape() -> None:
    """The document carries type, title, status and detail at minimum."""
    problem = SourceError("boom", source_slug="hh").to_problem(instance="/api/v1/vacancies")

    assert problem["status"] == 502
    assert problem["title"] == "Job source failed"
    assert problem["detail"] == "boom"
    assert problem["instance"] == "/api/v1/vacancies"
    assert problem["type"].endswith("#source-error")
    assert problem["source_slug"] == "hh"


def test_rate_limit_error_is_a_source_error() -> None:
    """Retry logic can catch SourceError and still see rate limiting."""
    error = RateLimitError("429", source_slug="adzuna", retry_after=30.0)

    assert isinstance(error, SourceError)
    assert error.status_code == 429
    assert error.to_problem()["retry_after"] == 30.0


@pytest.mark.parametrize(
    ("path", "expected_status", "expected_type"),
    [
        ("/source", 502, "source-error"),
        ("/rate-limit", 429, "rate-limited"),
        ("/parsing", 422, "parsing-error"),
        ("/llm", 502, "llm-error"),
    ],
)
async def test_domain_errors_render_as_problem_json(
    problem_client: AsyncClient,
    path: str,
    expected_status: int,
    expected_type: str,
) -> None:
    """Every domain error maps onto its own status and stable type slug."""
    response = await problem_client.get(path)

    assert response.status_code == expected_status
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    body = response.json()
    assert body["type"].endswith(f"#{expected_type}")
    assert body["status"] == expected_status
    assert body["instance"] == path


async def test_problem_carries_the_request_id(problem_client: AsyncClient) -> None:
    """A failing response can be traced back to its log line."""
    response = await problem_client.get("/source", headers={REQUEST_ID_HEADER: "trace-me"})

    assert response.json()["request_id"] == "trace-me"


async def test_http_exception_renders_as_problem_json(problem_client: AsyncClient) -> None:
    """FastAPI's own HTTPException is not allowed to escape the format."""
    response = await problem_client.get("/http")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    body = response.json()
    assert body["title"] == "Vacancy not found"
    assert body["type"].endswith("#http-error")


async def test_validation_error_keeps_field_details(problem_client: AsyncClient) -> None:
    """A client must be able to see which field it got wrong."""
    response = await problem_client.get("/validation", params={"score_min": "not-a-number"})

    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    body = response.json()
    assert body["type"].endswith("#validation-error")
    assert body["errors"]
    assert body["errors"][0]["loc"] == ["query", "score_min"]


async def test_unhandled_exception_is_opaque(problem_client: AsyncClient) -> None:
    """An unexpected crash returns 500 without leaking internals to the client."""
    response = await problem_client.get("/boom")

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "An unexpected error occurred."
    assert "something nobody predicted" not in response.text


def test_app_error_defaults_to_internal_error() -> None:
    """A subclass that forgets to set its status still produces a valid document."""

    class WeirdError(AppError):
        pass

    problem = WeirdError("hm").to_problem()

    assert problem["status"] == 500
    assert problem["type"].endswith("#internal-error")
