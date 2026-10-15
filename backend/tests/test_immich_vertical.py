from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from gbrain.application.crypto import encrypt_payload
from gbrain.application.immich_jobs import execute_immich_job
from gbrain.domain.immich import (
    ImmichAlbumInfo,
    ImmichAssetType,
    ImmichDateSource,
    ImmichNormalizedAsset,
    ImmichPerson,
    compute_immich_canonical_hash,
)
from gbrain.domain.models import (
    ConnectorAccount,
    ConnectorAccountStatus,
    ImportJob,
    ImportJobStatus,
    OwnerAccount,
    SourceClientCredential,
)
from gbrain.infrastructure.immich_repository import AsyncPostgresImmichRepository
from tests.test_gmail_sync_mode_and_checkpoints import FakeDatabase
from tests.test_sources_wizard_backend import (
    InMemorySession,
    _test_settings,
)


def _sample_asset(asset_id: str, desc: str = "Foto test") -> ImmichNormalizedAsset:
    dt = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    album = ImmichAlbumInfo(id="alb-1", name="Estate")
    person = ImmichPerson(id="per-1", name="Luca")
    chash = compute_immich_canonical_hash(
        external_id=asset_id,
        occurred_at=dt,
        date_source=ImmichDateSource.DATE_TIME_ORIGINAL,
        asset_type=ImmichAssetType.IMAGE,
        albums=[album],
        description=desc,
        people=[person],
    )
    return ImmichNormalizedAsset(
        external_id=asset_id,
        occurred_at=dt,
        date_source=ImmichDateSource.DATE_TIME_ORIGINAL,
        asset_type=ImmichAssetType.IMAGE,
        albums=(album,),
        description=desc,
        people=(person,),
        canonical_hash=chash,
        provenance_payload={"source_kind": "immich", "original_filename": f"{asset_id}.jpg"},
    )


@pytest.mark.asyncio
async def test_immich_repository_idempotence_and_revision_update() -> None:
    session = InMemorySession()
    repo = AsyncPostgresImmichRepository(session)  # type: ignore[arg-type]

    account_id = uuid4()
    owner_id = uuid4()
    owner = OwnerAccount(
        id=owner_id,
        owner_slot=1,
        username="immich_user",
        display_name="Immich User",
        password_hash="dummy_hash_for_test",  # noqa: S106
    )
    account = ConnectorAccount(
        id=account_id,
        owner_id=owner_id,
        connector_kind="immich",
        external_account_id="immich_inst_1",
        primary_email="user@immich.test",
        status=ConnectorAccountStatus.ACTIVE,
    )
    session.add(owner)
    session.add(account)

    assets = [_sample_asset("img-1"), _sample_asset("img-2")]

    # 1. Primo inserimento: 2 inseriti, 0 duplicati
    ins1, dup1 = await repo.persist_asset_batch(account_id, assets)
    assert ins1 == 2
    assert dup1 == 0

    # 2. Replay identico: 0 inseriti, 2 duplicati (idempotenza perfetta)
    ins2, dup2 = await repo.persist_asset_batch(account_id, assets)
    assert ins2 == 0
    assert dup2 == 2

    # 3. Modifica di un asset (descrizione aggiornata)
    modified_asset = _sample_asset("img-1", desc="Nuova descrizione aggiornata")
    ins3, dup3 = await repo.persist_asset_batch(account_id, [modified_asset])
    # Nessun nuovo SourceItem, ma un duplicato con nuova revisione creata
    assert ins3 == 0
    assert dup3 == 1


