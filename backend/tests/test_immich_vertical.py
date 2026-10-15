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


@pytest.mark.asyncio
async def test_immich_repository_v1_to_v2_migration_semantics() -> None:
    from gbrain.domain.immich import (
        IMMICH_PIPELINE_VERSION,
        IMMICH_SCHEMA_VERSION,
        IMMICH_SOURCE_KIND,
        compute_immich_v1_canonical_hash,
    )
    from gbrain.domain.models import SourceItem, SourceItemState, SourceRevision

    session = InMemorySession()
    repo = AsyncPostgresImmichRepository(session)  # type: ignore[arg-type]

    account_id = uuid4()
    item_id = uuid4()
    asset_id = "migration-photo-1"
    dt = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)

    # 1. Creiamo lo stato esistente nel DB derivato da una precedente pipeline v1
    v1_hash = compute_immich_v1_canonical_hash(
        external_id=asset_id,
        occurred_at=dt,
        date_source=ImmichDateSource.DATE_TIME_ORIGINAL,
        asset_type=ImmichAssetType.IMAGE,
        albums=(),
        description="Foto vacanza",
        people=(),
    )
    v1_item = SourceItem(
        id=item_id,
        connector_account_id=account_id,
        source_kind=IMMICH_SOURCE_KIND,
        external_id=asset_id,
        canonical_hash=v1_hash,
        occurred_at=dt,
        source_state=SourceItemState.ACTIVE,
        last_observed_at=dt,
    )
    v1_rev = SourceRevision(
        id=uuid4(),
        source_item_id=item_id,
        revision_number=1,
        content_hash=v1_hash,
        normalized_metadata={
            "external_id": asset_id,
            "asset_type": "IMAGE",
            "occurred_at": dt.isoformat().replace("+00:00", "Z"),
            "occurred_at_source": "dateTimeOriginal",
            "description": "Foto vacanza",
            "albums": [],
            "people": [],
        },
        normalized_content={
            "asset_id": asset_id,
            "type": "IMAGE",
            "description": "Foto vacanza",
            "albums": [],
            "people": [],
        },
        evidence={"provider": "immich", "version": "v1"},
        pipeline_version="immich-meta-v1",
        captured_at=dt,
    )
    session.add(v1_item)
    session.add(v1_rev)

    # 2. Prima osservazione v2 dell'asset SENZA nuovi metadati semantici:
    # deve essere classificato unchanged, nessuna nuova revisione, canonical_hash intatto
    v2_asset_same = ImmichNormalizedAsset(
        external_id=asset_id,
        occurred_at=dt,
        date_source=ImmichDateSource.DATE_TIME_ORIGINAL,
        asset_type=ImmichAssetType.IMAGE,
        albums=(),
        description="Foto vacanza",
        people=(),
        canonical_hash=compute_immich_canonical_hash(
            external_id=asset_id,
            occurred_at=dt,
            date_source=ImmichDateSource.DATE_TIME_ORIGINAL,
            asset_type=ImmichAssetType.IMAGE,
            albums=(),
            description="Foto vacanza",
            people=(),
            metadata={},
        ),
        provenance_payload={"metadata_only": True},
        metadata={},
    )

    outcome1 = await repo.persist_asset_batch(account_id, [v2_asset_same])
    assert outcome1.unchanged == 1
    assert outcome1.updated == 0
    assert outcome1.inserted == 0
    assert v1_item.canonical_hash == v1_hash

    # Verifica che non sia stata creata alcuna revisione 2
    revs = list(session.revisions.values())
    assert len(revs) == 1
    assert revs[0].revision_number == 1

    # 3. Seconda osservazione v2 CON nuovi metadati semantici (make, model, width, rating):
    # deve creare esattamente la revisione 2 con schema_version 2 e aggiornare canonical_hash
    v2_meta = {
        "make": "Sony",
        "model": "A7 IV",
        "width_px": 7008,
        "height_px": 4672,
        "rating": 5,
    }
    v2_hash = compute_immich_canonical_hash(
        external_id=asset_id,
        occurred_at=dt,
        date_source=ImmichDateSource.DATE_TIME_ORIGINAL,
        asset_type=ImmichAssetType.IMAGE,
        albums=(),
        description="Foto vacanza",
        people=(),
        metadata=v2_meta,
    )
    v2_asset_enriched = ImmichNormalizedAsset(
        external_id=asset_id,
        occurred_at=dt,
        date_source=ImmichDateSource.DATE_TIME_ORIGINAL,
        asset_type=ImmichAssetType.IMAGE,
        albums=(),
        description="Foto vacanza",
        people=(),
        canonical_hash=v2_hash,
        provenance_payload={"metadata_only": True},
        metadata=v2_meta,
    )

    outcome2 = await repo.persist_asset_batch(account_id, [v2_asset_enriched])
    assert outcome2.updated == 1
    assert outcome2.unchanged == 0
    assert outcome2.inserted == 0
    assert v1_item.canonical_hash == v2_hash

    revs2 = list(session.revisions.values())
    assert len(revs2) == 2
    rev2 = max(revs2, key=lambda r: r.revision_number)
    assert rev2.revision_number == 2
    assert rev2.pipeline_version == IMMICH_PIPELINE_VERSION
    assert rev2.content_hash == v2_hash
    assert rev2.normalized_metadata["schema_version"] == IMMICH_SCHEMA_VERSION
    assert rev2.normalized_metadata["make"] == "Sony"

    # 4. Replay successivo identico: invariato, nessuna revisione 3
    outcome3 = await repo.persist_asset_batch(account_id, [v2_asset_enriched])
    assert outcome3.unchanged == 1
    assert outcome3.updated == 0
    revs3 = list(session.revisions.values())
    assert len(revs3) == 2


