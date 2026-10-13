from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from gbrain.application.gmail_jobs import execute_gmail_job
from gbrain.application.gmail_oauth import encrypt_credentials
from gbrain.application.gmail_service import policy_input
from gbrain.application.gmail_sync import (
    GmailSentConnector,
    GmailSyncEngine,
    InMemoryGmailUnitOfWork,
)
from gbrain.domain.connectors import (
    PageState,
    RequestBudget,
)
from gbrain.domain.gmail import (
    GmailCheckpointState,
    GmailReconciliationState,
    GmailSelection,
    GmailSyncMode,
    is_initial_import_completed,
    resolve_sync_mode,
)
from gbrain.domain.models import (
    ConnectorAccount,
    ConnectorAccountStatus,
    GmailCheckpoint,
    ImportJob,
    ImportJobStatus,
    ReconciliationState,
    SourcePolicy,
    SourcePolicyStatus,
)
from tests.fakes.gmail_gateway import FakeGmailGateway
from tests.test_sources_wizard_backend import (
    InMemorySession,
    _build_test_client,
    _synthetic_credentials,
    _test_settings,
)

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "gmail" / "messages.json"
_NOW = datetime(2024, 2, 1, 12, 0, tzinfo=UTC)
_SELECTION = GmailSelection(
    after=datetime(2024, 1, 1, tzinfo=UTC),
    before=datetime(2024, 2, 1, tzinfo=UTC),
    limit=100,
    page_size=2,
)


def _fixture() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(_FIXTURE_PATH.read_text(encoding="utf-8")))


def _connector(gateway: FakeGmailGateway) -> GmailSentConnector:
    return GmailSentConnector(
        gateway,
        ("owner@example.test", "alias@example.test"),
        now=lambda: _NOW,
    )


class FakeDatabase:
    """Fake database minimale per il test di execute_gmail_job in-memory."""

    def __init__(self, session: InMemorySession) -> None:
        self._session = session

    def session_factory(self) -> Any:
        session = self._session

        class _Context:
            async def __aenter__(self) -> InMemorySession:
                return session

            async def __aexit__(self, *args: Any) -> None:
                pass

        return _Context()


# =========================================================================
# 1. Caso d'uso 1: Account nuovo (senza checkpoint) -> avvio sincronizzazione FULL
# =========================================================================


def test_case_1_new_account_resolves_to_full_sync() -> None:
    # 1. Funzione di dominio pura: checkpoint None -> risolve a FULL
    assert resolve_sync_mode(None, "incremental") == GmailSyncMode.FULL
    assert resolve_sync_mode(None, None) == GmailSyncMode.FULL
    assert is_initial_import_completed(None) is False

    # Checkpoint esistente ma con history_id nullo (mai completato)
    empty_chk = GmailCheckpointState(
        account_id="acc-1",
        policy_id="pol-1",
        history_id=None,
        page_token=None,
        coverage_after=_SELECTION.after,
        coverage_before=_SELECTION.before,
        last_synced_at=None,
        reconciliation_state=GmailReconciliationState.NOT_REQUIRED,
    )
    assert resolve_sync_mode(empty_chk, "incremental") == GmailSyncMode.FULL
    assert is_initial_import_completed(empty_chk) is False


def test_case_1_new_account_api_endpoint_forces_full() -> None:
    procrastinate_mock = AsyncMock()
    configure_task_mock = AsyncMock()
    defer_mock = AsyncMock(return_value=123)
    configure_task_mock.defer_async = defer_mock
    procrastinate_mock.configure_task = lambda *args, **kwargs: configure_task_mock

    client, session, owner = _build_test_client(procrastinate_mock=procrastinate_mock)
    settings = _test_settings()

    creds = _synthetic_credentials()
    ciphertext = encrypt_credentials(creds, settings)
    account = ConnectorAccount(
        id=uuid4(),
        owner_id=owner.id,
        connector_kind="gmail",
        external_account_id="acc-test",
        primary_email="user@testdomain.com",
        status=ConnectorAccountStatus.ACTIVE,
        oauth_token_ciphertext=ciphertext,
        oauth_token_format_version=1,
        confirmed_aliases=["user@testdomain.com"],
    )
    session.add(account)

    # Chiamata con richiesta fraudolenta o erronea di "incremental" senza checkpoint
    resp = client.post("/api/sources/gmail/sync", json={"mode": "incremental", "item_limit": 50})
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "full"
    assert "modalità full" in data["message"]