@pytest.mark.asyncio
async def test_execute_immich_job_full_flow() -> None:
    session = InMemorySession()
    db = FakeDatabase(session)

    owner_id = uuid4()
    account_id = uuid4()
    job_id = uuid4()

    owner = OwnerAccount(
        id=owner_id,
        owner_slot=1,
        username="test_owner",
        display_name="Test",
        password_hash="dummy_hash_for_test",  # noqa: S106
    )
    account = ConnectorAccount(
        id=account_id,
        owner_id=owner_id,
        connector_kind="immich",
        external_account_id="inst-1",
        primary_email="owner@immich.local",
        status=ConnectorAccountStatus.ACTIVE,
    )

    settings = _test_settings()
    # Credenziali cifrate con AES-256-GCM
    raw_creds = json.dumps(
        {"base_url": "http://127.0.0.1:2283", "api_key": "valid-api-key"}
    ).encode("utf-8")
    enc_creds = encrypt_payload(raw_creds, settings.source_credentials_encryption_key)
    cred = SourceClientCredential(
        owner_id=owner_id,
        source_kind="immich",
        auth_method="api_key",
        client_type="api_token",
        encrypted_credentials=enc_creds,
        is_active=True,
    )

    job = ImportJob(
        id=job_id,
        source_kind="immich",
        connector_account_id=account_id,
        mode="full",
        status=ImportJobStatus.PENDING,
        checkpoint={"phase": "queued"},
        policy_snapshot={
            "album_id": "album-vacanze",
            "item_limit": 60,
            # Un album usa una sola request remota; il budget non conta i chunk locali.
            "request_budget": 1,
        },
    )

    session.add(owner)
    session.add(account)
    session.add(cred)
    session.add(job)

    # Mock gateway
    mock_gw = MagicMock()
    mock_gw.fetch_album_assets = AsyncMock(
        return_value=(
            ImmichAlbumInfo(id="album-vacanze", name="Vacanze"),
            [_sample_asset(f"asset-{i}") for i in range(60)],
        )
    )

    await execute_immich_job(
        db,  # type: ignore[arg-type]
        settings,
        job_id=str(job_id),
        account_id=str(account_id),
        mode="full",
        gateway_factory=lambda url, key: mock_gw,
    )

    final_job = await session.get(ImportJob, job_id)
    assert final_job is not None
    assert final_job.status == ImportJobStatus.COMPLETED
    assert final_job.imported_items == 60
    assert final_job.checkpoint["phase"] == "completed"
    assert final_job.checkpoint["album_offset"] == 60
    assert final_job.checkpoint["pages_completed"] == 3
    assert final_job.checkpoint["requests_completed"] == 1
    mock_gw.fetch_album_assets.assert_awaited_once_with("album-vacanze")


