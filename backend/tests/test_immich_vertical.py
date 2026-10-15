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
            "item_limit": 25,
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
            [_sample_asset(f"asset-{i}") for i in range(10)],
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
    assert final_job.imported_items == 10
    assert final_job.checkpoint["phase"] == "completed"
