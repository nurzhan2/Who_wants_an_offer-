"""Domain exceptions and RFC 7807 (``application/problem+json``) error handlers."""

from typing import Any, cast

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger, get_request_id

logger = get_logger(__name__)

PROBLEM_JSON = "application/problem+json"
#: Base URI for problem types; resolves to the error catalogue in the docs.
PROBLEM_TYPE_BASE = "https://github.com/nurzhan2/Who_wants_an_offer-/blob/main/docs/errors.md"


class AppError(Exception):
    """Base class for every expected, domain-level failure.

    Subclasses map onto a single HTTP status and a stable ``problem_type``
    slug, so clients can branch on the type instead of parsing prose.
    """

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    title: str = "Internal Server Error"
    problem_type: str = "internal-error"

    def __init__(self, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.detail = detail
        self.extra = extra

    def to_problem(self, instance: str | None = None) -> dict[str, Any]:
        """Serialise into an RFC 7807 problem document."""
        problem: dict[str, Any] = {
            "type": f"{PROBLEM_TYPE_BASE}#{self.problem_type}",
            "title": self.title,
            "status": self.status_code,
            "detail": self.detail,
        }
        if instance is not None:
            problem["instance"] = instance
        request_id = get_request_id()
        if request_id is not None:
            problem["request_id"] = request_id
        problem.update(self.extra)
        return problem


class SourceError(AppError):
    """A job source failed: transport error, unexpected payload, or refusal."""

    status_code = status.HTTP_502_BAD_GATEWAY
    title = "Job source failed"
    problem_type = "source-error"

    def __init__(self, detail: str, *, source_slug: str | None = None, **extra: Any) -> None:
        if source_slug is not None:
            extra["source_slug"] = source_slug
        super().__init__(detail, **extra)


class RateLimitError(SourceError):
    """A source refused the request because we exceeded its rate limit."""

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    title = "Rate limited"
    problem_type = "rate-limited"

    def __init__(
        self,
        detail: str,
        *,
        source_slug: str | None = None,
        retry_after: float | None = None,
        **extra: Any,
    ) -> None:
        if retry_after is not None:
            extra["retry_after"] = retry_after
        super().__init__(detail, source_slug=source_slug, **extra)


class ParsingError(AppError):
    """Input could not be turned into a structured record (resume, posting, salary)."""

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    title = "Could not parse input"
    problem_type = "parsing-error"


class LLMError(AppError):
    """The LLM call failed, or returned something the schema rejects twice in a row."""

    status_code = status.HTTP_502_BAD_GATEWAY
    title = "LLM call failed"
    problem_type = "llm-error"


async def app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render an :class:`AppError` as problem+json."""
    error = cast(AppError, exc)
    logger.warning(
        "app_error",
        problem_type=error.problem_type,
        status_code=error.status_code,
        detail=error.detail,
        path=request.url.path,
    )
    return JSONResponse(
        status_code=error.status_code,
        content=error.to_problem(instance=request.url.path),
        media_type=PROBLEM_JSON,
    )


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render FastAPI's own ``HTTPException`` as problem+json."""
    error = cast(StarletteHTTPException, exc)
    problem: dict[str, Any] = {
        "type": f"{PROBLEM_TYPE_BASE}#http-error",
        "title": error.detail if isinstance(error.detail, str) else "HTTP error",
        "status": error.status_code,
        "detail": error.detail if isinstance(error.detail, str) else str(error.detail),
        "instance": request.url.path,
    }
    request_id = get_request_id()
    if request_id is not None:
        problem["request_id"] = request_id
    return JSONResponse(
        status_code=error.status_code,
        content=problem,
        media_type=PROBLEM_JSON,
        headers=error.headers,
    )


async def validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render request-validation failures as problem+json, keeping field errors."""
    error = cast(RequestValidationError, exc)
    problem: dict[str, Any] = {
        "type": f"{PROBLEM_TYPE_BASE}#validation-error",
        "title": "Request validation failed",
        "status": status.HTTP_422_UNPROCESSABLE_CONTENT,
        "detail": "One or more fields are invalid.",
        "instance": request.url.path,
        "errors": error.errors(),
    }
    request_id = get_request_id()
    if request_id is not None:
        problem["request_id"] = request_id
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=cast(dict[str, Any], jsonable_encoder(problem)),
        media_type=PROBLEM_JSON,
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last resort: log the traceback, return an opaque problem document."""
    logger.exception("unhandled_exception", path=request.url.path, error=str(exc))
    problem: dict[str, Any] = {
        "type": f"{PROBLEM_TYPE_BASE}#internal-error",
        "title": "Internal Server Error",
        "status": status.HTTP_500_INTERNAL_SERVER_ERROR,
        "detail": "An unexpected error occurred.",
        "instance": request.url.path,
    }
    request_id = get_request_id()
    if request_id is not None:
        problem["request_id"] = request_id
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=problem,
        media_type=PROBLEM_JSON,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Wire every handler above onto the application."""
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
