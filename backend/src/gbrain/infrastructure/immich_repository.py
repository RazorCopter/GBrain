from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gbrain.domain.immich import (
    IMMICH_PIPELINE_VERSION,
    IMMICH_SOURCE_KIND,
    ImmichNormalizedAsset,
    compute_immich_v1_canonical_hash,
    has_new_v2_semantic_metadata,
)
from gbrain.domain.models import (
    ConnectorAccount,
    ImportJob,
    SourceItem,
    SourceItemState,
    SourceRevision,
)


@dataclass(frozen=True, slots=True)
class ImmichBatchOutcome:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    invalid: int = 0
    missing: int = 0

    def __iter__(self) -> Iterator[int]:
        """Compatibilità con i call site v1: il secondo valore era «item già esistente»."""
        yield self.inserted
        yield self.updated + self.unchanged


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
        invalid_count: int = 0,
    ) -> ImmichBatchOutcome:
        """Salva atomicamente asset e checkpoint, distinguendo gli esiti semantici."""
        inserted_count = 0
        updated_count = 0
        unchanged_count = 0
        total_invalid = invalid_count
        valid_assets: list[ImmichNormalizedAsset] = []
        now = datetime.now(UTC)

        for asset in assets:
            if not isinstance(asset, ImmichNormalizedAsset) or not asset.external_id:
                total_invalid += 1
                continue
            valid_assets.append(asset)

        async with self._session.begin():
            for asset in valid_assets:
                item = await self._session.scalar(
                    select(SourceItem)
                    .where(
                        SourceItem.connector_account_id == account_id,
                        SourceItem.source_kind == IMMICH_SOURCE_KIND,
                        SourceItem.external_id == asset.external_id,
                    )
                    .with_for_update()
                )

                metadata_payload = asset.normalized_metadata()

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
                        pipeline_version=IMMICH_PIPELINE_VERSION,
                        captured_at=now,
                    )
                    self._session.add(revision)
                    inserted_count += 1
                else:
                    item.last_observed_at = now
                    item.occurred_at = asset.occurred_at

                    # Valutazione semantica v1→v2:
                    # 1. Se l'hash canonico coincide esattamente, l'asset è invariato.
                    # 2. Se l'hash differisce, verifichiamo se l'item salvato proviene da una
                    #    pipeline precedente v1 e l'asset attuale non introduce alcun metadato v2
                    #    nuovo oltre a quelli già presenti in v1. In tal caso, nessun cambiamento
                    #    semantico si è verificato e non deve essere generata alcuna revisione.
                    is_semantic_change = True
                    if item.canonical_hash == asset.canonical_hash:
                        is_semantic_change = False
                    else:
                        v1_hash = compute_immich_v1_canonical_hash(
                            external_id=asset.external_id,
                            occurred_at=asset.occurred_at,
                            date_source=asset.date_source,
                            asset_type=asset.asset_type,
                            albums=asset.albums,
                            description=asset.description,
                            people=asset.people,
                        )
                        if item.canonical_hash == v1_hash and not has_new_v2_semantic_metadata(
                            asset
                        ):
                            is_semantic_change = False

                    if not is_semantic_change:
                        unchanged_count += 1
                    else:
                        # Metadati modificati nell'istanza remota o nuovi metadati v2: nuova revisione
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
                            pipeline_version=IMMICH_PIPELINE_VERSION,
                            captured_at=now,
                        )
                        self._session.add(revision)
                        updated_count += 1

            if job_id is not None:
                job = await self._session.get(ImportJob, job_id)
                if job is not None:
                    job.imported_items = (job.imported_items or 0) + inserted_count + updated_count
                    job.duplicate_items = (job.duplicate_items or 0) + unchanged_count
                    job.not_imported_items = (job.not_imported_items or 0) + total_invalid
                    job.processed_items = (
                        (job.processed_items or 0) + len(valid_assets) + total_invalid
                    )
                    if checkpoint is not None:
                        job.checkpoint = {
                            **checkpoint,
                            "total_enumerated": job.processed_items,
                            "total_inserted": int(checkpoint.get("total_inserted", 0))
                            + inserted_count,
                            "total_updated": int(checkpoint.get("total_updated", 0))
                            + updated_count,
                            "total_unchanged": int(
                                checkpoint.get(
                                    "total_unchanged", checkpoint.get("total_duplicates", 0)
                                )
                            )
                            + unchanged_count,
                            "total_duplicates": job.duplicate_items,
                            "total_invalid": int(checkpoint.get("total_invalid", 0))
                            + total_invalid,
                            "total_missing": 0,
                            "missing_evaluation": "not_evaluated",
                        }

        return ImmichBatchOutcome(
            inserted=inserted_count,
            updated=updated_count,
            unchanged=unchanged_count,
            invalid=total_invalid,
            missing=0,
        )

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