# =========================================================================
# 2. Caso d'uso 2: Importazione iniziale completata con successo -> avvio INCREMENTAL
# =========================================================================


def test_case_2_completed_initial_import_resolves_to_incremental() -> None:
    completed_chk = GmailCheckpointState(
        account_id="acc-1",
        policy_id="pol-1",
        history_id="900",
        page_token=None,
        coverage_after=_SELECTION.after,
        coverage_before=_SELECTION.before,
        last_synced_at=_NOW,
        reconciliation_state=GmailReconciliationState.NOT_REQUIRED,
    )
    assert is_initial_import_completed(completed_chk) is True
    assert resolve_sync_mode(completed_chk, "incremental") == GmailSyncMode.INCREMENTAL
    assert resolve_sync_mode(completed_chk, None) == GmailSyncMode.INCREMENTAL
    # Rispetta richiesta esplicita di FULL se voluta dall'utente
    assert resolve_sync_mode(completed_chk, "full") == GmailSyncMode.FULL


def test_case_2_sync_engine_executes_incremental_when_completed() -> None:
    gateway = FakeGmailGateway(_fixture())
    gateway.history_pages = [
        {"history_id": "905", "changed": ["sent-plain-1"], "deleted": []},
    ]
    repository = InMemoryGmailUnitOfWork()
    completed_chk = GmailCheckpointState(
        account_id="account-1",
        policy_id="policy-1",
        history_id="900",
        page_token=None,
        coverage_after=_SELECTION.after,
        coverage_before=_SELECTION.before,
        last_synced_at=_NOW,
        reconciliation_state=GmailReconciliationState.NOT_REQUIRED,
    )

    engine = GmailSyncEngine(_connector(gateway), repository)
    result = engine.sync(
        _SELECTION, completed_chk, RequestBudget(10, 10), requested_mode="incremental"
    )

    assert result.mode == GmailSyncMode.INCREMENTAL
    assert result.state == PageState.COMPLETE
    assert result.checkpoint.history_id == "905"


# =========================================================================
# 3. Caso d'uso 3: Importazione parziale (interrotta o con errore) -> ripresa coerente
# =========================================================================


def test_case_3_partial_checkpoint_resolves_to_full_and_resumes() -> None:
    # Checkpoint parziale con cursore page_token pendente
    partial_chk = GmailCheckpointState(
        account_id="account-1",
        policy_id="policy-1",
        history_id="900",
        page_token="1",  # noqa: S106
        coverage_after=_SELECTION.after,
        coverage_before=_SELECTION.before,
        last_synced_at=_NOW,
        reconciliation_state=GmailReconciliationState.NOT_REQUIRED,
    )

    # 1. Non è considerata completata
    assert is_initial_import_completed(partial_chk) is False
    assert partial_chk.is_initial_complete is False

    # 2. Anche chiedendo "incremental", il backend autoritativo risolve su "full" per terminare l'importazione
    assert resolve_sync_mode(partial_chk, "incremental") == GmailSyncMode.FULL

    # 3. Verifica ripresa coerente tramite cursore di paginazione:
    # Il gateway riceve page_token "1"
    gateway = FakeGmailGateway(_fixture(), page_size=1)
    repository = InMemoryGmailUnitOfWork()
    engine = GmailSyncEngine(_connector(gateway), repository, batch_size=1)

    result = engine.sync(
        _SELECTION, partial_chk, RequestBudget(10, 10), requested_mode="incremental"
    )

    assert result.mode == GmailSyncMode.FULL
    # La chiamata al gateway per la pagina successiva deve aver usato il cursore memorizzato
    assert any(
        name == "messages.list" and args.get("pageToken") == "1" for name, args in gateway.calls
    )


