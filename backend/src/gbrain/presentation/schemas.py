from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class BootstrapRequest(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    display_name: str = Field(min_length=1, max_length=160)
    password: str = Field(min_length=14, max_length=256)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class OwnerResponse(BaseModel):
    id: UUID
    username: str
    display_name: str


class SessionResponse(BaseModel):
    authenticated: bool
    bootstrap_required: bool
    owner: OwnerResponse | None = None
    csrf_token: str | None = None


class ServiceStatus(BaseModel):
    status: Literal["ok", "degraded", "unavailable", "not_implemented"]
    detail: str


class StatusResponse(BaseModel):
    overall: Literal["ok", "degraded"]
    postgres: ServiceStatus
    neo4j_projection: ServiceStatus
    graphiti: ServiceStatus
    demo_mode: bool


class PublicConfigResponse(BaseModel):
    app_name: str = "GBrain"
    language: str = "it"
    demo_mode: bool
    environment: str
    session_ttl_seconds: int
    implemented_sources: list[str] = Field(default_factory=list)


class ImportJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source_kind: str
    status: str
    processed_items: int
    error_code: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ImportJobListResponse(BaseModel):
    items: list[ImportJobResponse]
    total: int


class DemoFixtureStatusResponse(BaseModel):
    enabled: bool
    seeded: bool
    dataset: str
    marker: str
    item_count: int
    revision_count: int
    import_job_count: int
    expected_item_count: int
    expected_revision_count: int
    cases: list[str]
    security_controls_unchanged: bool = True
    real_integrations: list[str] = Field(default_factory=list)
