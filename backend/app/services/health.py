"""Health checks for the service and its dependencies."""

import time
from importlib.metadata import PackageNotFoundError, version

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.schemas.health import ComponentHealth, HealthResponse


def service_version() -> str:
    """Version of the installed distribution, or a placeholder when running from source."""
    try:
        return version("who-wants-an-offer")
    except PackageNotFoundError:  # pragma: no cover - only hit outside an installed env
        return "0.0.0"


async def check_database(session: AsyncSession) -> ComponentHealth:
    """Ping the database with the cheapest possible round trip."""
    started = time.perf_counter()
    try:
        await session.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        return ComponentHealth(status="error", detail=type(exc).__name__)
    return ComponentHealth(
        status="ok",
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
    )


async def check_health(session: AsyncSession) -> HealthResponse:
    """Collect the status of every dependency into one response."""
    components = {"database": await check_database(session)}
    overall = "ok" if all(c.status == "ok" for c in components.values()) else "degraded"
    return HealthResponse(
        status=overall,
        version=service_version(),
        environment=settings.environment,
        components=components,
    )
