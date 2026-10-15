from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import desc as sa_desc
from sqlalchemy import select

from gbrain.application.crypto import encrypt_json
from gbrain.application.gmail_oauth import (
    GmailOAuthError,
    GmailOAuthNotConfiguredError,
    begin_oauth,
    complete_oauth,
    decrypt_credentials,
    disconnect_account,
    encrypt_credentials,
    is_gmail_configured,
    normalize_google_client_config,
    refresh_credentials,
)
from gbrain.application.gmail_service import (
    GmailServiceError,
    PolicyInput,
    SyncConfigInput,
    create_and_enqueue_job,
    enqueue_continuous_update_job,
    enqueue_history_import_job,
    get_gmail_account,
    get_or_create_sync_config,
    preview_history_selection,
    save_policy,
    update_sync_config,
)
from gbrain.application.import_job_cancel import cancel_import_job
from gbrain.application.import_job_runtime import build_import_job_runtime
from gbrain.domain.connectors import (
    RequestBudget,
    SourceOperationalStatus,
    get_source_catalog,
    get_source_descriptor,
)
from gbrain.domain.immich import ImmichErrorCategory, ImmichGatewayError
from gbrain.domain.job_queue import GMAIL_QUEUE_IDENTITY, IMMICH_QUEUE_IDENTITY
from gbrain.domain.models import (
    ConnectorAccount,
    ConnectorAccountStatus,
    ImportJob,
    ImportJobStatus,
)
from gbrain.infrastructure.gmail_gateway import OfficialGmailGateway
from gbrain.infrastructure.gmail_rate_limiter import PostgresGmailRateLimiter
from gbrain.infrastructure.gmail_repository import AsyncPostgresGmailRepository
from gbrain.infrastructure.source_credentials_repository import SourceCredentialsRepository
from gbrain.presentation.dependencies import (
    AppSettings,
    CsrfPrincipalDep,
    DbSession,
    PrincipalDep,
    ProcrastinateApp,
)
from gbrain.presentation.schemas import (
    ConnectorCapabilitiesResponse,
    GmailBackfillStateResponse,
    GmailCoverageResponse,
    GmailItemGapListResponse,
    GmailItemGapResponse,
    GmailOAuthStartResponse,
    GmailPreviewResponse,
    HistoryImportRequest,
    HistoryPreviewRequest,
    ImmichAssetListResponse,
    ImmichAssetResponse,
    ImportJobResponse,
    ImportPolicyDescriptorResponse,
    LanWarningResponse,
    SourceCatalogItemResponse,
    SourceCatalogResponse,
    SourceCredentialsSaveResponse,
    SourceCredentialsUploadRequest,
    SourceDisconnectResponse,
    SourceFieldDescriptorResponse,
    SourcePreviewResponse,
    SourcePreviewSampleItem,
    SourceStatusResponse,
    SourceSyncConfigRequest,
    SourceSyncConfigResponse,
    SourceSyncRequest,
    SourceSyncResponse,
    SourceTestConnectionResponse,
)

GMAIL_TASK_NAME = GMAIL_QUEUE_IDENTITY.task_name
logger = logging.getLogger(__name__)

sources_router = APIRouter(prefix="/sources", tags=["sources"])


def detect_lan_http_warning(base_url: str) -> LanWarningResponse | None:
    """Rileva se l'URL base corrente usa HTTP non sicuro con un indirizzo IP privato non supportato da Google."""
    parsed = urlsplit(base_url)
    hostname = parsed.hostname or ""
    if parsed.scheme == "http" and hostname not in {"localhost", "127.0.0.1", "::1"}:
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private:
                return LanWarningResponse(
                    is_private_ip=True,
                    warning_code="private_ip_http_unsupported_by_google",
                    message=(
                        f"L'indirizzo host rilevato ({hostname}) è un IP privato numerico non accettato "
                        "da Google Cloud Console per applicazioni Web OAuth 2.0 con protocollo HTTP. "
                        "Google richiede un hostname o localhost. Soluzioni raccomandate: "
                        "1) Assegnare un hostname locale nel file hosts (es. gbrain.local) e impostare GBRAIN_PUBLIC_URL; "
                        "2) Usare un reverse proxy locale con TLS (HTTPS)."
                    ),
                )
        except ValueError:
            pass
    return None


@sources_router.get("/catalog", response_model=SourceCatalogResponse)
async def list_source_catalog(principal: PrincipalDep) -> SourceCatalogResponse:
    """Restituisce il catalogo unificato ed estensibile di tutte le fonti gestite da GBrain."""
    del principal
    catalog = get_source_catalog()
    items: list[SourceCatalogItemResponse] = []
    for desc in catalog:
        sync_caps = None
        if desc.sync_capabilities is not None:
            sync_caps = ConnectorCapabilitiesResponse(
                source_kind=desc.sync_capabilities.source_kind,
                read_only=desc.sync_capabilities.read_only,
                pagination=desc.sync_capabilities.pagination,
                incremental=desc.sync_capabilities.incremental,
                reconciliation=desc.sync_capabilities.reconciliation,
                max_page_size=desc.sync_capabilities.max_page_size,
                concurrency=desc.sync_capabilities.concurrency,
                operations=list(desc.sync_capabilities.operations),
            )
        import_policy = None
        if desc.import_policy is not None:
            import_policy = ImportPolicyDescriptorResponse(
                supports_date_range=desc.import_policy.supports_date_range,
                default_window_days=desc.import_policy.default_window_days,
                max_items_limit=desc.import_policy.max_items_limit,
                default_item_limit=desc.import_policy.default_item_limit,
                max_request_budget=desc.import_policy.max_request_budget,
                default_request_budget=desc.import_policy.default_request_budget,
                label_restriction=desc.import_policy.label_restriction,
            )
        fields = [
            SourceFieldDescriptorResponse(
                name=f.name,
                label=f.label,
                field_type=f.field_type,
                required=f.required,
                placeholder=f.placeholder,
                description=f.description,
                help_text=f.help_text,
            )
            for f in desc.config_fields
        ]
        items.append(
            SourceCatalogItemResponse(
                id=desc.id,
                name=desc.name,
                category=desc.category.value,
                description=desc.description,
                icon=desc.icon,
                auth_method=desc.auth_method.value,
                status=desc.status.value,
                availability=desc.status.value,
                test_available=desc.test_available,
                sync_capabilities=sync_caps,
                config_fields=fields,
                import_policy=import_policy,
                documentation_url=desc.documentation_url,
            )
        )
    return SourceCatalogResponse(items=items, total=len(items))


