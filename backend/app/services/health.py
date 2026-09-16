"""Health checks for the service and its dependencies."""

import hashlib
import time
from functools import cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

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


@cache
def code_fingerprint() -> str:
    """SHA-256 over this package's ``*.py`` files, taken once per process.

    Taken at the first call, which the lifespan makes at startup, so it
    describes the code the process is running rather than whatever is on disk
    later. ``wwao/fingerprint.py`` computes the same digest from the checkout
    without importing ``app``; ``python -m wwao up`` compares the two and says
    so when the API on its port was started from other code.
    """
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
        digest.update(b"\0")
    return digest.hexdigest()


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
        code_fingerprint=code_fingerprint(),
        environment=settings.environment,
        components=components,
    )
