"""The overview's operation buttons.

Validate, call :mod:`app.services.operations`, answer — nothing else lives here
(CLAUDE.md rule 2). Why the operations are in-memory jobs, why a second copy is
refused and why two of them are only *recorded* here is argued in the service.

Starting is a JSON ``POST`` rather than a bare form post on purpose. A page on
another origin cannot send ``Content-Type: application/json`` without a CORS
preflight, and this API answers preflights only for the dashboard's own origin,
so a stray web page cannot press these buttons for the owner.
"""

from uuid import UUID

from fastapi import APIRouter, status

from app.schemas.operations import OperationRead, OperationsState, StartOperation
from app.services import operations as operations_service

router = APIRouter(prefix="/operations", tags=["dashboard"])


@router.get("", response_model=OperationsState, summary="Newest run of every operation")
async def read_operations() -> OperationsState:
    """What the operations panel draws: one line per kind and which are busy."""
    return operations_service.state()


@router.post(
    "",
    response_model=OperationRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start an operation and answer with a handle to poll",
)
async def start_operation(payload: StartOperation) -> OperationRead:
    """Start one operation. 409 when the same kind is already running."""
    return await operations_service.start(payload.kind)


@router.delete(
    "/{operation_id}",
    response_model=OperationRead,
    summary="Withdraw a request the local agent has not picked up",
)
async def cancel_operation(operation_id: UUID) -> OperationRead:
    """Only an agent request still waiting can be withdrawn; anything else is 409."""
    return operations_service.cancel(operation_id)