@sources_router.get("/{source_id}/status", response_model=SourceStatusResponse)
async def get_source_status(
    source_id: str,
    request: Request,
    session: DbSession,
    settings: AppSettings,
    principal: PrincipalDep,
) -> SourceStatusResponse:
    """Restituisce lo stato operativo della fonte, account collegati, redirect URI calcolato e avvisi LAN."""
    descriptor = get_source_descriptor(source_id)
    if descriptor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Sorgente '{source_id}' non trovata nel catalogo",
        )

    request_base_url = str(request.base_url).rstrip("/")
    base_url = settings.public_url.rstrip("/") if settings.public_url else request_base_url
    detected_callback = f"{base_url}/api/sources/{source_id}/oauth/callback"
    # Il warning LAN viene verificato prioritariamente sull'effettivo request_base_url (l'URL reale di accesso dell'utente)
    # oppure sul public_url configurato
    lan_warning = detect_lan_http_warning(request_base_url) or detect_lan_http_warning(base_url)

    if source_id == "gmail":
        configured, auth_source, client_id_preview = await is_gmail_configured(
            session, settings, principal.owner.id
        )
        account = await get_gmail_account(session, principal.owner.id, required=False)
        account_connected = bool(
            account is not None
            and account.status == ConnectorAccountStatus.ACTIVE
            and account.oauth_token_ciphertext
        )
        primary_email = account.primary_email if account else None
        external_account_id = account.external_account_id if account else None

        if account_connected and account:
            st = SourceOperationalStatus.CONNECTED.value
        elif account and account.status in {
            ConnectorAccountStatus.REVOKED,
            ConnectorAccountStatus.DISABLED,
            ConnectorAccountStatus.REAUTH_REQUIRED,
        }:
            st = (
                SourceOperationalStatus.ERROR.value
                if account.status == ConnectorAccountStatus.REAUTH_REQUIRED
                else SourceOperationalStatus.SUSPENDED.value
            )
        elif configured:
            st = SourceOperationalStatus.CREDENTIALS_CONFIGURED.value
        else:
            st = SourceOperationalStatus.NOT_CONFIGURED.value

        aliases_ready = bool(account and account.confirmed_aliases)
        confirmed_aliases = list(account.confirmed_aliases or []) if account else []

        initial_import_completed = False
        has_checkpoint = False
        checkpoint_history_id = None
        checkpoint_page_token = None
        if account:
            from gbrain.domain.models import GmailCheckpoint
            from gbrain.infrastructure.gmail_repository import checkpoint_from_model

            checkpoint_model = None
            if hasattr(session, "get"):
                try:
                    checkpoint_model = await session.get(GmailCheckpoint, account.id)
                except Exception:
                    checkpoint_model = None
            if checkpoint_model is None:
                try:
                    checkpoint_model = await session.scalar(
                        select(GmailCheckpoint).where(
                            GmailCheckpoint.connector_account_id == account.id
                        )
                    )
                except Exception:
                    checkpoint_model = None

            if checkpoint_model is not None:
                has_checkpoint = True
                checkpoint_history_id = checkpoint_model.history_id
                checkpoint_page_token = checkpoint_model.page_token
                initial_import_completed = checkpoint_from_model(
                    checkpoint_model
                ).is_initial_complete

        sync_config_resp: SourceSyncConfigResponse | None = None
        coverage_resp: GmailCoverageResponse | None = None
        backfill_resp: GmailBackfillStateResponse | None = None
        requires_import = False
        last_sync_at: datetime | None = None

        if account:
            repo = AsyncPostgresGmailRepository(session)
            cfg = await get_or_create_sync_config(session, account)
            sync_config_resp = SourceSyncConfigResponse(
                id=cfg.id,
                revision=cfg.revision,
                history_after=cfg.history_after,
                history_before=cfg.history_before,
                selection_label=cfg.selection_label,
                query_filter=cfg.query_filter,
                item_limit=cfg.item_limit,
                request_budget=cfg.request_budget,
                batch_size=cfg.batch_size,
                update_policy=cfg.update_policy,
                query_fingerprint=cfg.query_fingerprint,
                created_at=cfg.created_at,
                updated_at=cfg.updated_at,
            )

            cov = await repo.load_coverage(account.id)
            if cov:
                coverage_resp = GmailCoverageResponse(
                    covered_after=cov.covered_after,
                    covered_before=cov.covered_before,
                    query_fingerprint=cov.query_fingerprint,
                    config_revision=cov.config_revision,
                    is_legacy=cov.is_legacy,
                    has_gaps=cov.has_gaps,
                    missing_count=cov.missing_count,
                    status=cov.status,
                    last_completed_at=cov.last_completed_at,
                )
                if (
                    cov.config_revision != cfg.revision
                    or cov.query_fingerprint != cfg.query_fingerprint
                    or cov.covered_after != cfg.history_after
                    or cov.covered_before != cfg.history_before
                    or cov.is_legacy
                ):
                    requires_import = True
            else:
                requires_import = True

            bf = await repo.load_backfill_state(account.id)
            if bf:
                backfill_resp = GmailBackfillStateResponse(
                    config_revision=bf.config_revision,
                    query_fingerprint=bf.query_fingerprint,
                    history_after=bf.history_after,
                    history_before=bf.history_before,
                    page_token=bf.page_token,
                    status=bf.status,
                    items_imported=bf.items_imported,
                    items_not_imported=bf.items_not_imported,
                    last_run_at=bf.last_run_at,
                    last_error_code=bf.last_error_code,
                )

            if checkpoint_model and checkpoint_model.last_sync_at:
                last_sync_at = checkpoint_model.last_sync_at

        latest_job_orm = None
        latest_job_resp = None
        active_job_resp = None
        active_job_matches_config = False
        if account is not None:
            latest_job_orm = await session.scalar(
                select(ImportJob)
                .where(ImportJob.connector_account_id == account.id)
                .order_by(sa_desc(ImportJob.created_at))
                .limit(1)
            )
            if latest_job_orm is not None:
                runtime = await build_import_job_runtime(session, latest_job_orm)
                latest_job_resp = ImportJobResponse.from_job_and_runtime(latest_job_orm, runtime)
                if latest_job_orm.status in {ImportJobStatus.PENDING, ImportJobStatus.RUNNING}:
                    active_job_resp = latest_job_resp
                    snapshot = latest_job_orm.policy_snapshot or {}
                    # La selezione effettiva può materializzare un upper bound anche
                    # quando la configurazione è open-ended. Per stabilire se il job
                    # appartiene alla configurazione corrente si usa esclusivamente
                    # l'identità configurata catturata all'enqueue.
                    config_fingerprint = snapshot.get("config_query_fingerprint")
                    if config_fingerprint is None:
                        # Compatibilità esplicita con job legacy, nei quali
                        # ImportJob.query_fingerprint era la fingerprint configurata.
                        config_fingerprint = latest_job_orm.query_fingerprint
                    active_job_matches_config = bool(
                        sync_config_resp
                        and latest_job_orm.config_revision == sync_config_resp.revision
                        and config_fingerprint == sync_config_resp.query_fingerprint
                    )

        selection_matches_config = True
        period_matches_config = True
        coverage_matches_config = True
        coverage_period_matches_config = True
        selection_complete = False
        incomplete_reason = None

        if sync_config_resp and backfill_resp:
            selection_matches_config = bool(
                backfill_resp.config_revision == sync_config_resp.revision
                and backfill_resp.query_fingerprint == sync_config_resp.query_fingerprint
            )
            period_matches_config = bool(
                backfill_resp.history_after == sync_config_resp.history_after
                and backfill_resp.history_before == sync_config_resp.history_before
            )
            selection_complete = bool(
                selection_matches_config
                and period_matches_config
                and backfill_resp.status == "completed"
                and not backfill_resp.page_token
            )
            if not selection_matches_config:
                incomplete_reason = (
                    "Configurazione modificata: la scansione mostrata non è quella corrente"
                )
            elif not period_matches_config:
                incomplete_reason = "Periodo importato diverso dal periodo desiderato"
            elif not selection_complete:
                if backfill_resp.status == "running":
                    incomplete_reason = "Importazione in corso..."
                elif backfill_resp.status == "partial":
                    incomplete_reason = (
                        "Scansione parziale (token di continuazione presente o limite raggiunto)"
                    )
                elif backfill_resp.status == "failed":
                    incomplete_reason = (
                        f"Scansione fallita ({backfill_resp.last_error_code or 'errore'})"
                    )
        elif sync_config_resp and not backfill_resp:
            selection_complete = False
            incomplete_reason = "Scansione iniziale non ancora eseguita"

        if sync_config_resp and coverage_resp:
            coverage_matches_config = bool(
                coverage_resp.config_revision == sync_config_resp.revision
                and coverage_resp.query_fingerprint == sync_config_resp.query_fingerprint
                and not coverage_resp.is_legacy
            )
            coverage_period_matches_config = bool(
                coverage_resp.covered_after == sync_config_resp.history_after
                and coverage_resp.covered_before == sync_config_resp.history_before
            )
        elif sync_config_resp and not coverage_resp:
            coverage_matches_config = False

        return SourceStatusResponse(
            source_id=source_id,
            availability=descriptor.status.value,
            configured=configured,
            auth_source=auth_source,
            client_id_preview=client_id_preview,
            account_connected=account_connected,
            external_account_id=external_account_id,
            primary_email=primary_email,
            aliases_ready=aliases_ready,
            confirmed_aliases=confirmed_aliases,
            status=st,
            detected_callback_url=detected_callback,
            lan_warning=lan_warning,
            initial_import_completed=initial_import_completed,
            has_checkpoint=has_checkpoint,
            checkpoint_history_id=checkpoint_history_id,
            checkpoint_page_token=checkpoint_page_token,
            sync_config=sync_config_resp,
            coverage=coverage_resp,
            backfill_state=backfill_resp,
            has_gaps=coverage_resp.has_gaps if coverage_resp else False,
            missing_count=coverage_resp.missing_count if coverage_resp else 0,
            requires_import=requires_import,
            last_sync_at=last_sync_at,
            selection_matches_config=selection_matches_config,
            period_matches_config=period_matches_config,
            coverage_matches_config=coverage_matches_config,
            coverage_period_matches_config=coverage_period_matches_config,
            selection_complete=selection_complete,
            incomplete_reason=incomplete_reason,
            latest_job=latest_job_resp,
            active_job=active_job_resp,
            active_job_matches_config=active_job_matches_config,
        )

    if source_id == "immich":
        from gbrain.infrastructure.immich_repository import AsyncPostgresImmichRepository

        cred = await SourceCredentialsRepository.get_credentials(
            session, principal.owner.id, "immich"
        )
        configured = cred is not None and cred.is_active
        raw_payload = (
            await SourceCredentialsRepository.get_decrypted_payload(
                session, settings.source_credentials_encryption_key, principal.owner.id, "immich"
            )
            if configured
            else None
        )
        immich_base_url = raw_payload.get("base_url") if raw_payload else None
        lan_warn = detect_lan_http_warning(immich_base_url) if immich_base_url else lan_warning

        account = await session.scalar(
            select(ConnectorAccount).where(
                ConnectorAccount.owner_id == principal.owner.id,
                ConnectorAccount.connector_kind == "immich",
            )
        )
        account_connected = account is not None and account.status == ConnectorAccountStatus.ACTIVE

        latest_job_resp = None
        active_job_resp = None
        if account is not None:
            latest_job_orm = await session.scalar(
                select(ImportJob)
                .where(
                    ImportJob.source_kind == "immich",
                    ImportJob.connector_account_id == account.id,
                )
                .order_by(sa_desc(ImportJob.created_at))
                .limit(1)
            )
            if latest_job_orm is not None:
                runtime = await build_import_job_runtime(session, latest_job_orm)
                latest_job_resp = ImportJobResponse.from_job_and_runtime(latest_job_orm, runtime)
                if latest_job_orm.status in {ImportJobStatus.PENDING, ImportJobStatus.RUNNING}:
                    active_job_resp = latest_job_resp

        immich_repo = AsyncPostgresImmichRepository(session)
        total_assets = await immich_repo.count_imported_assets(principal.owner.id)

        st = (
            SourceOperationalStatus.CONNECTED.value
            if account_connected
            else (
                SourceOperationalStatus.CREDENTIALS_CONFIGURED.value
                if configured
                else SourceOperationalStatus.NOT_CONFIGURED.value
            )
        )

        return SourceStatusResponse(
            source_id=source_id,
            availability=descriptor.status.value,
            configured=configured,
            auth_source="db_encrypted" if configured else None,
            client_id_preview=immich_base_url,
            account_connected=account_connected,
            external_account_id=account.external_account_id if account else None,
            primary_email=account.primary_email if account else None,
            status=st,
            detected_callback_url=detected_callback,
            lan_warning=lan_warn,
            initial_import_completed=total_assets > 0,
            has_checkpoint=latest_job_resp is not None,
            selection_complete=total_assets > 0,
            latest_job=latest_job_resp,
            active_job=active_job_resp,
        )

    # Per connettori coming_soon
    return SourceStatusResponse(
        source_id=source_id,
        availability=descriptor.status.value,
        configured=False,
        auth_source=None,
        client_id_preview=None,
        account_connected=False,
        external_account_id=None,
        primary_email=None,
        status=SourceOperationalStatus.NOT_CONFIGURED.value,
        detected_callback_url=detected_callback,
        lan_warning=lan_warning,
    )


