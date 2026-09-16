"""Schemas for the health endpoint."""

from typing import Literal

from pydantic import BaseModel, Field

ComponentState = Literal["ok", "error"]
ServiceState = Literal["ok", "degraded"]


class ComponentHealth(BaseModel):
    """Status of a single dependency the service needs to do its job."""

    status: ComponentState
    detail: str | None = Field(default=None, description="Error summary when status is 'error'.")
    latency_ms: float | None = Field(default=None, ge=0)


class HealthResponse(BaseModel):
    """Aggregate service health, one entry per checked dependency."""

    status: ServiceState
    version: str
    #: SHA-256 of the source this process was started from. The package version
    #: is the same for every build, so this is what tells a launcher that the
    #: API answering on its port is not the code in its checkout.
    code_fingerprint: str
    environment: str
    components: dict[str, ComponentHealth]
