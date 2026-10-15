from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gbrain.domain.immich import IMMICH_SOURCE_KIND, ImmichNormalizedAsset
from gbrain.domain.models import (
    ConnectorAccount,
    ImportJob,
    SourceItem,
    SourceItemState,
    SourceRevision,
)


class AsyncPostgresImmichRepository:
    """Repository PostgreSQL transazionale per il connettore Immich metadata-only."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def persist_asset_batch(
        self,
        account_id: uuid.UUID,
        assets: Sequence[ImmichNormalizedAsset],
        *,
        job_id: uuid.UUID | None = None,
        checkpoint: dict[str, Any] | None = None,
    ) -> tuple[int, int]:
        """Salva atomicamente un batch di metadati asset e avanza il checkpoint del job.

        Ritorna la tupla (inseriti, duplicati).
        """
        inserted_count = 0
        duplicate_count = 0
        now = datetime.now(UTC)

        async with self._session.begin():
            for asset in assets:
                item = await self._session.scalar(
                    select(SourceItem)
                    .where(
                        SourceItem.connector_account_id == account_id,
                        SourceItem.source_kind == IMMICH_SOURCE_KIND,
                        SourceItem.external_id == asset.external_id,
                    )
                    .with_for_update()
                )

                metadata_payload = {
                    "external_id": asset.external_id,
                    "date_source": asset.date_source.value,
                    "asset_type": asset.asset_type.value,
                    "description": asset.description,
                    "albums": [{"id": a.id, "name": a.name} for a in asset.albums],
                    "people": [{"id": p.id, "name": p.name} for p in asset.people],
                }

                if item is None:
                    # Nuovo asset mai visto
                    item_id = uuid.uuid4()
                    item = SourceItem(
                        id=item_id,
                        connector_account_id=account_id,
                        source_kind=IMMICH_SOURCE_KIND,
                        external_id=asset.external_id,
                        canonical_hash=asset.canonical_hash,
                        occurred_at=asset.occurred_at,
                        source_state=SourceItemState.ACTIVE,
                        last_observed_at=now,
                    )
                    self._session.add(item)
                    revision = SourceRevision(
                        id=uuid.uuid4(),
                        source_item_id=item_id,
                        revision_number=1,
                        content_hash=asset.canonical_hash,
                        normalized_metadata=metadata_payload,
                        normalized_content={
                            "asset_id": asset.external_id,
                            "type": asset.asset_type.value,
                            "description": asset.description or "",
                            "albums": [a.name for a in asset.albums],
                            "people": [p.name for p in asset.people],
                        },
                        evidence=asset.provenance_payload,
                        pipeline_version="immich-meta-v1",
                        captured_at=now,
                    )
                    self._session.add(revision)
                    inserted_count += 1
                else:
                    item.last_observed_at = now
                    item.occurred_at = asset.occurred_at
                    if item.canonical_hash == asset.canonical_hash:
                        # Metadati identici: nessun drift, nessun duplicato di revisione
                        duplicate_count += 1
                    else:
                        # Metadati modificati nell'istanza remota: nuova revisione
                        max_rev = await self._session.scalar(
                            select(func.max(SourceRevision.revision_number)).where(
                                SourceRevision.source_item_id == item.id
                            )
                        )
                        next_rev = (max_rev or 1) + 1
                        item.canonical_hash = asset.canonical_hash
                        revision = SourceRevision(
                            id=uuid.uuid4(),
                            source_item_id=item.id,
                            revision_number=next_rev,
                            content_hash=asset.canonical_hash,
                            normalized_metadata=metadata_payload,
                            normalized_content={
                                "asset_id": asset.external_id,
                                "type": asset.asset_type.value,
                                "description": asset.description or "",
                                "albums": [a.name for a in asset.albums],
                                "people": [p.name for p in asset.people],
                            },
                            evidence=asset.provenance_payload,
                            pipeline_version="immich-meta-v1",
                            captured_at=now,
                        )
                        self._session.add(revision)
                        duplicate_count += 1

            if job_id is not None:
                job = await self._session.get(ImportJob, job_id)
                if job is not None:
                    job.imported_items = (job.imported_items or 0) + inserted_count
                    job.duplicate_items = (job.duplicate_items or 0) + duplicate_count
                    job.processed_items = (job.processed_items or 0) + len(assets)
                    if checkpoint is not None:
                        job.checkpoint = checkpoint

        return inserted_count, duplicate_count

    async def count_imported_assets(
        self,
        owner_id: uuid.UUID,
        *,
        account_id: uuid.UUID | None = None,
    ) -> int:
        """Restituisce il conteggio autorevole degli asset Immich attivi a database."""
        query = (
            select(func.count(SourceItem.id))
            .join(ConnectorAccount, ConnectorAccount.id == SourceItem.connector_account_id)
            .where(
                ConnectorAccount.owner_id == owner_id,
                SourceItem.source_kind == IMMICH_SOURCE_KIND,
                SourceItem.source_state == SourceItemState.ACTIVE,
            )
        )
        if account_id is not None:
            query = query.where(SourceItem.connector_account_id == account_id)
        val = await self._session.scalar(query)
        return int(val or 0)

    async def list_imported_assets(
        self,
        owner_id: uuid.UUID,
        *,
        account_id: uuid.UUID | None = None,
        limit: int = 50,
    ) -> tuple[dict[str, Any], ...]:
        """Elenca gli asset Immich importati con i relativi metadati dell'ultima revisione."""
        latest_rev = (
            select(
                SourceRevision.source_item_id.label("source_item_id"),
                func.max(SourceRevision.revision_number).label("max_rev"),
            )
            .group_by(SourceRevision.source_item_id)
            .subquery()
        )

        query = (
            select(SourceItem, SourceRevision)
            .join(ConnectorAccount, ConnectorAccount.id == SourceItem.connector_account_id)
            .join(latest_rev, latest_rev.c.source_item_id == SourceItem.id)
            .join(
                SourceRevision,
                (SourceRevision.source_item_id == SourceItem.id)
                & (SourceRevision.revision_number == latest_rev.c.max_rev),
            )
            .where(
                ConnectorAccount.owner_id == owner_id,
                SourceItem.source_kind == IMMICH_SOURCE_KIND,
                SourceItem.source_state == SourceItemState.ACTIVE,
            )
        )
        if account_id is not None:
            query = query.where(SourceItem.connector_account_id == account_id)

        rows = await self._session.execute(
            query.order_by(desc(SourceItem.occurred_at)).limit(limit)
        )
        results = []
        for row in rows:
            item: SourceItem = row[0]
            rev: SourceRevision = row[1]
            meta = rev.normalized_metadata or {}
            results.append(
                {
                    "source_item_id": str(item.id),
                    "external_id": item.external_id,
                    "occurred_at": item.occurred_at.isoformat() if item.occurred_at else None,
                    "date_source": meta.get("date_source", "unknown"),
                    "asset_type": meta.get("asset_type", "IMAGE"),
                    "description": meta.get("description"),
                    "albums": meta.get("albums", []),
                    "people": meta.get("people", []),
                    "evidence": rev.evidence,
                }
            )
        return tuple(results)