# =========================================================================
# 4. Caso d'uso 4: History scaduto su Google durante incremental -> fallback a riconciliazione
# =========================================================================


def test_case_4_history_expired_fallbacks_to_reconciliation() -> None:
    gateway = FakeGmailGateway(_fixture())
    gateway.history_expired = True
    repository = InMemoryGmailUnitOfWork()

    checkpoint = GmailCheckpointState(
        account_id="account-1",
        policy_id="policy-1",
        history_id="old-history-expired",
        page_token=None,
        coverage_after=_SELECTION.after,
        coverage_before=_SELECTION.before,
        last_synced_at=_NOW,
        reconciliation_state=GmailReconciliationState.NOT_REQUIRED,
    )

    engine = GmailSyncEngine(_connector(gateway), repository)
    # L'engine rileva la scadenza e fa fallback controllato a full_reconcile
    result = engine.sync(
        _SELECTION, checkpoint, RequestBudget(20, 20), requested_mode="incremental"
    )

    assert result.mode == GmailSyncMode.RECONCILIATION
    assert "history_expired_fallback_reconciled" in result.detail_codes
    assert result.state == PageState.COMPLETE
    # Il checkpoint risultante ha reconciliation_state = COMPLETE
    assert result.checkpoint.reconciliation_state == GmailReconciliationState.COMPLETE


@pytest.mark.asyncio
async def test_case_4_worker_executes_fallback_reconciliation_on_expired_history() -> None:
    session = InMemorySession()
    database = FakeDatabase(session)
    settings = _test_settings()

    account_id = uuid4()
    policy_id = uuid4()
    job_id = uuid4()

    account = ConnectorAccount(
        id=account_id,
        owner_id=uuid4(),
        connector_kind="gmail",
        external_account_id="acc-expired",
        primary_email="owner@example.test",
        status=ConnectorAccountStatus.ACTIVE,
        oauth_token_ciphertext=encrypt_credentials(_synthetic_credentials(), settings),
        oauth_token_format_version=1,
        confirmed_aliases=["owner@example.test"],
    )
    policy = SourcePolicy(
        id=policy_id,
        connector_account_id=account_id,
        period_after=_SELECTION.after,
        period_before=_SELECTION.before,
        item_limit=50,
        request_budget=50,
        batch_size=10,
        status=SourcePolicyStatus.ACTIVE,
    )
    job = ImportJob(
        id=job_id,
        source_kind="gmail_sent",
        connector_account_id=account_id,
        source_policy_id=policy_id,
        mode="incremental",
        policy_snapshot=policy_input(policy).snapshot(),
        checkpoint={"phase": "created"},
        status=ImportJobStatus.PENDING,
    )
    chk_model = GmailCheckpoint(
        connector_account_id=account_id,
        source_policy_id=policy_id,
        history_id="expired-history-999",
        page_token=None,
        reconciliation_state=ReconciliationState.NOT_REQUIRED,
    )

    session.add(account)
    session.add(policy)
    session.add(job)
    session.add(chk_model)

    gateway = FakeGmailGateway(_fixture())
    gateway.history_expired = True

    try:
        await execute_gmail_job(
            cast(Any, database),
            settings,
            job_id=str(job_id),
            account_id=str(account_id),
            mode="incremental",
            gateway_builder=lambda acc, sett: cast(Any, gateway),
        )
    except Exception as exc:
        print("EXCEPTION RAISED IN EXECUTE_GMAIL_JOB:", type(exc), exc)
        raise

    # Il job non è crashato, bensì è stato completato con successo grazie alla riconciliazione automatica!
    finished_job = session.jobs[job_id]
    assert finished_job.status == ImportJobStatus.COMPLETED, (
        f"Job error: {finished_job.error_code} - {finished_job.error_detail}"
    )
    assert finished_job.error_code is None