@sources_router.post("/{source_id}/credentials", response_model=SourceCredentialsSaveResponse)
async def configure_source_credentials(
    source_id: str,
    payload: SourceCredentialsUploadRequest,
    session: DbSession,
    settings: AppSettings,
    principal: CsrfPrincipalDep,
) -> SourceCredentialsSaveResponse:
    """Configura o aggiorna le credenziali client della fonte crittografandole con AES-256-GCM."""
    if source_id == "immich":
        from gbrain.application.crypto import encrypt_payload
        from gbrain.infrastructure.immich_gateway import validate_immich_url

        base_url = payload.base_url
        api_key = payload.api_key
        if payload.raw_json and isinstance(payload.raw_json, dict):
            base_url = base_url or payload.raw_json.get("base_url")
            api_key = api_key or payload.raw_json.get("api_key")

        if not base_url or not api_key:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Specificare sia 'base_url' (URL dell'istanza) sia 'api_key' per Immich.",
            )

        try:
            clean_url, _ = validate_immich_url(base_url)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"URL istanza Immich non valido: {exc}",
            ) from exc

        creds_dict = {"base_url": clean_url, "api_key": api_key.strip()}
        ciphertext = encrypt_payload(
            json.dumps(creds_dict).encode("utf-8"),
            settings.source_credentials_encryption_key,
        )

        await SourceCredentialsRepository.save_credentials(
            session,
            principal.owner.id,
            "immich",
            auth_method="api_key",
            client_type="api_token",
            client_id=clean_url,
            encrypted_credentials=ciphertext,
        )

        return SourceCredentialsSaveResponse(
            source_id="immich",
            client_id=clean_url,
            client_type="api_token",
            configured_redirect_uri=None,
            message="Credenziali Immich configurate con successo e cifrate con AES-256-GCM.",
        )

    if source_id != "gmail":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"La configurazione delle credenziali client per '{source_id}' non è ancora implementata.",
        )

    raw_config: dict[str, Any] = {}
    if payload.raw_json is not None:
        if isinstance(payload.raw_json, str):
            if len(payload.raw_json.encode("utf-8")) > 1_048_576:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="Il file di configurazione fornito supera la dimensione massima consentita di 1MB.",
                )
            try:
                raw_config = json.loads(payload.raw_json)
            except json.JSONDecodeError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Il JSON delle credenziali fornito non è valido.",
                ) from exc
        elif isinstance(payload.raw_json, dict):
            raw_config = payload.raw_json
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Formato di raw_json non valido.",
            )
        if not isinstance(raw_config, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Il JSON delle credenziali fornito non è valido.",
            )
    elif payload.client_id and payload.client_secret:
        raw_config = {
            "client_id": payload.client_id.strip(),
            "client_secret": payload.client_secret.strip(),
        }
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Fornire il file JSON scaricato da Google Cloud Console oppure inserire client_id e client_secret.",
        )

    try:
        normalized = normalize_google_client_config(
            raw_config, redirect_uri=payload.redirect_uri_override
        )
    except GmailOAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=exc.safe_message,
        ) from exc

    client_type = next(iter(normalized))
    client_entry = normalized[client_type]
    client_id = str(client_entry["client_id"])
    redirect_uri = (
        payload.redirect_uri_override
        or (client_entry.get("redirect_uris") and client_entry["redirect_uris"][0])
        or None
    )

    encrypted = encrypt_json(normalized, settings.source_credentials_encryption_key)

    await SourceCredentialsRepository.save_credentials(
        session,
        owner_id=principal.owner.id,
        source_kind=source_id,
        auth_method="oauth2",
        client_type=client_type,
        client_id=client_id,
        encrypted_credentials=encrypted,
        configured_redirect_uri=redirect_uri,
        is_active=True,
    )

    masked_id = f"{client_id[:12]}...{client_id[-8:]}" if len(client_id) > 16 else client_id
    return SourceCredentialsSaveResponse(
        source_id=source_id,
        client_id=masked_id,
        client_type=client_type,
        configured_redirect_uri=redirect_uri,
        message="Credenziali client salvate con successo e cifrate a riposo con AES-256-GCM.",
    )


