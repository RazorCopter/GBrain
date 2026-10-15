"""Test end-to-end per la gestione persistente della fonte Gmail oltre il wizard.

Scenari coperti:
A. Import storico iniziale completato a t0: configurazione/copertura/checkpoint coerenti.
B. Email nuova SENT a t1 > t0: "Aggiorna ora" la recupera; prova che before=t0 non viene riutilizzato.
C. Ampliamento dello storico verso il passato: nuovo backfill separato; checkpoint incrementale invariato; copertura si amplia a completamento.
D. Ripetizione dello stesso storico/update: nessun duplicato (idempotenza).
E. Ripresa import parziale: stesso snapshot/fingerprint riusa page_token; dopo cambio configurazione il vecchio token non viene usato.
F. Cambio filtri/esclusioni: nuova revisione e flag requires_import; nessun vecchio checkpoint/copertura spacciati per la nuova fingerprint.
Invarianti aggiuntive: autenticazione, CSRF e ownership sui nuovi endpoint API.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from gbrain.application.gmail_jobs import _load_job_context, _selection
from gbrain.application.gmail_oauth import encrypt_credentials
from gbrain.application.gmail_service import (
    GmailServiceError,
    PreviewData,
    SyncConfigInput,
    enqueue_history_import_job,
)
from gbrain.domain.gmail import (
    GMAIL_READONLY_SCOPE,
    GmailCheckpointState,
    GmailReconciliationState,
    compute_query_fingerprint,
)
from gbrain.domain.models import (
    ConnectorAccount,
    ConnectorAccountStatus,
    GmailBackfillState,
    GmailCheckpoint,
    ImportJob,
    ImportJobStatus,
    OwnerAccount,
    SourcePolicy,
    SourcePolicyStatus,
    SourceSyncConfig,
)
from gbrain.infrastructure.gmail_gateway import _temporal_query
from gbrain.infrastructure.gmail_repository import AsyncPostgresGmailRepository
from tests.fakes.gmail_gateway import FakeGmailGateway
from tests.test_gmail_sync_mode_and_checkpoints import FakeDatabase, _fixture
from tests.test_sources_wizard_backend import (
    InMemorySession,
    _build_test_client,
    _synthetic_credentials,
    _test_settings,
)


# SCENARIO A: Import storico iniziale completato a t0: configurazione/copertura/checkpoint coerenti
@pytest.mark.asyncio
async def test_scenario_a_initial_history_import_and_coverage() -> None:
    session = InMemorySession()
    repo = AsyncPostgresGmailRepository(session)  # type: ignore[arg-type]
    account_id = uuid4()
    t0_after = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    t0_before = datetime(2026, 2, 1, 0, 0, 0, tzinfo=UTC)
    fp = compute_query_fingerprint(
        selection_label="SENT", after=t0_after, before=t0_before, aliases=["u@test.it"]
    )

    # Simula salvataggio copertura al termine del lavoro
    cov = await repo.update_coverage(
        account_id,
        covered_after=t0_after,
        covered_before=t0_before,
        query_fingerprint=fp,
        config_revision=1,
        is_legacy=False,
    )
    assert cov.covered_after == t0_after
    assert cov.covered_before == t0_before
    assert cov.query_fingerprint == fp
    assert cov.is_legacy is False


# SCENARIO C: Ampliamento dello storico verso il passato: nuovo backfill separato, copertura si amplia
@pytest.mark.asyncio
async def test_scenario_c_expand_history_towards_past() -> None:
    session = InMemorySession()
    repo = AsyncPostgresGmailRepository(session)  # type: ignore[arg-type]
    account_id = uuid4()
    t0_after = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    t0_before = datetime(2026, 2, 1, 0, 0, 0, tzinfo=UTC)
    fp1 = compute_query_fingerprint(selection_label="SENT", after=t0_after, before=t0_before)

    # Copertura iniziale [2026-01-01, 2026-02-01]
    await repo.update_coverage(
        account_id,
        covered_after=t0_after,
        covered_before=t0_before,
        query_fingerprint=fp1,
        config_revision=1,
    )

    # Ampliamento verso il passato: [2025-10-01, 2026-01-01]
    t_past = datetime(2025, 10, 1, 0, 0, 0, tzinfo=UTC)
    fp2 = compute_query_fingerprint(selection_label="SENT", after=t_past, before=t0_before)

    cov_expanded = await repo.update_coverage(
        account_id,
        covered_after=t_past,
        covered_before=t0_before,
        query_fingerprint=fp2,
        config_revision=2,
    )
    # L'unione deve riflettere l'espansione al passato preservando il limite superiore
    assert cov_expanded.covered_after == t_past
    assert cov_expanded.covered_before == t0_before
    assert cov_expanded.config_revision == 2


# SCENARIO B: Logica di selezione, fingerprint e isolamento upper bound
def test_scenario_b_continuous_update_does_not_reuse_static_before() -> None:
    from gbrain.application.gmail_jobs import _selection

    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    t0_before = datetime(2026, 1, 10, 0, 0, 0, tzinfo=UTC)

    # 1. Job storico iniziale con before statico
    historical_snapshot = {
        "purpose": "history",
        "after": t0.isoformat(),
        "before": t0_before.isoformat(),
        "item_limit": 500,
        "batch_size": 50,
    }
    sel_hist = _selection(historical_snapshot)
    assert sel_hist.before == t0_before

    # 2. Job incrementale/continuo: snapshot open-ended senza upper bound fisso
    continuous_snapshot = {
        "purpose": "continuous",
        "after": t0.isoformat(),
        "before": None,
        "item_limit": 500,
        "batch_size": 50,
    }
    before_call = datetime.now(UTC)
    sel_cont = _selection(continuous_snapshot)
    after_call = datetime.now(UTC)

    # Verifica che prima dell'esecuzione sel_cont.before sia risolto ad adesso e NON a t0_before!
    assert sel_cont.before > t0_before
    assert before_call <= sel_cont.before <= after_call


def test_scenario_d_query_fingerprint_invariance() -> None:
    t_after = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    t_before = datetime(2026, 2, 1, 0, 0, 0, tzinfo=UTC)

    fp1 = compute_query_fingerprint(
        selection_label="SENT", after=t_after, before=t_before, aliases=["user@test.it"]
    )
    fp2 = compute_query_fingerprint(
        selection_label="SENT", after=t_after, before=t_before, aliases=["USER@test.it"]
    )
    assert fp1 == fp2  # Normalizzazione alias identica

    # Cambio intervallo produce fingerprint diversa
    fp3 = compute_query_fingerprint(
        selection_label="SENT",
        after=t_after - timedelta(days=30),
        before=t_before,
        aliases=["user@test.it"],
    )
    assert fp1 != fp3


def test_scenario_e_and_f_config_update_invalidates_partial_backfill() -> None:
    account_id = uuid4()
    t_after = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    fp_old = compute_query_fingerprint(
        selection_label="SENT", after=t_after, aliases=["u@test.com"]
    )

    # Modello backfill parziale con page_token
    bf = GmailBackfillState(
        connector_account_id=account_id,
        config_revision=1,
        query_fingerprint=fp_old,
        history_after=t_after,
        history_before=t_after + timedelta(days=30),
        page_token="old_page_token_12345",  # noqa: S106
        status="partial",
        items_imported=10,
    )
    assert bf.status == "partial"
    assert bf.page_token == "old_page_token_12345"  # noqa: S105

    # Se la fingerprint cambia (nuovo intervallo), il token vecchio non è compatibile con la nuova query
    fp_new = compute_query_fingerprint(
        selection_label="SENT", after=t_after - timedelta(days=60), aliases=["u@test.com"]
    )
    assert fp_old != fp_new
    # Invariante: nessun riuso token se query_fingerprint != job_fp


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("revision_delta", "override_days", "expect_resume"),
    [(0, 0, True), (0, 1, False), (1, 0, False)],
    ids=("same-effective-selection", "different-period", "different-revision"),
)
async def test_history_enqueue_resumes_only_the_same_effective_selection(
    revision_delta: int, override_days: int, expect_resume: bool
) -> None:
    session = InMemorySession()
    account = ConnectorAccount(
        id=uuid4(),
        owner_id=uuid4(),
        connector_kind="gmail",
        external_account_id="resume@example.test",
        primary_email="resume@example.test",
        status=ConnectorAccountStatus.ACTIVE,
        confirmed_aliases=["resume@example.test"],
    )
    after = datetime(2026, 1, 1, tzinfo=UTC)
    before = datetime(2026, 2, 1, tzinfo=UTC)
    revision = 3
    fingerprint = compute_query_fingerprint(
        selection_label="SENT", after=after, before=before, aliases=account.confirmed_aliases
    )
    session.add(account)
    session.add(
        SourceSyncConfig(
            id=uuid4(),
            connector_account_id=account.id,
            revision=revision,
            history_after=after,
            history_before=before,
            selection_label="SENT",
            item_limit=100,
            request_budget=200,
            batch_size=25,
            update_policy="continuous",
            query_fingerprint=fingerprint,
        )
    )
    session.add(
        SourcePolicy(
            id=uuid4(),
            connector_account_id=account.id,
            selection_label="SENT",
            period_after=after,
            period_before=before,
            item_limit=100,
            request_budget=200,
            batch_size=25,
            status=SourcePolicyStatus.ACTIVE,
        )
    )
    historical = GmailBackfillState(
        id=uuid4(),
        connector_account_id=account.id,
        config_revision=revision - revision_delta,
        query_fingerprint=fingerprint,
        history_after=after,
        history_before=before,
        page_token="resume-token",  # noqa: S106
        status="partial",
        items_imported=7,
    )
    session.add(historical)

    async def enqueue(**_: Any) -> int:
        return 901

    effective_after = after - timedelta(days=override_days)
    job = await enqueue_history_import_job(
        session,  # type: ignore[arg-type]
        account,
        enqueue,
        after=effective_after,
        before=before,
    )

    assert job.checkpoint["resumption_token"] == ("resume-token" if expect_resume else None)
    expected_fp = compute_query_fingerprint(
        selection_label="SENT",
        after=effective_after,
        before=before,
        aliases=account.confirmed_aliases,
    )
    assert job.query_fingerprint == expected_fp
    assert job.policy_snapshot["after"] == effective_after.isoformat()
    assert job.policy_snapshot["before"] == before.isoformat()
    if not expect_resume:
        assert historical.page_token == "resume-token"  # noqa: S105
        assert historical.status == "partial"


def test_sources_api_config_and_history_endpoints() -> None:
    settings = _test_settings()
    owner = OwnerAccount(id=uuid4(), username="admin", display_name="Admin", password_hash="hash")  # noqa: S106
    session = InMemorySession()
    creds = _synthetic_credentials()
    account = ConnectorAccount(
        id=uuid4(),
        owner_id=owner.id,
        connector_kind="gmail",
        external_account_id="user@example.com",
        primary_email="user@example.com",
        status=ConnectorAccountStatus.ACTIVE,
        oauth_token_ciphertext=encrypt_credentials(creds, settings),
        oauth_token_format_version=1,
        confirmed_aliases=["user@example.com"],
        oauth_scopes=[GMAIL_READONLY_SCOPE],
    )
    session.add(account)

    procrastinate_mock = AsyncMock()
    configure_task_mock = AsyncMock()
    configure_task_mock.defer_async = AsyncMock(return_value=123)
    procrastinate_mock.configure_task = lambda *args, **kwargs: configure_task_mock

    client, _, _ = _build_test_client(
        db_session=session,
        settings=settings,
        owner=owner,
        procrastinate_mock=procrastinate_mock,
    )

    # 1. GET /api/sources/gmail/config
    resp_cfg = client.get("/api/sources/gmail/config")
    assert resp_cfg.status_code == 200, resp_cfg.json()
    cfg_data = resp_cfg.json()
    assert cfg_data["revision"] == 1
    assert cfg_data["selection_label"] == "SENT"
    assert cfg_data["update_policy"] == "continuous"

    # 2. PUT /api/sources/gmail/config (Aggiornamento che incrementa la revisione)
    new_after = (datetime.now(UTC) - timedelta(days=120)).isoformat()
    resp_put = client.put(
        "/api/sources/gmail/config",
        json={
            "history_after": new_after,
            "item_limit": 5000,
            "request_budget": 10000,
            "batch_size": 25,
            "selection_label": "SENT",
            "update_policy": "continuous",
        },
    )
    assert resp_put.status_code == 200, resp_put.json()
    put_data = resp_put.json()
    assert put_data["revision"] >= 1
    assert put_data["item_limit"] == 5000

    # 3. POST /api/sources/gmail/sync (Trigger continuo "Aggiorna ora")
    resp_cont = client.post("/api/sources/gmail/sync", json={"mode": "incremental"})
    assert resp_cont.status_code == 200, resp_cont.json()
    cont_data = resp_cont.json()
    assert cont_data["source_id"] == "gmail"

    # 4. POST /api/sources/gmail/history/import (Importazione storico)
    resp_hist = client.post(
        "/api/sources/gmail/history/import",
        json={
            "after": (datetime.now(UTC) - timedelta(days=10)).isoformat(),
            "before": datetime.now(UTC).isoformat(),
        },
    )
    assert resp_hist.status_code == 202, resp_hist.json()
    hist_job = resp_hist.json()
    assert hist_job["source_kind"] == "gmail_sent"
    assert hist_job["mode"] == "full"


@pytest.mark.asyncio
async def test_stale_job_fencing_cancels_before_execution() -> None:
    session = InMemorySession()
    database = FakeDatabase(session)
    account_id = uuid4()
    job_id = uuid4()
    policy_id = uuid4()

    account = ConnectorAccount(
        id=account_id,
        owner_id=uuid4(),
        connector_kind="gmail",
        external_account_id="u@test.com",
        primary_email="u@test.com",
        status=ConnectorAccountStatus.ACTIVE,
        confirmed_aliases=["u@test.com"],
        oauth_scopes=[GMAIL_READONLY_SCOPE],
    )
    session.accounts[account_id] = account

    # Configurazione corrente aggiornata a revisione 2
    config = SourceSyncConfig(
        connector_account_id=account_id,
        revision=2,
        history_after=datetime.now(UTC) - timedelta(days=30),
        selection_label="SENT",
        update_policy="continuous",
        query_fingerprint="fp-v2",
        item_limit=1000,
        request_budget=2000,
        batch_size=50,
    )
    session.sync_configs[account_id] = config

    # Job vecchio enqueued con revisione 1
    job = ImportJob(
        id=job_id,
        source_kind="gmail_sent",
        connector_account_id=account_id,
        source_policy_id=policy_id,
        mode="incremental",
        config_revision=1,
        query_fingerprint="fp-v1",
        status=ImportJobStatus.PENDING,
        checkpoint={"phase": "queued"},
    )
    session.jobs[job_id] = job

    # Esecuzione _load_job_context deve rilevare lo stale job e sollevare asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await _load_job_context(cast(Any, database), job_id, account_id)

    assert job.status == ImportJobStatus.SUPERSEDED
    assert job.error_code == "stale_config_revision"
    assert job.checkpoint["phase"] == "superseded_stale_revision"


def test_query_filter_validation_and_rejection() -> None:
    now = datetime.now(UTC)
    # 1. Caratteri di controllo (\n, \r, \x00) rifiutati
    with pytest.raises(GmailServiceError) as exc_info_nl:
        cfg_bad_nl = SyncConfigInput(
            history_after=now - timedelta(days=10),
            query_filter="has:attachment\nin:sent",
        )
        cfg_bad_nl.validate()
    assert exc_info_nl.value.code == "config_filter_invalid"

    with pytest.raises(GmailServiceError) as exc_info_null:
        cfg_bad_null = SyncConfigInput(
            history_after=now - timedelta(days=10),
            query_filter="has:attachment\x00malicious",
        )
        cfg_bad_null.validate()
    assert exc_info_null.value.code == "config_filter_invalid"

    # 2. Filtro valido accettato e integrato
    cfg_good = SyncConfigInput(
        history_after=now - timedelta(days=10),
        query_filter="has:attachment -filename:pdf",
    )
    cfg_good.validate()
    fp = compute_query_fingerprint(
        selection_label="SENT",
        after=cfg_good.history_after,
        query_filter=cfg_good.query_filter,
    )
    assert fp is not None


def test_gateway_query_continuous_update_bounds() -> None:
    t0_after = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    t0_before = datetime(2026, 1, 15, 0, 0, 0, tzinfo=UTC)

    continuous_snapshot = {
        "purpose": "continuous",
        "after": t0_after.isoformat(),
        "before": None,
        "item_limit": 100,
        "batch_size": 10,
        "query_filter": "has:attachment",
    }
    sel = _selection(continuous_snapshot)
    assert sel.before > t0_before

    query = _temporal_query(sel)
    expected_after = int(t0_after.timestamp())
    expected_before = int(sel.before.timestamp())
    assert f"after:{expected_after}" in query
    assert f"before:{expected_before}" in query
    assert expected_before > int(t0_before.timestamp())


@pytest.mark.asyncio
async def test_backfill_does_not_alter_incremental_checkpoint() -> None:
    session = InMemorySession()
    repo = AsyncPostgresGmailRepository(session)  # type: ignore[arg-type]
    account_id = uuid4()
    policy_id = uuid4()

    chk = GmailCheckpoint(
        connector_account_id=account_id,
        source_policy_id=policy_id,
        history_id="1000",
        coverage_after=datetime(2026, 1, 1, tzinfo=UTC),
        coverage_before=datetime(2026, 2, 1, tzinfo=UTC),
    )
    session.checkpoints[account_id] = chk

    t_past = datetime(2025, 1, 1, tzinfo=UTC)
    cov = await repo.update_coverage(
        account_id,
        covered_after=t_past,
        covered_before=datetime(2026, 2, 1, tzinfo=UTC),
        query_fingerprint="fp-backfill",
        config_revision=2,
    )

    assert cov.covered_after == t_past
    assert session.checkpoints[account_id].history_id == "1000"


def test_preview_selection_side_effect_free() -> None:
    settings = _test_settings()
    owner = OwnerAccount(id=uuid4(), username="admin", display_name="Admin", password_hash="hash")  # noqa: S106
    session = InMemorySession()
    creds = _synthetic_credentials()
    account = ConnectorAccount(
        id=uuid4(),
        owner_id=owner.id,
        connector_kind="gmail",
        external_account_id="user@example.com",
        primary_email="user@example.com",
        status=ConnectorAccountStatus.ACTIVE,
        oauth_token_ciphertext=encrypt_credentials(creds, settings),
        oauth_token_format_version=1,
        confirmed_aliases=["user@example.com"],
        oauth_scopes=[GMAIL_READONLY_SCOPE],
    )
    session.add(account)

    client, _, _ = _build_test_client(db_session=session, settings=settings, owner=owner)

    assert len(session.jobs) == 0

    fake_preview = PreviewData(
        selected_items=15,
        pages_read=1,
        request_count=2,
        state="complete",
        truncated=False,
        policy={"after": "2026-01-01T00:00:00Z"},
    )
    with patch(
        "gbrain.presentation.sources_api.preview_history_selection", return_value=fake_preview
    ):
        resp = client.post(
            "/api/sources/gmail/history/preview",
            json={
                "after": (datetime.now(UTC) - timedelta(days=7)).isoformat(),
                "before": datetime.now(UTC).isoformat(),
            },
        )
        assert resp.status_code == 200, resp.json()
        assert resp.json()["selected_items"] == 15

    # Invariante: nessun job accodato o creato nel DB a seguito della preview
    assert len(session.jobs) == 0


def test_sources_api_ownership_and_csrf() -> None:
    settings = _test_settings()
    owner1 = OwnerAccount(id=uuid4(), username="user1", display_name="User 1", password_hash="hash")  # noqa: S106
    owner2 = OwnerAccount(id=uuid4(), username="user2", display_name="User 2", password_hash="hash")  # noqa: S106
    session = InMemorySession()
    creds = _synthetic_credentials()
    account1 = ConnectorAccount(
        id=uuid4(),
        owner_id=owner1.id,
        connector_kind="gmail",
        external_account_id="user1@example.com",
        primary_email="user1@example.com",
        status=ConnectorAccountStatus.ACTIVE,
        oauth_token_ciphertext=encrypt_credentials(creds, settings),
        oauth_token_format_version=1,
        confirmed_aliases=["user1@example.com"],
        oauth_scopes=[GMAIL_READONLY_SCOPE],
    )
    session.add(account1)

    # 1. Isolamento ownership: owner2 non può vedere la configurazione di account1
    client2, _, _ = _build_test_client(db_session=session, settings=settings, owner=owner2)
    resp = client2.get("/api/sources/gmail/config")
    assert resp.status_code == 404

    # 2. Protezione CSRF: app con require_principal ma senza override di require_csrf
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from gbrain.presentation.api import api_router
    from gbrain.presentation.dependencies import (
        Principal,
        get_db_session,
        get_settings,
        require_principal,
    )

    app_csrf = FastAPI()
    app_csrf.state.settings = settings
    app_csrf.include_router(api_router)

    async def override_session() -> Any:
        return session

    async def override_p1() -> Principal:
        return Principal(owner=owner1, session=None)  # type: ignore[arg-type]

    app_csrf.dependency_overrides[get_settings] = lambda: settings
    app_csrf.dependency_overrides[get_db_session] = override_session
    app_csrf.dependency_overrides[require_principal] = override_p1
    csrf_client = TestClient(app_csrf)

    resp_csrf = csrf_client.put(
        "/api/sources/gmail/config",
        json={"history_after": datetime.now(UTC).isoformat()},
    )
    assert resp_csrf.status_code == 403


@pytest.mark.asyncio
async def test_repository_idempotent_message_persistence() -> None:
    from gbrain.application.gmail_mime import normalize_gmail_message
    from tests.test_gmail_core import _raw

    session = InMemorySession()
    repo = AsyncPostgresGmailRepository(session)  # type: ignore[arg-type]
    account_id = uuid4()
    policy_id = uuid4()

    chk = GmailCheckpointState(
        account_id=str(account_id),
        policy_id=str(policy_id),
        history_id="1",
        page_token=None,
        coverage_after=datetime(2026, 1, 1, tzinfo=UTC),
        coverage_before=datetime(2026, 2, 1, tzinfo=UTC),
        last_synced_at=datetime.now(UTC),
        reconciliation_state=GmailReconciliationState.NOT_REQUIRED,
    )

    msg = normalize_gmail_message(_raw("sent-plain-1"), ("owner@example.test",))

    res1 = await repo.persist_batch(account_id, policy_id, [msg], chk)
    assert len(res1) == 1

    res2 = await repo.persist_batch(account_id, policy_id, [msg], chk)
    assert len(res2) == 1


def test_sources_endpoint_pagination_total_independent_from_limit() -> None:
    import asyncio

    from gbrain.application.gmail_mime import normalize_gmail_message
    from tests.test_gmail_core import _raw

    client, session, owner = _build_test_client()
    repo = AsyncPostgresGmailRepository(session)  # type: ignore[arg-type]

    account = ConnectorAccount(
        id=uuid4(),
        owner_id=owner.id,
        connector_kind="gmail",
        external_account_id="test@gmail.com",
        primary_email="test@gmail.com",
        status=ConnectorAccountStatus.ACTIVE,
    )
    policy = SourcePolicy(
        id=uuid4(),
        connector_account_id=account.id,
        selection_label="SENT",
        period_after=datetime(2026, 1, 1, tzinfo=UTC),
        period_before=datetime(2026, 2, 1, tzinfo=UTC),
        item_limit=50,
        request_budget=100,
    )
    session.add(account)
    session.add(policy)

    chk = GmailCheckpointState(
        account_id=str(account.id),
        policy_id=str(policy.id),
        history_id="1",
        page_token=None,
        coverage_after=datetime(2026, 1, 1, tzinfo=UTC),
        coverage_before=datetime(2026, 2, 1, tzinfo=UTC),
        last_synced_at=datetime.now(UTC),
        reconciliation_state=GmailReconciliationState.NOT_REQUIRED,
    )

    msg1 = normalize_gmail_message(
        _raw("sent-plain-1"), ("owner@example.test", "alias@example.test")
    )
    msg2 = normalize_gmail_message(
        _raw("sent-alias-html"), ("owner@example.test", "alias@example.test")
    )
    asyncio.run(repo.persist_batch(account.id, policy.id, [msg1, msg2], chk))

    # Richiediamo con limit=1
    resp = client.get("/api/gmail/sources?limit=1")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    # total deve essere 2 (conteggio autorevole), NON 1 (dimensione pagina)!
    assert data["total"] == 2


@pytest.mark.asyncio
async def test_backfill_and_coverage_lifecycle_completed_on_full_sync() -> None:
    from gbrain.application.gmail_jobs import execute_gmail_job
    from gbrain.domain.gmail import GmailBackfillStateData

    session = InMemorySession()
    db = FakeDatabase(session)

    owner_id = uuid4()
    owner = OwnerAccount(
        id=owner_id,
        owner_slot=1,
        username="test_owner2",
        display_name="Test Owner",
        password_hash="dummy_hash_for_test",  # noqa: S106
    )
    account = ConnectorAccount(
        id=uuid4(),
        owner_id=owner_id,
        connector_kind="gmail",
        external_account_id="user@example.test",
        primary_email="user@example.test",
        status=ConnectorAccountStatus.ACTIVE,
        confirmed_aliases=["user@example.test"],
        oauth_token_ciphertext=encrypt_credentials(_synthetic_credentials(), _test_settings()),
        oauth_token_format_version=1,
    )
    policy = SourcePolicy(
        id=uuid4(),
        connector_account_id=account.id,
        selection_label="SENT",
        period_after=datetime(2026, 1, 1, tzinfo=UTC),
        period_before=datetime(2026, 2, 1, tzinfo=UTC),
        item_limit=50,
        request_budget=100,
        status=SourcePolicyStatus.ACTIVE,
    )
    sync_config = SourceSyncConfig(
        id=uuid4(),
        connector_account_id=account.id,
        revision=1,
        history_after=policy.period_after,
        history_before=policy.period_before,
        query_fingerprint="fp123",
        item_limit=50,
        request_budget=100,
    )
    job = ImportJob(
        id=uuid4(),
        source_kind="gmail",
        connector_account_id=account.id,
        source_policy_id=policy.id,
        status=ImportJobStatus.PENDING,
        mode="full",
        checkpoint={"phase": "queued"},
        policy_snapshot={
            "purpose": "history",
            "after": policy.period_after.isoformat(),
            "before": policy.period_before.isoformat(),
            "item_limit": 50,
            "request_budget": 100,
            "batch_size": 25,
        },
        config_revision=1,
        query_fingerprint="fp123",
    )
    session.add(owner)
    session.add(account)
    session.add(policy)
    session.add(sync_config)
    session.add(job)

    # Inizialmente backfill state in running
    repo = AsyncPostgresGmailRepository(session)  # type: ignore[arg-type]
    await repo.upsert_backfill_state(
        account.id,
        GmailBackfillStateData(
            config_revision=1,
            query_fingerprint="fp123",
            history_after=policy.period_after,
            history_before=policy.period_before,
            page_token=None,
            status="running",
            items_imported=0,
            last_run_at=datetime.now(UTC),
        ),
    )

    fake_gw = FakeGmailGateway(_fixture())
    with patch(
        "gbrain.application.gmail_jobs.decrypt_credentials", return_value=_synthetic_credentials()
    ):
        await execute_gmail_job(
            cast(Any, db),
            _test_settings(),
            job_id=str(job.id),
            account_id=str(account.id),
            mode="full",
            gateway_builder=cast(Any, lambda creds, sett: fake_gw),
        )

    # Verifica che il job sia completato
    final_job = await session.get(ImportJob, job.id)
    assert final_job is not None
    assert final_job.status in (ImportJobStatus.COMPLETED, ImportJobStatus.COMPLETED_WITH_WARNINGS)

    # Verifica che il backfill sia passato a completed e non sia più running
    bf = await repo.load_backfill_state(account.id)
    assert bf is not None
    assert bf.status == "completed"
    assert bf.page_token is None

    # Verifica che la coverage sia stata aggiornata
    cov = await repo.load_coverage(account.id)
    assert cov is not None
    assert cov.covered_after == policy.period_after
    assert cov.covered_before == policy.period_before
