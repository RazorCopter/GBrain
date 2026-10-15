from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol, TypeVar

SelectionT_contra = TypeVar("SelectionT_contra", contravariant=True)
CursorT = TypeVar("CursorT")
LocatorT = TypeVar("LocatorT")
RawT = TypeVar("RawT")
NormalizedT_co = TypeVar("NormalizedT_co", covariant=True)
CheckpointT = TypeVar("CheckpointT")
ReconcileT_co = TypeVar("ReconcileT_co", covariant=True)


class ConnectorErrorCategory(StrEnum):
    AUTH = "auth"
    QUOTA = "quota"
    TRANSIENT = "transient"
    HISTORY_EXPIRED = "history_expired"
    PERMANENT = "permanent"
    PARSE = "parse"
    PERIMETER = "perimeter"
    BUDGET = "budget"
    CHECKPOINT_CONFLICT = "checkpoint_conflict"


class ConnectorError(RuntimeError):
    def __init__(
        self,
        category: ConnectorErrorCategory,
        safe_message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
        reason_code: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(safe_message)
        self.category = category
        self.safe_message = safe_message
        self.retryable = retryable
        self.status_code = status_code
        self.reason_code = reason_code
        self.retry_after_seconds = retry_after_seconds


class GmailMimeError(ConnectorError):
    def __init__(self, reason_code: str, safe_message: str | None = None) -> None:
        super().__init__(
            ConnectorErrorCategory.PARSE,
            safe_message or f"Parsing MIME fallito: {reason_code}",
            reason_code=reason_code,
        )


class BudgetExhaustedError(ConnectorError):
    def __init__(self, resource: str) -> None:
        super().__init__(
            ConnectorErrorCategory.BUDGET,
            f"Budget del connettore esaurito: {resource}",
        )
        self.resource = resource


class SourceCategory(StrEnum):
    EMAIL_COMMUNICATIONS = "email_communications"
    CLOUD_STORAGE = "cloud_storage"
    MEDIA_PHOTOS = "media_photos"
    LOCAL_STORAGE = "local_storage"
    DEVELOPER_CODE = "developer_code"
    CONVERSATIONS_AI = "conversations_ai"
    NETWORKING = "networking"


class AuthenticationMethod(StrEnum):
    OAUTH2_AUTHORIZATION_CODE = "oauth2_authorization_code"
    API_TOKEN_HEADER = "api_token_header"  # noqa: S105
    BEARER_TOKEN = "bearer_token"  # noqa: S105
    FILESYSTEM_PATH = "filesystem_path"
    ARCHIVE_UPLOAD = "archive_upload"
    NONE = "none"


class ConnectorStatus(StrEnum):
    FULLY_AVAILABLE = "fully_available"
    COMING_SOON = "coming_soon"
    MAINTENANCE = "maintenance"
    DEPRECATED = "deprecated"


class SourceOperationalStatus(StrEnum):
    NOT_CONFIGURED = "not_configured"
    CREDENTIALS_CONFIGURED = "credentials_configured"
    CONNECTED = "connected"
    ERROR = "error"
    SUSPENDED = "suspended"


@dataclass(frozen=True, slots=True)
class SourceFieldDescriptor:
    name: str
    label: str
    field_type: str
    required: bool = True
    placeholder: str = ""
    description: str = ""
    help_text: str = ""


@dataclass(frozen=True, slots=True)
class ImportPolicyDescriptor:
    supports_date_range: bool = True
    default_window_days: int = 30
    max_items_limit: int = 5000
    default_item_limit: int = 500
    max_request_budget: int = 2000
    default_request_budget: int = 200
    label_restriction: str | None = None


@dataclass(frozen=True, slots=True)
class SourceDescriptor:
    id: str
    name: str
    category: SourceCategory
    description: str
    icon: str
    auth_method: AuthenticationMethod
    status: ConnectorStatus
    test_available: bool = False
    sync_capabilities: ConnectorCapabilities | None = None
    config_fields: tuple[SourceFieldDescriptor, ...] = ()
    import_policy: ImportPolicyDescriptor | None = None
    documentation_url: str | None = None


class PageState(StrEnum):
    COMPLETE = "complete"
    PARTIAL_BUDGET = "partial_budget"
    TRUNCATED_LIMIT = "truncated_limit"


@dataclass(frozen=True, slots=True)
class ConnectionStatus:
    connected: bool
    account_reference: str | None
    checked_at: datetime
    detail_code: str


@dataclass(frozen=True, slots=True)
class ConnectorCapabilities:
    source_kind: str
    read_only: bool
    pagination: bool
    incremental: bool
    reconciliation: bool
    max_page_size: int
    concurrency: int
    operations: tuple[str, ...]


@dataclass(slots=True)
class RequestBudget:
    max_requests: int
    max_items: int
    requests_used: int = 0
    items_used: int = 0

    def __post_init__(self) -> None:
        if self.max_requests < 1:
            raise ValueError("max_requests deve essere positivo")
        if self.max_items < 1:
            raise ValueError("max_items deve essere positivo")
        if self.requests_used < 0 or self.items_used < 0:
            raise ValueError("I contatori di budget non possono essere negativi")
        if self.requests_used > self.max_requests or self.items_used > self.max_items:
            raise ValueError("I contatori iniziali superano il budget")

    @property
    def requests_remaining(self) -> int:
        return self.max_requests - self.requests_used

    @property
    def items_remaining(self) -> int:
        return self.max_items - self.items_used

    def consume_request(self) -> None:
        if self.requests_remaining <= 0:
            raise BudgetExhaustedError("richieste")
        self.requests_used += 1

    def consume_items(self, count: int) -> int:
        if count < 0:
            raise ValueError("count non può essere negativo")
        accepted = min(count, self.items_remaining)
        self.items_used += accepted
        return accepted


@dataclass(frozen=True, slots=True)
class ConnectorPage[LocatorT, CursorT]:
    items: tuple[LocatorT, ...]
    next_cursor: CursorT | None
    state: PageState


@dataclass(frozen=True, slots=True)
class PreviewResult:
    selected_items: int
    pages_read: int
    state: PageState
    requests_used: int
    next_cursor_present: bool


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    observed_items: int
    unchanged_items: int
    revised_items: int
    removed_items: int
    out_of_scope_items: int
    excluded_items_preserved: int
    state: PageState
    detail_codes: tuple[str, ...] = field(default_factory=tuple)


class ConnectorContract(
    Protocol[
        SelectionT_contra,
        CursorT,
        LocatorT,
        RawT,
        NormalizedT_co,
        CheckpointT,
        ReconcileT_co,
    ]
):
    """Contratto semantico comune; non espone oggetti SDK proprietari."""

    def test_connection(self, budget: RequestBudget) -> ConnectionStatus: ...

    def inspect_capabilities(self) -> ConnectorCapabilities: ...

    def preview_selection(
        self, selection: SelectionT_contra, budget: RequestBudget
    ) -> PreviewResult: ...

    def enumerate_page(
        self,
        selection: SelectionT_contra,
        cursor: CursorT | None,
        budget: RequestBudget,
    ) -> ConnectorPage[LocatorT, CursorT]: ...

    def fetch_item(self, locator: LocatorT, budget: RequestBudget) -> RawT: ...

    def normalize_item(self, raw_data: RawT) -> NormalizedT_co: ...

    def checkpoint(self, state: CheckpointT) -> CheckpointT: ...

    def reconcile(
        self,
        selection: SelectionT_contra,
        checkpoint: CheckpointT,
        budget: RequestBudget,
    ) -> ReconcileT_co: ...


SOURCE_CATALOG: tuple[SourceDescriptor, ...] = (
    SourceDescriptor(
        id="gmail",
        name="Google Gmail",
        category=SourceCategory.EMAIL_COMMUNICATIONS,
        description="Sincronizzazione riservata delle email inviate (SENT) con perimetro read-only.",
        icon="gmail",
        auth_method=AuthenticationMethod.OAUTH2_AUTHORIZATION_CODE,
        status=ConnectorStatus.FULLY_AVAILABLE,
        test_available=True,
        sync_capabilities=ConnectorCapabilities(
            source_kind="gmail",
            read_only=True,
            pagination=True,
            incremental=True,
            reconciliation=True,
            max_page_size=100,
            concurrency=1,
            operations=(
                "test_connection",
                "preview_selection",
                "enumerate_page",
                "fetch_raw",
                "normalize_content",
                "reconcile",
            ),
        ),
        config_fields=(
            SourceFieldDescriptor(
                name="client_id",
                label="Client ID Google OAuth",
                field_type="text",
                required=True,
                placeholder="xxxx.apps.googleusercontent.com",
                description="ID client OAuth 2.0 Web Application",
            ),
            SourceFieldDescriptor(
                name="client_secret",
                label="Client Secret Google",
                field_type="password",
                required=True,
                placeholder="GOCSPX-xxxx",
                description="Client secret riservato dell'applicazione Google Cloud Console",
            ),
            SourceFieldDescriptor(
                name="redirect_uri_override",
                label="Redirect URI personalizzata",
                field_type="url",
                required=False,
                placeholder="https://dominio-o-ip/api/sources/gmail/oauth/callback",
                description="Opzionale; se omessa viene rilevata automaticamente dall'istanza",
            ),
        ),
        import_policy=ImportPolicyDescriptor(
            supports_date_range=True,
            default_window_days=30,
            max_items_limit=5000,
            default_item_limit=500,
            max_request_budget=2000,
            default_request_budget=200,
            label_restriction="SENT",
        ),
        documentation_url="https://console.cloud.google.com/apis/credentials",
    ),
    SourceDescriptor(
        id="google_drive",
        name="Google Drive",
        category=SourceCategory.CLOUD_STORAGE,
        description="Indicizzazione documentale, PDF e fogli di calcolo archiviati su Google Drive.",
        icon="google_drive",
        auth_method=AuthenticationMethod.OAUTH2_AUTHORIZATION_CODE,
        status=ConnectorStatus.COMING_SOON,
        test_available=False,
    ),
    SourceDescriptor(
        id="google_calendar",
        name="Google Calendar",
        category=SourceCategory.EMAIL_COMMUNICATIONS,
        description="Importazione eventi, riunioni storiche e metadati di calendario.",
        icon="google_calendar",
        auth_method=AuthenticationMethod.OAUTH2_AUTHORIZATION_CODE,
        status=ConnectorStatus.COMING_SOON,
        test_available=False,
    ),
    SourceDescriptor(
        id="immich",
        name="Immich Foto & Video",
        category=SourceCategory.MEDIA_PHOTOS,
        description="Asset multimediali, timeline ed eventi fotografici self-hosted da istanza Immich.",
        icon="immich",
        auth_method=AuthenticationMethod.API_TOKEN_HEADER,
        status=ConnectorStatus.FULLY_AVAILABLE,
        test_available=True,
        sync_capabilities=ConnectorCapabilities(
            source_kind="immich",
            read_only=True,
            pagination=True,
            incremental=False,
            reconciliation=True,
            max_page_size=25,
            concurrency=1,
            operations=(
                "test_connection",
                "preview_selection",
                "enumerate_page",
                "fetch_raw",
                "normalize_content",
            ),
        ),
        config_fields=(
            SourceFieldDescriptor(
                name="base_url",
                label="URL Istanza Immich",
                field_type="url",
                required=True,
                placeholder="https://immich.example.com o http://192.168.1.100:2283",
                description="Indirizzo completo dell'istanza Immich (HTTPS pubblico o HTTP LAN/loopback)",
                help_text="Supporta istanze locali o protette da reverse proxy",
            ),
            SourceFieldDescriptor(
                name="api_key",
                label="API Key Immich",
                field_type="password",
                required=True,
                placeholder="xxxx-xxxx-xxxx",
                description="Chiave API generata dalle impostazioni del tuo account in Immich",
                help_text="Permessi di sola lettura sufficienti",
            ),
        ),
        import_policy=ImportPolicyDescriptor(
            supports_date_range=True,
            default_window_days=30,
            max_items_limit=25,
            default_item_limit=25,
            max_request_budget=100,
            default_request_budget=50,
            label_restriction=None,
        ),
    ),
    SourceDescriptor(
        id="synology",
        name="Synology NAS",
        category=SourceCategory.LOCAL_STORAGE,
        description="Cartelle condivise, snapshot e file system su dispositivi NAS Synology via API DSM.",
        icon="synology",
        auth_method=AuthenticationMethod.BEARER_TOKEN,
        status=ConnectorStatus.COMING_SOON,
        test_available=False,
    ),
    SourceDescriptor(
        id="local_documents",
        name="Documenti Locali",
        category=SourceCategory.LOCAL_STORAGE,
        description="Scansione sicura di directory locali del server (PDF, markdown, note di testo).",
        icon="folder",
        auth_method=AuthenticationMethod.FILESYSTEM_PATH,
        status=ConnectorStatus.COMING_SOON,
        test_available=False,
    ),
    SourceDescriptor(
        id="github",
        name="GitHub",
        category=SourceCategory.DEVELOPER_CODE,
        description="Commit history, pull request, issue e documentazione repository.",
        icon="github",
        auth_method=AuthenticationMethod.BEARER_TOKEN,
        status=ConnectorStatus.COMING_SOON,
        test_available=False,
    ),
    SourceDescriptor(
        id="chatgpt_export",
        name="ChatGPT Export",
        category=SourceCategory.CONVERSATIONS_AI,
        description="Importazione sicura di archivi zip esportati da ChatGPT contenenti conversazioni storiche.",
        icon="chatgpt",
        auth_method=AuthenticationMethod.ARCHIVE_UPLOAD,
        status=ConnectorStatus.COMING_SOON,
        test_available=False,
    ),
    SourceDescriptor(
        id="omniroute",
        name="OmniRoute",
        category=SourceCategory.NETWORKING,
        description="Topologia di rete, routing table e diagnostica da apparati OmniRoute.",
        icon="network",
        auth_method=AuthenticationMethod.API_TOKEN_HEADER,
        status=ConnectorStatus.COMING_SOON,
        test_available=False,
    ),
)


def get_source_catalog() -> tuple[SourceDescriptor, ...]:
    """Restituisce l'elenco immutabile di tutti i descrittori di fonte supportati o pianificati."""
    return SOURCE_CATALOG


def get_source_descriptor(source_id: str) -> SourceDescriptor | None:
    """Recupera il descrittore di fonte specificato o None se inesistente."""
    for descriptor in SOURCE_CATALOG:
        if descriptor.id == source_id:
            return descriptor
    return None