@sources_router.post("/{source_id}/test-connection", response_model=SourceTestConnectionResponse)
async def test_source_connection(
    source_id: str,
    session: DbSession,
    settings: AppSettings,
    principal: CsrfPrincipalDep,
) -> SourceTestConnectionResponse:
    """Verifica live dello stato di connettività verso le API remote del provider della fonte."""
    if source_id == "immich":
        from gbrain.infrastructure.immich_gateway import ImmichGateway

        cred_payload = await SourceCredentialsRepository.get_decrypted_payload(
            session, settings.source_credentials_encryption_key, principal.owner.id, "immich"
        )
        if not cred_payload or not cred_payload.get("base_url") or not cred_payload.get("api_key"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Credenziali Immich non configurate. Salva prima l'URL e l'API key.",
            )

        start_t = datetime.now(UTC)
        version: str | None = None
        try:
            gw = ImmichGateway(cred_payload["base_url"], cred_payload["api_key"])
            user = await gw.get_current_user()
            version = await gw.get_server_version()
            capabilities = await gw.inspect_capabilities(server_version=version)
            latency = (datetime.now(UTC) - start_t).total_seconds() * 1000.0

            # Crea o aggiorna ConnectorAccount attivo
            account = await session.scalar(
                select(ConnectorAccount).where(
                    ConnectorAccount.owner_id == principal.owner.id,
                    ConnectorAccount.connector_kind == "immich",
                )
            )
            ext_id = f"immich_{user['id']}"
            primary_email = user.get("email") or f"{user.get('name', 'user')}@immich.local"
            if account is None:
                account = ConnectorAccount(
                    id=uuid.uuid4(),
                    owner_id=principal.owner.id,
                    connector_kind="immich",
                    external_account_id=ext_id,
                    primary_email=primary_email,
                    status=ConnectorAccountStatus.ACTIVE,
                    authorized_at=datetime.now(UTC),
                    last_connection_test_at=datetime.now(UTC),
                )
                session.add(account)
            else:
                account.status = ConnectorAccountStatus.ACTIVE
                account.external_account_id = ext_id
                account.primary_email = primary_email
                account.last_connection_test_at = datetime.now(UTC)
            await session.commit()

            return SourceTestConnectionResponse(
                source_id="immich",
                connected=True,
                latency_ms=latency,
                primary_identity=user.get("email") or user.get("name"),
                aliases=[],
                message=f"Connessione a Immich v{version} verificata con successo ({user.get('name')})",
                server_version=version,
                version_supported=True,
                capabilities={
                    "albums": capabilities.supports_albums,
                    "search": capabilities.supports_search,
                    "people": capabilities.supports_people,
                },
            )
        except ImmichGatewayError as exc:
            latency = (datetime.now(UTC) - start_t).total_seconds() * 1000.0
            known_version = exc.server_version or version
            return SourceTestConnectionResponse(
                source_id="immich",
                connected=False,
                latency_ms=latency,
                primary_identity=None,
                aliases=[],
                message=exc.message,
                server_version=known_version,
                version_supported=(
                    False if exc.category == ImmichErrorCategory.UNSUPPORTED_VERSION else None
                ),
                error_category=exc.category.value,
                error_code=exc.reason_code,
                failure_phase=exc.phase,
            )
        except Exception:
            logger.exception("Test connessione Immich fallito per errore interno")
            latency = (datetime.now(UTC) - start_t).total_seconds() * 1000.0
            return SourceTestConnectionResponse(
                source_id="immich",
                connected=False,
                latency_ms=latency,
                primary_identity=None,
                aliases=[],
                message="Test di connessione Immich fallito per un errore interno",
                server_version=version,
                error_category="internal",
                error_code="unexpected_error",
                failure_phase="application",
            )

    if source_id != "gmail":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Test di connessione non disponibile per la sorgente '{source_id}'.",
        )

    account = await get_gmail_account(session, principal.owner.id, required=False)
    if (
        account is None
        or account.status != ConnectorAccountStatus.ACTIVE
        or not account.oauth_token_ciphertext
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nessun account Gmail attualmente connesso o autorizzato per questo utente.",
        )

    try:
        credentials = decrypt_credentials(account, settings)
    except GmailOAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Errore nella decifratura delle credenziali: {exc.safe_message}",
        ) from exc

    start_time = time.perf_counter()
    try:
        if not credentials.valid and credentials.refresh_token:
            await asyncio.to_thread(refresh_credentials, credentials, settings)
            account.oauth_token_ciphertext = encrypt_credentials(credentials, settings)
            await session.commit()

        initial_token = credentials.token
        rate_limiter = PostgresGmailRateLimiter(
            settings.database_url,
            settings.gmail_rate_limit_per_second,
        )
        gateway = OfficialGmailGateway.from_credentials(
            credentials,
            account_id=account.id,
            rate_limiter=rate_limiter,
        )
        profile = await asyncio.to_thread(
            gateway.get_profile, RequestBudget(max_requests=1, max_items=1)
        )
        if credentials.token != initial_token:
            account.oauth_token_ciphertext = encrypt_credentials(credentials, settings)
            await session.commit()

        latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
        email_address = str(profile.get("emailAddress") or account.primary_email or "")
        return SourceTestConnectionResponse(
            source_id=source_id,
            connected=True,
            latency_ms=latency_ms,
            primary_identity=email_address,
            aliases=list(account.confirmed_aliases or [email_address]),
            message="Connessione alle API Gmail verificata con successo.",
        )
    except Exception as exc:
        latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
        logger.warning("Test di connessione Gmail fallito: %s", exc)
        return SourceTestConnectionResponse(
            source_id=source_id,
            connected=False,
            latency_ms=latency_ms,
            primary_identity=account.primary_email,
            aliases=[],
            message=f"Verifica connessione non riuscita: {exc}",
        )