@pytest.mark.asyncio
async def test_immich_job_lifecycle_period_v3_fixture_and_replay_idempotency() -> None:
    session = InMemorySession()
    db = FakeDatabase(session)

    owner_id = uuid4()
    account_id = uuid4()
    job_id = uuid4()

    owner = OwnerAccount(
        id=owner_id,
        owner_slot=1,
        username="test_owner_v3",
        display_name="Test V3",
        password_hash="dummy_hash_for_test",  # noqa: S106
    )
    account = ConnectorAccount(
        id=account_id,
        owner_id=owner_id,
        connector_kind="immich",
        external_account_id="inst-v3",
        primary_email="owner@immich.local",
        status=ConnectorAccountStatus.ACTIVE,
    )

    settings = _test_settings()
    raw_creds = json.dumps(
        {"base_url": "http://127.0.0.1:2283", "api_key": "valid-api-key"}
    ).encode("utf-8")
    enc_creds = encrypt_payload(raw_creds, settings.source_credentials_encryption_key)
    cred = SourceClientCredential(
        owner_id=owner_id,
        source_kind="immich",
        auth_method="api_key",
        client_type="api_token",
        encrypted_credentials=enc_creds,
        is_active=True,
    )

    job = ImportJob(
        id=job_id,
        source_kind="immich",
        connector_account_id=account_id,
        mode="full",
        status=ImportJobStatus.PENDING,
        checkpoint={"phase": "queued"},
        policy_snapshot={
            "after": "2026-01-01T00:00:00+00:00",
            "before": "2026-06-01T00:00:00+00:00",
            "item_limit": 30,
        },
    )

    session.add(owner)
    session.add(account)
    session.add(cred)
    session.add(job)

    # 1. Prima esecuzione: due pagine cursor v3.3.3 (25 + 5 asset).
    mock_gw = MagicMock()
    mock_gw.search_period_assets = AsyncMock(
        side_effect=[
            ([_sample_asset(f"v3-asset-{i}") for i in range(25)], "cursor-page-2"),
            ([_sample_asset(f"v3-asset-{i}") for i in range(25, 30)], None),
        ]
    )

    await execute_immich_job(
        db,  # type: ignore[arg-type]
        settings,
        job_id=str(job_id),
        account_id=str(account_id),
        mode="full",
        gateway_factory=lambda url, key: mock_gw,
    )

    final_job = await session.get(ImportJob, job_id)
    assert final_job is not None
    assert final_job.status == ImportJobStatus.COMPLETED
    assert final_job.imported_items == 30
    assert final_job.checkpoint["pages_completed"] == 2
    assert final_job.checkpoint["requests_completed"] == 2
    assert mock_gw.search_period_assets.await_args_list[1].kwargs["cursor"] == "cursor-page-2"

    # 2. Replay idempotente: la riesecuzione non duplica item e non solleva conflitti
    second_job_id = uuid4()
    second_job = ImportJob(
        id=second_job_id,
        source_kind="immich",
        connector_account_id=account_id,
        mode="full",
        status=ImportJobStatus.PENDING,
        checkpoint={"phase": "queued"},
        policy_snapshot={
            "after": "2026-01-01T00:00:00+00:00",
            "before": "2026-06-01T00:00:00+00:00",
            "item_limit": 30,
        },
    )
    session.add(second_job)
    replay_gw = MagicMock()
    replay_gw.search_period_assets = AsyncMock(
        side_effect=[
            ([_sample_asset(f"v3-asset-{i}") for i in range(25)], "cursor-page-2"),
            ([_sample_asset(f"v3-asset-{i}") for i in range(25, 30)], None),
        ]
    )

    await execute_immich_job(
        db,  # type: ignore[arg-type]
        settings,
        job_id=str(second_job_id),
        account_id=str(account_id),
        mode="full",
        gateway_factory=lambda url, key: replay_gw,
    )

    final_second_job = await session.get(ImportJob, second_job_id)
    assert final_second_job is not None
    assert final_second_job.status == ImportJobStatus.COMPLETED
    assert final_second_job.imported_items == 0
    assert final_second_job.duplicate_items == 30
    assert final_second_job.checkpoint["phase"] == "completed"


