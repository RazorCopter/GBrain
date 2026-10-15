from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

IMMICH_SOURCE_KIND = "immich"
IMMICH_MAX_ITEMS_LIMIT = 25


class ImmichDateSource(StrEnum):
    LOCAL_DATE_TIME = "localDateTime"
    DATE_TIME_ORIGINAL = "dateTimeOriginal"
    FILE_CREATED_AT = "fileCreatedAt"
    UPDATED_AT = "updatedAt"
    UNKNOWN = "unknown"


class ImmichAssetType(StrEnum):
    IMAGE = "IMAGE"
    VIDEO = "VIDEO"
    OTHER = "OTHER"


class ImmichErrorCategory(StrEnum):
    AUTH = "auth"
    UNSUPPORTED_VERSION = "unsupported_version"
    MISSING_CAPABILITY = "missing_capability"
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    BUDGET = "budget"
    PARSE = "parse"


class ImmichGatewayError(RuntimeError):
    def __init__(
        self,
        category: ImmichErrorCategory,
        reason_code: str,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.reason_code = reason_code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ImmichAlbumInfo:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class ImmichPerson:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class ImmichSelection:
    album_id: str | None = None
    after: datetime | None = None
    before: datetime | None = None
    item_limit: int = IMMICH_MAX_ITEMS_LIMIT

    def __post_init__(self) -> None:
        if self.item_limit < 1 or self.item_limit > IMMICH_MAX_ITEMS_LIMIT:
            raise ValueError(f"item_limit deve essere compreso tra 1 e {IMMICH_MAX_ITEMS_LIMIT}")
        if self.album_id is not None:
            if not self.album_id.strip():
                raise ValueError("album_id non può essere una stringa vuota")
            if self.after is not None or self.before is not None:
                raise ValueError(
                    "La selezione per album e la selezione temporale sono mutuamente esclusive"
                )
        else:
            if self.after is None and self.before is None:
                raise ValueError(
                    "Specificare un album_id oppure un intervallo temporale (after/before)"
                )
            if self.after is not None and self.before is not None and self.after >= self.before:
                raise ValueError("after deve essere temporalmente antecedente a before")


@dataclass(frozen=True, slots=True)
class ImmichLocator:
    asset_id: str


@dataclass(frozen=True, slots=True)
class ImmichCapabilities:
    server_version: str
    version_supported: bool
    supports_albums: bool
    supports_people: bool
    supports_search: bool


@dataclass(frozen=True, slots=True)
class ImmichNormalizedAsset:
    external_id: str
    occurred_at: datetime
    date_source: ImmichDateSource
    asset_type: ImmichAssetType
    albums: tuple[ImmichAlbumInfo, ...]
    description: str | None
    people: tuple[ImmichPerson, ...]
    canonical_hash: str
    provenance_payload: dict[str, Any]


def _parse_immich_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def normalize_immich_asset(
    asset: dict[str, Any],
    *,
    albums: Sequence[ImmichAlbumInfo] = (),
) -> ImmichNormalizedAsset:
    """Normalizza un payload API Immich senza I/O o dipendenze infrastrutturali."""
    raw_id = asset.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        raise ValueError("Asset Immich privo di ID stabile")

    raw_exif = asset.get("exifInfo")
    exif: dict[str, Any] = raw_exif if isinstance(raw_exif, dict) else {}
    date_candidates = (
        (ImmichDateSource.LOCAL_DATE_TIME, asset.get("localDateTime")),
        (
            ImmichDateSource.DATE_TIME_ORIGINAL,
            exif.get("dateTimeOriginal") or asset.get("dateTimeOriginal"),
        ),
        (ImmichDateSource.FILE_CREATED_AT, asset.get("fileCreatedAt")),
        (ImmichDateSource.UPDATED_AT, asset.get("updatedAt")),
    )
    occurred_at: datetime | None = None
    date_source = ImmichDateSource.UNKNOWN
    raw_selected_date: str | None = None
    for candidate_source, candidate_value in date_candidates:
        parsed = _parse_immich_datetime(candidate_value)
        if parsed is not None:
            occurred_at = parsed
            date_source = candidate_source
            raw_selected_date = str(candidate_value)
            break
    if occurred_at is None:
        raise ValueError(f"Asset Immich {raw_id} privo di una data valida")

    raw_type = str(asset.get("type", "OTHER")).upper()
    asset_type = (
        ImmichAssetType(raw_type) if raw_type in {"IMAGE", "VIDEO"} else ImmichAssetType.OTHER
    )
    description_value = exif.get("description") or asset.get("description")
    description = str(description_value).strip() if description_value else None

    people: list[ImmichPerson] = []
    raw_people = asset.get("people")
    if isinstance(raw_people, list):
        for raw_person in raw_people:
            if not isinstance(raw_person, dict) or not raw_person.get("id"):
                continue
            clean_name = str(raw_person.get("name") or "").strip()
            if clean_name:
                people.append(ImmichPerson(id=str(raw_person["id"]), name=clean_name))

    normalized_albums = tuple(albums)
    normalized_people = tuple(people)
    canonical_hash = compute_immich_canonical_hash(
        external_id=raw_id,
        occurred_at=occurred_at,
        date_source=date_source,
        asset_type=asset_type,
        albums=normalized_albums,
        description=description,
        people=normalized_people,
    )
    return ImmichNormalizedAsset(
        external_id=raw_id,
        occurred_at=occurred_at,
        date_source=date_source,
        asset_type=asset_type,
        albums=normalized_albums,
        description=description,
        people=normalized_people,
        canonical_hash=canonical_hash,
        provenance_payload={
            "provider": IMMICH_SOURCE_KIND,
            "external_id": raw_id,
            "selected_date": occurred_at.isoformat(),
            "selected_date_source": date_source.value,
            "selected_date_raw": raw_selected_date,
            "metadata_only": True,
        },
    )


def compute_immich_canonical_hash(
    *,
    external_id: str,
    occurred_at: datetime,
    date_source: ImmichDateSource,
    asset_type: ImmichAssetType,
    albums: Sequence[ImmichAlbumInfo],
    description: str | None,
    people: Sequence[ImmichPerson],
) -> str:
    utc_date = (
        occurred_at.astimezone(UTC).isoformat() if occurred_at.tzinfo else occurred_at.isoformat()
    )
    album_tuples = sorted((a.id, a.name) for a in albums)
    people_tuples = sorted((p.id, p.name) for p in people)
    data = {
        "external_id": external_id,
        "occurred_at": utc_date,
        "date_source": date_source.value,
        "asset_type": asset_type.value,
        "albums": album_tuples,
        "description": description or "",
        "people": people_tuples,
    }
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compute_immich_selection_fingerprint(selection: ImmichSelection) -> str:
    data: dict[str, Any] = {
        "album_id": selection.album_id,
        "after": selection.after.astimezone(UTC).isoformat() if selection.after else None,
        "before": selection.before.astimezone(UTC).isoformat() if selection.before else None,
        "item_limit": selection.item_limit,
    }
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