@sources_router.get("/{source_id}/preview", response_model=SourcePreviewResponse)
async def preview_source_import(
    source_id: str,
    session: DbSession,
    settings: AppSettings,
    principal: PrincipalDep,
    period_after: Annotated[datetime | None, Query()] = None,
    period_before: Annotated[datetime | None, Query()] = None,
    album_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=25)] = 10,
) -> SourcePreviewResponse:
    """Restituisce un'anteprima read-only dei messaggi inviati senza persistere nulla a database."""
    if source_id == "immich":
        from gbrain.infrastructure.immich_gateway import ImmichGateway

        cred_payload = await SourceCredentialsRepository.get_decrypted_payload(
            session, settings.source_credentials_encryption_key, principal.owner.id, "immich"
        )
        if not cred_payload or not cred_payload.get("base_url") or not cred_payload.get("api_key"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Credenziali Immich non configurate.",
            )

        try:
            gw = ImmichGateway(cred_payload["base_url"], cred_payload["api_key"])
            bounded_limit = min(limit, 25)
            if album_id and (period_after is not None or period_before is not None):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Selezionare un album oppure un periodo, non entrambi.",
                )
            if album_id:
                _, assets = await gw.fetch_album_assets(album_id, limit=bounded_limit)
                after_dt = None
                before_dt = None
            else:
                before_dt = period_before or datetime.now(UTC)
                after_dt = period_after or (before_dt - timedelta(days=30))
                if after_dt >= before_dt:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="Il limite inferiore UTC deve precedere quello superiore (intervallo [after, before)).",
                    )
                assets, _ = await gw.search_period_assets(after_dt, before_dt, limit=bounded_limit)

            samples = [
                SourcePreviewSampleItem(
                    id=a.external_id,
                    date=a.occurred_at,
                    subject=a.description or f"Asset ({a.asset_type})",
                    to=[p.name for p in a.people],
                    media_type=a.asset_type.value,
                    description=a.description,
                    albums=[al.name for al in a.albums],
                )
                for a in assets
            ]
            return SourcePreviewResponse(
                source_id="immich",
                estimated_count=len(assets),
                period_after=after_dt,
                period_before=before_dt,
                sample_items=samples,
                query=(
                    f"immich_album({album_id}, limit={bounded_limit})"
                    if album_id
                    else f"immich_period_search(limit={bounded_limit})"
                ),
                note="Anteprima read-only di metadati Immich (max 25 asset; nessuna persistenza)",
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Errore durante l'anteprima Immich: {exc}",
            ) from exc

    if source_id != "gmail":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Anteprima di importazione non disponibile per la sorgente '{source_id}'.",
        )

    account = await get_gmail_account(session, principal.owner.id, required=False)
    if (
        account is None
        or account.status != ConnectorAccountStatus.ACTIVE
        or not account.oauth_token_ciphertext
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Account Gmail non connesso o autorizzazione non attiva.",
        )

    before_dt = period_before or datetime.now(UTC)
    after_dt = period_after or (before_dt - timedelta(days=30))
    if after_dt >= before_dt:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La data iniziale (period_after) deve essere precedente alla data finale (period_before).",
        )

    credentials = decrypt_credentials(account, settings)
    if not credentials.valid and credentials.refresh_token:
        try:
            await asyncio.to_thread(refresh_credentials, credentials, settings)
            account.oauth_token_ciphertext = encrypt_credentials(credentials, settings)
            await session.commit()
        except Exception as exc:
            logger.warning("Refresh credenziali preview fallito: %s", exc)

    initial_token = credentials.token
    rate_limiter = PostgresGmailRateLimiter(
        settings.database_url,
        settings.gmail_rate_limit_per_second,
    )
    gateway = OfficialGmailGateway.from_credentials(
        credentials,
        account_id=account.id,
        rate_limiter=rate_limiter,
    )
    after_epoch = int(after_dt.timestamp())
    before_epoch = int(before_dt.timestamp())
    query = f"label:SENT after:{after_epoch} before:{before_epoch}"

    try:
        from gbrain.domain.gmail import GmailSelection

        list_budget = RequestBudget(max_requests=2, max_items=limit)
        selection = GmailSelection(
            after=after_dt,
            before=before_dt,
            limit=limit,
            page_size=limit,
        )
        page = await asyncio.to_thread(
            gateway.list_sent_page,
            selection=selection,
            cursor=None,
            budget=list_budget,
        )

        sample_items: list[SourcePreviewSampleItem] = []
        for loc in page.items[:limit]:
            msg_id = loc.message_id
            try:
                fetch_budget = RequestBudget(max_requests=1, max_items=1)
                raw_item = await asyncio.to_thread(gateway.fetch_sent_message, loc, fetch_budget)
                headers_list = raw_item.payload.get("headers") or raw_item.payload.get(
                    "payload", {}
                ).get("headers", [])
                headers = {h.get("name", "").lower(): h.get("value", "") for h in headers_list}
                subject = headers.get("subject")
                to_hdr = headers.get("to", "")
                to_list = [addr.strip() for addr in to_hdr.split(",") if addr.strip()]
                msg_date = None
                if raw_item.internal_date_ms is not None:
                    msg_date = datetime.fromtimestamp(raw_item.internal_date_ms / 1000.0, tz=UTC)
                else:
                    internal_date_str = raw_item.payload.get("internalDate")
                    if internal_date_str and str(internal_date_str).isdigit():
                        msg_date = datetime.fromtimestamp(int(internal_date_str) / 1000.0, tz=UTC)
                sample_items.append(
                    SourcePreviewSampleItem(
                        id=msg_id,
                        date=msg_date,
                        subject=subject,
                        to=to_list,
                        snippet=raw_item.payload.get("snippet"),
                    )
                )
            except Exception as item_exc:
                logger.debug(
                    "Impossibile caricare dettaglio messaggio preview %s: %s", msg_id, item_exc
                )
                sample_items.append(
                    SourcePreviewSampleItem(
                        id=msg_id,
                        snippet="Anteprima non disponibile",
                    )
                )

        if credentials.token != initial_token:
            account.oauth_token_ciphertext = encrypt_credentials(credentials, settings)
            await session.commit()

        estimated_count = len(sample_items)
        return SourcePreviewResponse(
            source_id=source_id,
            estimated_count=estimated_count,
            period_after=after_dt,
            period_before=before_dt,
            sample_items=sample_items,
            query=query,
            note="Anteprima di sola lettura per posta inviata (SENT)",
        )
    except Exception as exc:
        logger.warning("Errore durante l'anteprima Gmail: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Errore durante l'interrogazione delle API Google: {exc}",
        ) from exc