@pytest.mark.asyncio
async def test_immich_resume_budget_and_cooperative_cancel_contracts() -> None:
    async def build_job(
        *,
        checkpoint: dict[str, object],
        policy_snapshot: dict[str, object],
    ) -> tuple[InMemorySession, FakeDatabase, object, ConnectorAccount, ImportJob]:
        session = InMemorySession()
        db = FakeDatabase(session)
        owner = OwnerAccount(
            id=uuid4(),
            owner_slot=1,
            username=f"owner-{uuid4()}",
            display_name="Test",
            password_hash="dummy_hash_for_test",  # noqa: S106
        )
        account = ConnectorAccount(
            id=uuid4(),
            owner_id=owner.id,
            connector_kind="immich",
            external_account_id=f"instance-{uuid4()}",
            primary_email="owner@immich.local",
            status=ConnectorAccountStatus.ACTIVE,
        )
        settings = _test_settings()
        encrypted = encrypt_payload(
            json.dumps({"base_url": "http://127.0.0.1:2283", "api_key": "valid-api-key"}).encode(),
            settings.source_credentials_encryption_key,
        )
        credential = SourceClientCredential(
            owner_id=owner.id,
            source_kind="immich",
            auth_method="api_key",
            client_type="api_token",
            encrypted_credentials=encrypted,
            is_active=True,
        )
        job = ImportJob(
            id=uuid4(),
            source_kind="immich",
            connector_account_id=account.id,
            mode="full",
            status=ImportJobStatus.PENDING,
            checkpoint=checkpoint,
            policy_snapshot=policy_snapshot,
        )
        for entity in (owner, account, credential, job):
            session.add(entity)
        return session, db, settings, account, job

    period = {
        "after": "2026-01-01T00:00:00+00:00",
        "before": "2026-06-01T00:00:00+00:00",
        "item_limit": 30,
    }

    # Resume: il cursor committato viene riutilizzato e i primi 25 elementi non sono richiesti di nuovo.
    session, db, settings, account, job = await build_job(
        checkpoint={
            "phase": "paginating",
            "cursor": "cursor-page-2",
            "total_enumerated": 25,
            "total_inserted": 25,
            "pages_completed": 1,
            "requests_completed": 1,
        },
        policy_snapshot=period,
    )
    resume_gateway = MagicMock()
    resume_gateway.search_period_assets = AsyncMock(
        return_value=([_sample_asset(f"resume-{i}") for i in range(5)], None)
    )
    await execute_immich_job(
        db,  # type: ignore[arg-type]
        settings,  # type: ignore[arg-type]
        job_id=str(job.id),
        account_id=str(account.id),
        mode="full",
        gateway_factory=lambda url, key: resume_gateway,
    )
    resumed = await session.get(ImportJob, job.id)
    assert resumed.status == ImportJobStatus.COMPLETED
    assert resumed.processed_items == 30
    assert resume_gateway.search_period_assets.await_args.kwargs["cursor"] == "cursor-page-2"

    # Budget: una pagina con cursor residuo non viene dichiarata completata.
    session, db, settings, account, job = await build_job(
        checkpoint={"phase": "queued"},
        policy_snapshot={**period, "request_budget": 1},
    )
    budget_gateway = MagicMock()
    budget_gateway.search_period_assets = AsyncMock(
        return_value=([_sample_asset(f"budget-{i}") for i in range(25)], "still-more")
    )
    await execute_immich_job(
        db,  # type: ignore[arg-type]
        settings,  # type: ignore[arg-type]
        job_id=str(job.id),
        account_id=str(account.id),
        mode="full",
        gateway_factory=lambda url, key: budget_gateway,
    )
    budgeted = await session.get(ImportJob, job.id)
    assert budgeted.status == ImportJobStatus.COMPLETED_WITH_WARNINGS
    assert budgeted.checkpoint["stopped_reason"] == "request_budget_reached"
    assert budgeted.checkpoint["cursor"] == "still-more"
    assert budgeted.checkpoint["requests_completed"] == 1

    # Cancel: una richiesta arrivata durante il batch viene osservata prima del batch seguente.
    session, db, settings, account, job = await build_job(
        checkpoint={"phase": "queued"},
        policy_snapshot={**period, "item_limit": 50},
    )

    async def first_page_then_cancel(
        *args: object, **kwargs: object
    ) -> tuple[list[ImmichNormalizedAsset], str]:
        job.cancel_requested_at = datetime.now(UTC)
        return [_sample_asset(f"cancel-{i}") for i in range(25)], "next-page"

    cancel_gateway = MagicMock()
    cancel_gateway.search_period_assets = AsyncMock(side_effect=first_page_then_cancel)
    await execute_immich_job(
        db,  # type: ignore[arg-type]
        settings,  # type: ignore[arg-type]
        job_id=str(job.id),
        account_id=str(account.id),
        mode="full",
        gateway_factory=lambda url, key: cancel_gateway,
    )
    cancelled = await session.get(ImportJob, job.id)
    assert cancelled.status == ImportJobStatus.CANCELLED
    assert cancelled.processed_items == 25
    assert cancelled.checkpoint["cursor"] == "next-page"
    cancel_gateway.search_period_assets.assert_awaited_once()
