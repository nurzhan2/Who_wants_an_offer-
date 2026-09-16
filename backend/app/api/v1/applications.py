"""The seam between the crawler's database and the local apply agent.

The backend hands out candidates; the agent hands back results. Two endpoints,
no third: ``agent/`` has no database access and never will, and this is the only
contact between the two halves of the project.

**Authentication is local and deliberately nothing more.** A single shared token
from ``AGENT_API_TOKEN``, compared in constant time. There is no user table, no
session, no OAuth and no external identity provider, because there is no second
party to authenticate: this API is bound to the owner's own machine and the only
client is a process the same person started from a terminal on that machine. A
login flow between two of your own processes protects nothing and would only add
a second place for a credential to leak. What the token *does* buy is real and
small: anything else running on the same host — a browser page, another tool —
cannot reach the queue by guessing a URL, and a queue reachable that way would
hand a candidate's letters to whatever asked. If this ever has to answer from
another machine, the fix is not a bigger token in this file; it is not exposing
it, and the deployment notes say so.

Both endpoints refuse to serve at all when no token is configured. An unset
secret must fail loudly rather than quietly disabling the check — CLAUDE.md
rule 4 — because "the agent could not reach the backend" is a five-minute
problem and "the queue was open" is not.

HTTP error text here stays English like the rest of this API; the strings a
person actually reads travel inside the payload, in Russian, because the agent
prints them on the confirmation card.
"""

import secrets
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_session
from app.schemas.agent import (
    CONTRACT_VERSION,
    QueueResponse,
    ResultsRequest,
    ResultsResponse,
)
from app.schemas.operations import AgentClaim, AgentProgress, OperationRead
from app.services import agent_queue
from app.services import operations as operations_service

#: The only scheme accepted. One way in, so there is one thing to get right.
BEARER_PREFIX = "Bearer "


async def require_local_token(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Let the request through only for the configured local token.

    Raises rather than returning a flag: a predicate is checked at some call
    sites and forgotten at others, and this one guards a letter queue.
    """
    configured = settings.agent_api_token
    if configured is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "AGENT_API_TOKEN is not set. The apply queue refuses to serve "
                "without it; set it in .env on both sides."
            ),
        )
    presented = ""
    if authorization is not None and authorization.startswith(BEARER_PREFIX):
        presented = authorization[len(BEARER_PREFIX) :].strip()
    # compare_digest on both branches, so a missing header and a wrong token
    # take the same path and the same time.
    if not secrets.compare_digest(presented, configured.get_secret_value()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid local agent token is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )


router = APIRouter(
    prefix="/applications",
    tags=["applications"],
    dependencies=[Depends(require_local_token)],
)


@router.get(
    "/queue",
    response_model=QueueResponse,
    summary="Vacancies worth an application, best first",
)
async def queue(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    profile_id: Annotated[UUID | None, Query()] = None,
    min_score: Annotated[Decimal | None, Query(ge=0, le=100)] = None,
    require_letter: Annotated[bool, Query()] = True,
) -> QueueResponse:
    """Hand the agent what it needs to open one vacancy and stop.

    Reads only. Being offered a vacancy is not acting on one, so nothing is
    written here; see ``app/services/agent_queue.py`` for why the tracker row
    belongs to the letter that was written and to the result that comes back,
    and to neither end of this request.

    ``require_letter`` defaults to true because an item without a letter is one
    the agent will refuse anyway. Turn it off to see what would be queued once
    the letters are generated.
    """
    return await agent_queue.build_queue(
        session,
        limit=limit,
        profile_id=profile_id,
        min_score=min_score,
        require_letter=require_letter,
    )


@router.post(
    "/results",
    response_model=ResultsResponse,
    summary="Record what the agent saw",
)
async def results(
    payload: ResultsRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ResultsResponse:
    """Take the outcomes back, idempotently.

    The same result posted twice updates the same row and creates no second
    one, which matters because a run that dies between sending and reporting is
    re-reported by hand.
    """
    if payload.version != CONTRACT_VERSION:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Contract version {payload.version} does not match this "
                f"backend's {CONTRACT_VERSION}."
            ),
        )
    return await agent_queue.record_results(session, payload.results)


@router.post(
    "/operations/claim",
    response_model=AgentClaim,
    summary="The local watcher asks for the next thing only it can do",
)
async def claim_operation() -> AgentClaim:
    """Hand the oldest waiting agent request to the watcher, or nothing.

    Behind the token like everything under this prefix: the watcher is the
    owner's own process, and what it is handed decides whether a browser opens
    under their hh login. Asking is also how the dashboard learns a watcher is
    alive, so an empty answer is still a useful call.
    """
    return AgentClaim(operation=operations_service.claim_for_agent())


@router.post(
    "/operations/{operation_id}",
    response_model=OperationRead,
    summary="The local watcher reports on an operation it claimed",
)
async def report_operation(operation_id: UUID, payload: AgentProgress) -> OperationRead:
    """Record progress or the end of an agent operation."""
    return operations_service.report_from_agent(operation_id, payload)
