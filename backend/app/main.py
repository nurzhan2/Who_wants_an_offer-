"""FastAPI application factory and ASGI entrypoint."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.api.v1.router import router as api_v1_router
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import RequestIDMiddleware
from app.db.session import dispose_engine
from app.services.health import service_version

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Configure logging on startup, release the connection pool on shutdown."""
    configure_logging()
    logger.info(
        "application_start",
        environment=settings.environment,
        version=service_version(),
    )
    try:
        yield
    finally:
        await dispose_engine()
        logger.info("application_stop")


def create_app() -> FastAPI:
    """Build the ASGI application."""
    app = FastAPI(
        title="Who wants an offer?",
        description="Resume-driven job aggregator: CV in, scored vacancies out.",
        version=service_version(),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    # Added first, therefore innermost: CORS runs after the request already has an id.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(RequestIDMiddleware)

    register_exception_handlers(app)

    app.include_router(health_router)
    app.include_router(api_v1_router, prefix=settings.api_v1_prefix)
    return app


app = create_app()