@pytest.mark.asyncio
async def test_immich_repository_tracks_invalid_outcome_atomically() -> None:
    session = InMemorySession()
    repo = AsyncPostgresImmichRepository(session)  # type: ignore[arg-type]

    account_id = uuid4()
    job_id = uuid4()
    job = ImportJob(
        id=job_id,
        source_kind="immich",
        connector_account_id=account_id,
        mode="full",
        status=ImportJobStatus.RUNNING,
        checkpoint={"total_invalid": 1},
    )
    session.add(job)

    valid_asset = _sample_asset("valid-asset-1")
    outcome = await repo.persist_asset_batch(
        account_id,
        [valid_asset],
        job_id=job_id,
        checkpoint={"phase": "paginating", "total_invalid": 1},
        invalid_count=2,
    )

    assert outcome.inserted == 1
    assert outcome.invalid == 2
    assert job.not_imported_items == 2
    assert job.checkpoint["total_invalid"] == 3
    assert job.checkpoint["missing_evaluation"] == "not_evaluated"


@pytest.mark.asyncio
async def test_immich_job_enrichment_unbounded_and_resume_tranche() -> None:
    session = InMemorySession()
    db = FakeDatabase(session)
    owner_id = uuid4()
    account_id = uuid4()
    job_id = uuid4()

    owner = OwnerAccount(
        id=owner_id,
        owner_slot=1,
        username="enrich_user",
        display_name="Enrich User",
        password_hash="dummy_hash_for_test",  # noqa: S106
    )
    account = ConnectorAccount(
        id=account_id,
        owner_id=owner_id,
        connector_kind="immich",
        external_account_id="inst-enrich",
        primary_email="user@enrich.test",
        status=ConnectorAccountStatus.ACTIVE,
    )
    settings = _test_settings()
    raw_creds = json.dumps({"base_url": "http://127.0.0.1:2283", "api_key": "enrich-key"}).encode(
        "utf-8"
    )
    enc_creds = encrypt_payload(raw_creds, settings.source_credentials_encryption_key)
    cred = SourceClientCredential(
        owner_id=owner_id,
        source_kind="immich",
        auth_method="api_key",
        client_type="api_token",
        encrypted_credentials=enc_creds,
        is_active=True,
    )
    session.add(owner)
    session.add(account)
    session.add(cred)

    # Tranche 1: enrichment con budget richieste a 2 (50 elementi totali), item_limit=None
    job1 = ImportJob(
        id=job_id,
        source_kind="immich",
        connector_account_id=account_id,
        mode="enrichment",
        status=ImportJobStatus.PENDING,
        checkpoint={"phase": "queued"},
        policy_snapshot={
            "import_all": True,
            "item_limit": None,
            "request_budget": 2,
        },
    )
    session.add(job1)

    mock_gw = MagicMock()
    mock_gw.search_period_assets = AsyncMock(
        side_effect=[
            ([_sample_asset(f"enrich-p1-{i}") for i in range(25)], "cursor-page-2"),
            ([_sample_asset(f"enrich-p2-{i}") for i in range(25)], "cursor-page-3"),
            ([_sample_asset(f"enrich-p3-{i}") for i in range(10)], None),
        ]
    )

    await execute_immich_job(
        db,  # type: ignore[arg-type]
        settings,
        job_id=str(job_id),
        account_id=str(account_id),
        mode="enrichment",
        gateway_factory=lambda url, key: mock_gw,
    )

    final_job1 = await session.get(ImportJob, job_id)
    assert final_job1.status == ImportJobStatus.COMPLETED_WITH_WARNINGS
    assert final_job1.checkpoint["phase"] == "incomplete"
    assert final_job1.checkpoint["cursor"] == "cursor-page-3"
    assert final_job1.checkpoint["requests_completed"] == 2
    assert final_job1.processed_items == 50

    # Tranche 2: ripresa a partire dal cursore della tranche precedente (cursor-page-3)
    job2_id = uuid4()
    job2 = ImportJob(
        id=job2_id,
        source_kind="immich",
        connector_account_id=account_id,
        mode="enrichment",
        status=ImportJobStatus.PENDING,
        checkpoint={
            "phase": "resuming",
            "cursor": final_job1.checkpoint["cursor"],
            "total_enumerated": final_job1.processed_items,
            "total_inserted": final_job1.checkpoint["total_inserted"],
            "total_updated": final_job1.checkpoint["total_updated"],
            "total_unchanged": final_job1.checkpoint["total_unchanged"],
            "total_duplicates": final_job1.checkpoint["total_duplicates"],
        },
        policy_snapshot={
            "import_all": True,
            "item_limit": None,
            "request_budget": 2,
        },
    )
    session.add(job2)

    await execute_immich_job(
        db,  # type: ignore[arg-type]
        settings,
        job_id=str(job2_id),
        account_id=str(account_id),
        mode="enrichment",
        gateway_factory=lambda url, key: mock_gw,
    )

    final_job2 = await session.get(ImportJob, job2_id)
    assert final_job2.status == ImportJobStatus.COMPLETED
    assert final_job2.checkpoint["phase"] == "completed"
    assert final_job2.checkpoint["scan_exhaustive"] is True
    assert final_job2.processed_items == 60