@sources_router.post("/{source_id}/sync", response_model=SourceSyncResponse)
async def trigger_source_sync(
    source_id: str,
    body: SourceSyncRequest,
    session: DbSession,
    settings: AppSettings,
    procrastinate: ProcrastinateApp,
    principal: CsrfPrincipalDep,
) -> SourceSyncResponse:
    """Accoda un job Procrastinate asincrono di importazione o sincronizzazione della fonte."""
    if source_id == "immich":
        account = await session.scalar(
            select(ConnectorAccount).where(
                ConnectorAccount.owner_id == principal.owner.id,
                ConnectorAccount.connector_kind == "immich",
                ConnectorAccount.status == ConnectorAccountStatus.ACTIVE,
            )
        )
        if account is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Account Immich non connesso o non attivo. Esegui prima il test di connessione.",
            )

        enrichment = body.mode == "enrichment"
        selection = (
            "all" if enrichment else body.selection or ("album" if body.album_id else "range")
        )
        item_limit = None if (enrichment and body.item_limit is None) else body.item_limit
        request_budget = (
            body.request_budget if (not enrichment or body.request_budget is not None) else None
        )
        job_mode = "enrichment" if enrichment else "full"

        initial_checkpoint: dict[str, Any] = {
            "phase": "queued",
            "cursor": None,
            "album_offset": 0,
            "total_enumerated": 0,
            "total_inserted": 0,
            "total_updated": 0,
            "total_unchanged": 0,
            "total_duplicates": 0,
            "total_invalid": 0,
            "total_missing": 0,
            "missing_evaluation": "not_evaluated",
            "pages_completed": 0,
            "requests_completed": 0,
            "item_limit": item_limit,
            "request_budget": request_budget,
            "batch_size": 25,
        }

        if body.resume_from_job_id is not None:
            prev_job = await session.get(ImportJob, body.resume_from_job_id)
            if (
                prev_job is not None
                and prev_job.connector_account_id == account.id
                and prev_job.source_kind == "immich"
                and isinstance(prev_job.checkpoint, dict)
            ):
                cp = prev_job.checkpoint
                initial_checkpoint.update(
                    {
                        "phase": "resuming",
                        "cursor": cp.get("cursor"),
                        "album_offset": int(cp["album_offset"])
                        if cp.get("album_offset") is not None
                        else 0,
                        "total_enumerated": int(cp["total_enumerated"])
                        if cp.get("total_enumerated") is not None
                        else 0,
                        "total_inserted": int(cp["total_inserted"])
                        if cp.get("total_inserted") is not None
                        else 0,
                        "total_updated": int(cp["total_updated"])
                        if cp.get("total_updated") is not None
                        else 0,
                        "total_unchanged": int(cp["total_unchanged"])
                        if cp.get("total_unchanged") is not None
                        else (
                            int(cp["total_duplicates"])
                            if cp.get("total_duplicates") is not None
                            else 0
                        ),
                        "total_duplicates": int(cp["total_duplicates"])
                        if cp.get("total_duplicates") is not None
                        else 0,
                        "total_invalid": int(cp["total_invalid"])
                        if cp.get("total_invalid") is not None
                        else 0,
                        "high_water_updated_at": cp.get("high_water_updated_at"),
                    }
                )

        immich_job = ImportJob(
            id=uuid.uuid4(),
            source_kind="immich",
            connector_account_id=account.id,
            mode=job_mode,
            status=ImportJobStatus.PENDING,
            checkpoint=initial_checkpoint,
            policy_snapshot={
                "selection": selection,
                "album_id": body.album_id if selection == "album" else None,
                "after": body.period_after.isoformat()
                if body.period_after and not enrichment
                else None,
                "before": body.period_before.isoformat()
                if body.period_before and not enrichment
                else None,
                "import_all": selection == "all",
                "item_limit": item_limit,
                "request_budget": request_budget,
                "pipeline_version": "immich-meta-v2",
                "gps_policy": "withhold_exact",
                "batch_size": 25,
                "concurrency": 1,
            },
        )
        session.add(immich_job)
        await session.commit()
        await session.refresh(immich_job)

        queue_job_id = None
        try:
            queue_job_id = await procrastinate.configure_task(
                IMMICH_QUEUE_IDENTITY.task_name,
                queue=IMMICH_QUEUE_IDENTITY.queue_name,
                lock=f"immich-sync-{account.id}",
            ).defer_async(
                job_id=str(immich_job.id),
                account_id=str(account.id),
                mode=job_mode,
            )
            immich_job.queue_job_id = queue_job_id
            await session.commit()
        except Exception as exc:
            logger.warning("Accodamento Procrastinate Immich non riuscito: %s", exc)

        limit_desc = (
            "senza limite asset"
            if item_limit is None
            else f"con limite totale di {item_limit} asset"
        )
        message = (
            f"Job di analisi completa Immich (enrichment) avviato {limit_desc}."
            if enrichment
            else f"Job di sincronizzazione Immich avviato con limite totale di {item_limit} asset."
        )

        return SourceSyncResponse(
            source_id="immich",
            job_id=immich_job.id,
            queue_job_id=queue_job_id,
            status="pending",
            mode=job_mode,
            message=message,
        )

    if source_id != "gmail":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Sincronizzazione non disponibile per la sorgente '{source_id}'.",
        )

    account = await get_gmail_account(session, principal.owner.id, required=False)
    if (
        account is None
        or account.status != ConnectorAccountStatus.ACTIVE
        or not account.oauth_token_ciphertext
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Account Gmail non connesso o non attivo.",
        )

    async def enqueue(*, job_id: str, account_id: str, mode: str) -> int:
        return await procrastinate.configure_task(
            GMAIL_QUEUE_IDENTITY.task_name,
            queue=GMAIL_QUEUE_IDENTITY.queue_name,
            lock="gmail-heavy-global",
        ).defer_async(job_id=job_id, account_id=account_id, mode=mode)

    # Se la richiesta è incrementale o priva di date fisse, usiamo l'aggiornamento continuo backend-authoritative
    if body.mode == "incremental" or (body.period_after is None and body.period_before is None):
        try:
            job = await enqueue_continuous_update_job(session, account, enqueue)
        except GmailServiceError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=exc.safe_message,
            ) from exc
        return SourceSyncResponse(
            source_id=source_id,
            job_id=str(job.id),
            queue_job_id=job.queue_job_id,
            status=job.status.value,
            mode=job.mode or "incremental",
            message=f"Job di sincronizzazione Gmail avviato in modalità {job.mode}.",
        )

    if body.item_limit is None or body.request_budget is None or body.mode == "enrichment":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Gmail richiede item_limit e request_budget numerici e non supporta enrichment.",
        )

    before_dt = body.period_before or datetime.now(UTC)
    after_dt = body.period_after or (before_dt - timedelta(days=30))
    if after_dt >= before_dt:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La data iniziale (period_after) deve essere precedente alla data finale (period_before).",
        )

    policy_data = PolicyInput(
        after=after_dt,
        before=before_dt,
        item_limit=body.item_limit,
        request_budget=body.request_budget,
        batch_size=body.batch_size,
    )
    policy = await save_policy(session, account, policy_data)

    try:
        job = await create_and_enqueue_job(
            session,
            account,
            policy,
            body.mode,
            enqueue,
        )
    except GmailServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.safe_message,
        ) from exc

    return SourceSyncResponse(
        source_id=source_id,
        job_id=str(job.id),
        queue_job_id=job.queue_job_id,
        status=job.status.value,
        mode=job.mode or body.mode,
        message=f"Job di sincronizzazione Gmail avviato in modalità {job.mode}.",
    )


