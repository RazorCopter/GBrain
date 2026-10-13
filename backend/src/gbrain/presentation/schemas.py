from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class GmailAccountResponse(BaseModel):
    id: UUID
    primary_email: str
    status: str
    confirmed_aliases: list[str]
    aliases_ready: bool
    authorized_at: datetime | None
    revoked_at: datetime | None


class GmailStatusResponse(BaseModel):
    configured: bool
    connected: bool
    account: GmailAccountResponse | None
    scope: str
    scope_notice: str
    sent_only: bool
    capabilities: list[str]
    configuration_action: str | None = None


class GmailOAuthStartResponse(BaseModel):
    authorization_url: str
    expires_at: datetime


class GmailDisconnectResponse(BaseModel):
    remote_attempted: bool
    remote_revoked: bool
    detail_code: str


class GmailAliasesRequest(BaseModel):
    aliases: list[str] = Field(default_factory=list, max_length=50)
    confirmed_addresses: list[str] | None = Field(default=None, max_length=50)

    @model_validator(mode="after")
    def validate_aliases_or_confirmed(self) -> GmailAliasesRequest:
        if not self.aliases and self.confirmed_addresses:
            self.aliases = self.confirmed_addresses
        if not self.aliases:
            raise ValueError("Confermare almeno un alias")
        return self


class GmailPolicyRequest(BaseModel):
    after: datetime
    before: datetime
    item_limit: int = Field(ge=1, le=100_000)
    request_budget: int = Field(ge=1, le=200_000)
    batch_size: int = Field(default=25, ge=1, le=500)

    @model_validator(mode="after")
    def validate_period(self) -> GmailPolicyRequest:
        if self.after.tzinfo is None or self.before.tzinfo is None or self.after >= self.before:
            raise ValueError("after deve precedere before e includere il fuso orario")
        return self


class GmailPolicyResponse(GmailPolicyRequest):
    id: UUID
    status: str
    concurrency: int


class GmailPreviewResponse(BaseModel):
    job_id: UUID
    selected_items: int
    pages_read: int
    requests_used: int
    state: str
    truncated: bool
    policy: dict[str, str | int]
    persisted_messages: int = 0
    checkpoint_advanced: bool = False


class GmailImportConfirmRequest(BaseModel):
    policy_id: UUID
    mode: Literal["full", "incremental", "reconciliation"]
    confirmed_policy: dict[str, str | int]
    confirm_import: Literal[True]


class ImportJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source_kind: str
    connector_account_id: UUID | None
    source_policy_id: UUID | None
    queue_job_id: int | None
    mode: str | None
    status: str
    processed_items: int
    total_items: int | None
    checkpoint: dict[str, Any]
    policy_snapshot: dict[str, Any]
    error_code: str | None
    error_detail: str | None
    retry_count: int
    cancel_requested_at: datetime | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ImportJobListResponse(BaseModel):
    items: list[ImportJobResponse]
    total: int


class GmailCitationResponse(BaseModel):
    source_item_id: UUID
    source_revision_id: UUID
    account_id: UUID
    occurred_at: datetime | None
    subject: str | None
    snippet: str | None
    evidence: dict[str, Any]
    proof_url: str
    source_limit: str = "solo posta inviata (SENT)"


class GmailSourceListResponse(BaseModel):
    items: list[GmailCitationResponse]
    total: int
    source_limit: str = "solo posta inviata (SENT)"


class GmailLiveEvidenceResponse(BaseModel):
    source_item_id: UUID
    source_limit: str = "solo posta inviata (SENT), verificato dal wrapper server-side"
    message: dict[str, Any]


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


class ConnectorCapabilitiesResponse(BaseModel):
    source_kind: str
    read_only: bool
    pagination: bool
    incremental: bool
    reconciliation: bool
    max_page_size: int
    concurrency: int
    operations: list[str] = Field(default_factory=list)


class SourceFieldDescriptorResponse(BaseModel):
    name: str
    label: str
    field_type: str
    required: bool
    placeholder: str = ""
    description: str = ""
    help_text: str = ""


class ImportPolicyDescriptorResponse(BaseModel):
    supports_date_range: bool
    default_window_days: int
    max_items_limit: int
    default_item_limit: int
    max_request_budget: int
    default_request_budget: int
    label_restriction: str | None = None


class SourceCatalogItemResponse(BaseModel):
    id: str
    name: str
    category: str
    description: str
    icon: str
    auth_method: str
    status: str
    availability: str = "fully_available"
    test_available: bool
    sync_capabilities: ConnectorCapabilitiesResponse | None = None
    config_fields: list[SourceFieldDescriptorResponse] = Field(default_factory=list)
    import_policy: ImportPolicyDescriptorResponse | None = None
    documentation_url: str | None = None


class SourceCatalogResponse(BaseModel):
    items: list[SourceCatalogItemResponse]
    total: int


class LanWarningResponse(BaseModel):
    is_private_ip: bool
    warning_code: str
    message: str


class SourceStatusResponse(BaseModel):
    source_id: str
    availability: str = "fully_available"
    configured: bool
    auth_source: str | None = None
    client_id_preview: str | None = None
    account_connected: bool = False
    external_account_id: str | None = None
    primary_email: str | None = None
    aliases_ready: bool = False
    confirmed_aliases: list[str] = Field(default_factory=list)
    status: str
    detected_callback_url: str
    lan_warning: LanWarningResponse | None = None


class SourceCredentialsUploadRequest(BaseModel):
    raw_json: dict[str, Any] | str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    redirect_uri_override: str | None = None


class SourceCredentialsSaveResponse(BaseModel):
    source_id: str
    client_id: str | None
    client_type: str
    configured_redirect_uri: str | None
    message: str


class SourceTestConnectionResponse(BaseModel):
    source_id: str
    connected: bool
    latency_ms: float | None = None
    primary_identity: str | None = None
    aliases: list[str] = Field(default_factory=list)
    message: str


class SourcePreviewSampleItem(BaseModel):
    id: str
    date: datetime | None = None
    subject: str | None = None
    to: list[str] = Field(default_factory=list)
    snippet: str | None = None


class SourcePreviewResponse(BaseModel):
    source_id: str
    estimated_count: int
    period_after: datetime
    period_before: datetime
    sample_items: list[SourcePreviewSampleItem] = Field(default_factory=list)
    query: str
    note: str = "Anteprima di sola lettura per posta inviata (SENT)"


class SourceSyncRequest(BaseModel):
    period_after: datetime | None = None
    period_before: datetime | None = None
    item_limit: int = Field(default=500, ge=1, le=5000)
    request_budget: int = Field(default=200, ge=1, le=2000)
    batch_size: int = Field(default=25, ge=1, le=500)
    concurrency: int = Field(default=1, ge=1, le=1)
    mode: Literal["full", "incremental", "reconciliation"] = "full"


class SourceSyncResponse(BaseModel):
    source_id: str
    job_id: UUID
    queue_job_id: int | None
    status: str
    mode: str
    message: str


class SourceDisconnectResponse(BaseModel):
    source_id: str
    disconnected: bool
    remote_revoked: bool
    credentials_removed: bool
    message: str
