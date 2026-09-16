"""FastAPI application factory and ASGI entrypoint."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.api.metrics import router as metrics_router
from app.api.v1.router import router as api_v1_router
from app.core.config import settings
from app.core.exceptions import ConfigurationError, register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import RequestIDMiddleware
from app.db.checks import verify_embedding_dimension
from app.db.session import dispose_engine, session_factory
from app.llm.base import LLMTask
from app.llm.router import get_router
from app.services.health import code_fingerprint, service_version
from app.services.resume import fail_interrupted_parses, sweep_orphaned_uploads
from app.sources.http import close_client as close_source_client

logger = get_logger(__name__)


def verify_llm_routing() -> None:
    """Refuse to serve production through the Claude Code CLI.

    The CLI authenticates with a personal subscription. That is the right thing
    on a laptop and the wrong thing on a server: the credential belongs to a
    person, the session limits are per-account, and a shared deployment would
    spend someone's individual quota. Better a startup failure that names the
    task than a service that works until it does not.
    """
    if not settings.is_production:
        return
    routed_to_cli = sorted(task.value for task in LLMTask if settings.provider_for(task) == "cli")
    if routed_to_cli:
        raise ConfigurationError(
            "ENVIRONMENT=production routes these tasks to the Claude Code CLI: "
            f"{routed_to_cli}. Subscription authentication is not for server "
            "deployments; route them to 'api' in LLM_ROUTING."
        )


async def probe_optional_providers() -> None:
    """Ask the providers that can be absent whether they are here.

    Only Ollama needs this: it is a server that may not be running, and the
    router's availability check has to be cheap enough to run before every
    call, so it reads a flag this sets rather than making a request.
    """
    provider = get_router().provider("ollama")
    probe = getattr(provider, "probe", None)
    if probe is not None:
        await probe()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Configure logging on startup, release the connection pool on shutdown."""
    configure_logging()
    logger.info(
        "application_start",
        environment=settings.environment,
        version=service_version(),
    )
    # Cheap, and the alternative is discovering the mismatch on the first
    # embedding write, in a different phase of the project.
    verify_llm_routing()
    # Taken now, while the files on disk are the ones this process imported.
    code_fingerprint()
    async with session_factory() as session:
        await verify_embedding_dimension(session)
        # A parse is a background task of this process, so anything still
        # pending now was left by a process that died and will never finish.
        await fail_interrupted_parses(session)
    await probe_optional_providers()
    # A killed process leaves its staged uploads behind; nothing else deletes
    # them, and uploads/ would grow one resume at a time.
    await sweep_orphaned_uploads()
    try:
        yield
    finally:
        # The source layer owns a connection pool of its own, kept for the life
        # of the process so a crawl reuses connections instead of building one
        # per request.
        await close_source_client()
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
    app.include_router(metrics_router)
    app.include_router(api_v1_router, prefix=settings.api_v1_prefix)
    return app


app = create_app()