@sources_router.get("/{source_id}/config", response_model=SourceSyncConfigResponse)
async def get_source_config(
    source_id: str,
    session: DbSession,
    principal: PrincipalDep,
) -> SourceSyncConfigResponse:
    if source_id != "gmail":
        raise HTTPException(
            status_code=400, detail=f"Configurazione non disponibile per '{source_id}'"
        )
    account = await get_gmail_account(session, principal.owner.id, required=False)
    if (
        account is None
        or account.status != ConnectorAccountStatus.ACTIVE
        or not account.oauth_token_ciphertext
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Account Gmail non connesso o non attivo.",
        )
    cfg = await get_or_create_sync_config(session, account)
    return SourceSyncConfigResponse(
        id=cfg.id,
        revision=cfg.revision,
        history_after=cfg.history_after,
        history_before=cfg.history_before,
        selection_label=cfg.selection_label,
        query_filter=cfg.query_filter,
        item_limit=cfg.item_limit,
        request_budget=cfg.request_budget,
        batch_size=cfg.batch_size,
        update_policy=cfg.update_policy,
        query_fingerprint=cfg.query_fingerprint,
        created_at=cfg.created_at,
        updated_at=cfg.updated_at,
    )


@sources_router.put("/{source_id}/config", response_model=SourceSyncConfigResponse)
async def update_source_config_endpoint(
    source_id: str,
    payload: SourceSyncConfigRequest,
    session: DbSession,
    principal: CsrfPrincipalDep,
) -> SourceSyncConfigResponse:
    if source_id != "gmail":
        raise HTTPException(
            status_code=400, detail=f"Configurazione non disponibile per '{source_id}'"
        )
    account = await get_gmail_account(session, principal.owner.id, required=False)
    if (
        account is None
        or account.status != ConnectorAccountStatus.ACTIVE
        or not account.oauth_token_ciphertext
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Account Gmail non connesso o non attivo.",
        )
    input_data = SyncConfigInput(
        history_after=payload.history_after,
        history_before=payload.history_before,
        selection_label=payload.selection_label,
        query_filter=payload.query_filter,
        item_limit=payload.item_limit,
        request_budget=payload.request_budget,
        batch_size=payload.batch_size,
        update_policy=payload.update_policy,
    )
    try:
        cfg, _ = await update_sync_config(session, account, input_data)
    except GmailServiceError as exc:
        if exc.code == "conflict_active_job":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=exc.safe_message
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=exc.safe_message
        ) from exc

    return SourceSyncConfigResponse(
        id=cfg.id,
        revision=cfg.revision,
        history_after=cfg.history_after,
        history_before=cfg.history_before,
        selection_label=cfg.selection_label,
        query_filter=cfg.query_filter,
        item_limit=cfg.item_limit,
        request_budget=cfg.request_budget,
        batch_size=cfg.batch_size,
        update_policy=cfg.update_policy,
        query_fingerprint=cfg.query_fingerprint,
        created_at=cfg.created_at,
        updated_at=cfg.updated_at,
    )


@sources_router.post("/{source_id}/history/preview", response_model=GmailPreviewResponse)
async def preview_history_endpoint(
    source_id: str,
    payload: HistoryPreviewRequest,
    session: DbSession,
    settings: AppSettings,
    principal: PrincipalDep,
) -> GmailPreviewResponse:
    if source_id != "gmail":
        raise HTTPException(status_code=400, detail=f"Anteprima non disponibile per '{source_id}'")
    account = await get_gmail_account(session, principal.owner.id, required=False)
    if (
        account is None
        or account.status != ConnectorAccountStatus.ACTIVE
        or not account.oauth_token_ciphertext
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Account Gmail non connesso o non attivo.",
        )
    try:
        preview = await preview_history_selection(
            account,
            settings,
            after=payload.after,
            before=payload.before,
            item_limit=payload.item_limit,
            request_budget=payload.request_budget,
            batch_size=payload.batch_size,
        )
    except GmailServiceError as exc:
        raise HTTPException(status_code=400, detail=exc.safe_message) from exc
    return GmailPreviewResponse(
        job_id=None,
        selected_items=preview.selected_items,
        pages_read=preview.pages_read,
        requests_used=preview.request_count,
        state=preview.state,
        truncated=preview.truncated,
        policy=preview.policy,
    )


@sources_router.post(
    "/{source_id}/history/import",
    response_model=ImportJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def import_history_endpoint(
    source_id: str,
    payload: HistoryImportRequest,
    session: DbSession,
    procrastinate: ProcrastinateApp,
    principal: CsrfPrincipalDep,
) -> ImportJob:
    if source_id != "gmail":
        raise HTTPException(
            status_code=400, detail=f"Importazione storico non disponibile per '{source_id}'"
        )
    account = await get_gmail_account(session, principal.owner.id, required=False)
    if (
        account is None
        or account.status != ConnectorAccountStatus.ACTIVE
        or not account.oauth_token_ciphertext
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Account Gmail non connesso o non attivo.",
        )

    async def enqueue(*, job_id: str, account_id: str, mode: str) -> int:
        return await procrastinate.configure_task(
            GMAIL_QUEUE_IDENTITY.task_name,
            queue=GMAIL_QUEUE_IDENTITY.queue_name,
            lock="gmail-heavy-global",
        ).defer_async(job_id=job_id, account_id=account_id, mode=mode)

    try:
        return await enqueue_history_import_job(
            session,
            account,
            enqueue,
            after=payload.after,
            before=payload.before,
        )
    except GmailServiceError as exc:
        if exc.code == "account_job_already_active":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=exc.safe_message
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=exc.safe_message
        ) from exc


@sources_router.get("/{source_id}/diagnostics", response_model=GmailItemGapListResponse)
async def get_source_diagnostics(
    source_id: str,
    session: DbSession,
    principal: PrincipalDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> GmailItemGapListResponse:
    """Restituisce le lacune diagnostiche consultabili e bounded per la fonte specificata."""
    if source_id != "gmail":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fonte non supportata")

    account = await get_gmail_account(session, principal.owner.id, required=False)
    if account is None:
        return GmailItemGapListResponse(
            items=[],
            total=0,
            page=page,
            page_size=page_size,
            has_gaps=False,
            open_count=0,
            resolved_count=0,
        )

    repo = AsyncPostgresGmailRepository(session)
    items, total = await repo.list_gaps(
        account.id, status=status_filter, page=page, page_size=page_size
    )
    open_count, resolved_count = await repo.count_gaps(account.id)
    return GmailItemGapListResponse(
        items=[
            GmailItemGapResponse(
                id=item.id,
                gmail_message_id=item.gmail_message_id,
                gmail_thread_id=item.gmail_thread_id,
                config_revision=item.config_revision,
                query_fingerprint=item.query_fingerprint,
                phase=item.phase,
                reason_code=item.reason_code,
                status=item.status,
                occurred_at=item.occurred_at,
                resolved_at=item.resolved_at,
                details=item.details,
            )
            for item in items
        ],
        total=total,
        page=page,
        page_size=page_size,
        has_gaps=open_count > 0,
        open_count=open_count,
        resolved_count=resolved_count,
    )


@sources_router.delete("/{source_id}", response_model=SourceDisconnectResponse)
async def disconnect_source(
    source_id: str,
    session: DbSession,
    settings: AppSettings,
    principal: CsrfPrincipalDep,
    remove_credentials: Annotated[bool, Query()] = False,
) -> SourceDisconnectResponse:
    """Disconnette la fonte, revoca i token remoti ed opzionalmente rimuove le credenziali client memorizzate."""
    if source_id == "immich":
        account = await session.scalar(
            select(ConnectorAccount).where(
                ConnectorAccount.owner_id == principal.owner.id,
                ConnectorAccount.connector_kind == "immich",
            )
        )
        disconnected = False
        if account is not None:
            account.status = ConnectorAccountStatus.REVOKED
            disconnected = True
            await session.commit()

        credentials_removed = False
        if remove_credentials:
            credentials_removed = await SourceCredentialsRepository.delete_credentials(
                session, principal.owner.id, "immich"
            )

        return SourceDisconnectResponse(
            source_id="immich",
            disconnected=disconnected,
            remote_revoked=False,
            credentials_removed=credentials_removed,
            message="Fonte Immich disconnessa con successo.",
        )

    if source_id != "gmail":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Disconnessione non disponibile per la sorgente '{source_id}'.",
        )

    account = await get_gmail_account(session, principal.owner.id, required=False)
    disconnected = False
    remote_revoked = False
    if account is not None:
        rev_res = await disconnect_account(session, settings, account)
        disconnected = True
        remote_revoked = rev_res.remote_revoked

    credentials_removed = False
    if remove_credentials:
        credentials_removed = await SourceCredentialsRepository.delete_credentials(
            session, principal.owner.id, source_id
        )

    return SourceDisconnectResponse(
        source_id=source_id,
        disconnected=disconnected,
        remote_revoked=remote_revoked,
        credentials_removed=credentials_removed,
        message="Fonte Gmail disconnessa con successo.",
    )


@sources_router.post("/{source_id}/oauth/authorize", response_model=GmailOAuthStartResponse)
async def start_source_oauth(
    source_id: str,
    session: DbSession,
    settings: AppSettings,
    principal: CsrfPrincipalDep,
    redirect_uri_override: Annotated[str | None, Query()] = None,
) -> GmailOAuthStartResponse:
    """Inizia il flusso OAuth 2.0 PKCE per la fonte specificata."""
    if source_id != "gmail":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Autenticazione OAuth non supportata per '{source_id}'.",
        )

    try:
        oauth_start = await begin_oauth(
            session,
            settings,
            principal.owner,
            principal.session,
            redirect_uri_override=redirect_uri_override,
        )
        return GmailOAuthStartResponse(
            authorization_url=oauth_start.authorization_url,
            state=oauth_start.state,
            expires_at=oauth_start.expires_at,
        )
    except GmailOAuthNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail=exc.safe_message,
        ) from exc
    except GmailOAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=exc.safe_message,
        ) from exc


@sources_router.get("/{source_id}/oauth/callback")
async def source_oauth_callback(
    source_id: str,
    session: DbSession,
    settings: AppSettings,
    principal: PrincipalDep,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """Callback OAuth 2.0 di ritorno: perfeziona lo scambio del token e reindirizza alla UI."""
    if source_id != "gmail":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"OAuth callback non supportato per '{source_id}'.",
        )

    if error:
        logger.warning("Callback OAuth rifiutata dall'utente per sorgente %s: %s", source_id, error)
        return RedirectResponse(
            url=f"/sources?error={error}",
            status_code=status.HTTP_302_FOUND,
        )

    if not code or not state:
        logger.warning("Callback OAuth incompleta per sorgente %s", source_id)
        return RedirectResponse(
            url="/sources?error=Callback OAuth incompleta",
            status_code=status.HTTP_302_FOUND,
        )

    try:
        await complete_oauth(
            session,
            settings,
            principal.owner,
            principal.session,
            code=code,
            state=state,
        )
        return RedirectResponse(
            url="/sources?connected=gmail&gmail_connected=1",
            status_code=status.HTTP_302_FOUND,
        )
    except GmailOAuthError as exc:
        logger.warning("Callback OAuth fallita per sorgente %s: %s", source_id, exc)
        return RedirectResponse(
            url=f"/sources?error={exc.safe_message}",
            status_code=status.HTTP_302_FOUND,
        )


@sources_router.post("/{source_id}/jobs/{job_id}/cancel", response_model=ImportJobResponse)
async def cancel_source_job(
    source_id: str,
    job_id: uuid.UUID,
    session: DbSession,
    procrastinate: ProcrastinateApp,
    principal: CsrfPrincipalDep,
) -> ImportJobResponse:
    """Annulla in sicurezza un job appartenente alla fonte e al proprietario correnti."""
    if source_id != "immich":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lavoro non trovato")

    job = await session.scalar(
        select(ImportJob)
        .join(ConnectorAccount, ImportJob.connector_account_id == ConnectorAccount.id)
        .where(
            ImportJob.id == job_id,
            ImportJob.source_kind == source_id,
            ConnectorAccount.connector_kind == source_id,
            ConnectorAccount.owner_id == principal.owner.id,
        )
    )
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Lavoro Immich non trovato"
        )

    job = await cancel_import_job(
        session,
        procrastinate,
        job,
        expected_source_kind=source_id,
    )
    runtime = await build_import_job_runtime(session, job)
    return ImportJobResponse.from_job_and_runtime(job, runtime)


@sources_router.get("/{source_id}/items", response_model=ImmichAssetListResponse)
async def list_source_items(
    source_id: str,
    session: DbSession,
    principal: PrincipalDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> ImmichAssetListResponse:
    """Restituisce gli item/asset importati per la fonte specificata con conteggio autorevole."""
    if source_id == "immich":
        from gbrain.infrastructure.immich_repository import AsyncPostgresImmichRepository

        repo = AsyncPostgresImmichRepository(session)
        raw_items = await repo.list_imported_assets(principal.owner.id, limit=limit)
        total = await repo.count_imported_assets(principal.owner.id)

        responses = [
            ImmichAssetResponse(
                source_item_id=item["source_item_id"],
                external_id=item["external_id"],
                occurred_at=datetime.fromisoformat(item["occurred_at"])
                if item.get("occurred_at")
                else None,
                date_source=item.get("date_source", "unknown"),
                asset_type=item.get("asset_type", "IMAGE"),
                description=item.get("description"),
                albums=item.get("albums", []),
                people=item.get("people", []),
                evidence=item.get("evidence", {}),
            )
            for item in raw_items
        ]
        return ImmichAssetListResponse(items=responses, total=total, source_kind="immich")

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Consultazione item non disponibile per la sorgente '{source_id}'.",
    )
