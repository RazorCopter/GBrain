from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

IMMICH_SOURCE_KIND = "immich"
IMMICH_API_BATCH_SIZE = 25
IMMICH_MAX_ITEMS_LIMIT = 50_000
IMMICH_DEFAULT_ITEMS_LIMIT = 5_000
IMMICH_DEFAULT_REQUEST_BUDGET = 200
IMMICH_SCHEMA_VERSION = 2
IMMICH_HASH_VERSION = "immich-canonical-v2"
IMMICH_PIPELINE_VERSION = "immich-meta-v2"

IMMICH_HASH_EXCLUDED_KEYS: frozenset[str] = frozenset(
    {
        "remote_updated_at",
        "file_modified_at",
        "exif_modify_date",
        "location_precision",
    }
)

IMMICH_V2_SEMANTIC_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "remote_checksum",
        "original_filename",
        "mime_type",
        "width_px",
        "height_px",
        "duration_ms",
        "rating",
        "file_created_at",
        "local_datetime",
        "exif_datetime_original",
        "exif_timezone",
        "make",
        "model",
        "lens_model",
        "exposure_time",
        "iso",
        "focal_length_mm",
        "aperture_f_number",
        "orientation",
        "city",
        "state",
        "country",
        "favorite",
        "archived",
        "trashed",
        "visibility",
        "duplicate_id",
        "live_photo_video_id",
        "stack_id",
        "stack_primary_asset_id",
    }
)


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
        phase: str | None = None,
        server_version: str | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.reason_code = reason_code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.phase = phase
        self.server_version = server_version


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
    import_all: bool = False
    item_limit: int | None = IMMICH_DEFAULT_ITEMS_LIMIT

    def __post_init__(self) -> None:
        if self.item_limit is not None and (
            self.item_limit < 1 or self.item_limit > IMMICH_MAX_ITEMS_LIMIT
        ):
            raise ValueError(
                f"item_limit deve essere compreso tra 1 e {IMMICH_MAX_ITEMS_LIMIT} oppure null"
            )
        if self.album_id is not None:
            if not self.album_id.strip():
                raise ValueError("album_id non può essere una stringa vuota")
            if self.import_all or self.after is not None or self.before is not None:
                raise ValueError(
                    "La selezione per album, intervallo e intera libreria sono mutuamente esclusive"
                )
        elif self.import_all:
            if self.after is not None or self.before is not None:
                raise ValueError(
                    "La selezione dell'intera libreria e l'intervallo sono mutuamente esclusivi"
                )
        elif self.after is None and self.before is None:
            raise ValueError("Specificare un album_id, un intervallo temporale oppure import_all")
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
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = IMMICH_SCHEMA_VERSION
    hash_version: str = IMMICH_HASH_VERSION

    def normalized_metadata(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "hash_version": self.hash_version,
            "external_id": self.external_id,
            "asset_type": self.asset_type.value,
            "occurred_at": _canonical_datetime(self.occurred_at),
            "occurred_at_source": self.date_source.value,
            "description": self.description,
            "albums": [{"id": a.id, "name": a.name} for a in self.albums],
            "people": [{"id": p.id, "name": p.name} for p in self.people],
            **self.metadata,
        }


def _parse_immich_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _canonical_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.isoformat().replace("+00:00", "Z")


def _clean_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _canonical_decimal(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    normalized = format(number.normalize(), "f")
    return "0" if normalized in {"-0", ""} else normalized


def _uuid_string(value: Any) -> str | None:
    return _clean_string(value)


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
    description = _clean_string(description_value)

    people: list[ImmichPerson] = []
    raw_people = asset.get("people")
    if isinstance(raw_people, list):
        for raw_person in raw_people:
            if not isinstance(raw_person, dict) or not raw_person.get("id"):
                continue
            clean_name = str(raw_person.get("name") or "").strip()
            if clean_name:
                people.append(ImmichPerson(id=str(raw_person["id"]), name=clean_name))

    normalized_albums = tuple(sorted(set(albums), key=lambda value: (value.id, value.name)))
    normalized_people = tuple(sorted(set(people), key=lambda value: (value.id, value.name)))
    gps_present = exif.get("latitude") is not None or exif.get("longitude") is not None
    metadata: dict[str, Any] = {
        "remote_checksum": _clean_string(asset.get("checksum")),
        "original_filename": _clean_string(asset.get("originalFileName")),
        "mime_type": _clean_string(asset.get("originalMimeType")),
        "width_px": _non_negative_int(asset.get("width")),
        "height_px": _non_negative_int(asset.get("height")),
        "duration_ms": _non_negative_int(asset.get("duration")),
        "rating": _non_negative_int(exif.get("rating")),
        "file_created_at": _canonical_datetime(_parse_immich_datetime(asset.get("fileCreatedAt"))),
        "file_modified_at": _canonical_datetime(
            _parse_immich_datetime(asset.get("fileModifiedAt"))
        ),
        "remote_updated_at": _canonical_datetime(_parse_immich_datetime(asset.get("updatedAt"))),
        "local_datetime": _canonical_datetime(_parse_immich_datetime(asset.get("localDateTime"))),
        "exif_datetime_original": _canonical_datetime(
            _parse_immich_datetime(exif.get("dateTimeOriginal"))
        ),
        "exif_modify_date": _canonical_datetime(_parse_immich_datetime(exif.get("modifyDate"))),
        "exif_timezone": _clean_string(exif.get("timeZone")),
        "make": _clean_string(exif.get("make")),
        "model": _clean_string(exif.get("model")),
        "lens_model": _clean_string(exif.get("lensModel")),
        "exposure_time": _canonical_decimal(exif.get("exposureTime")),
        "iso": _non_negative_int(exif.get("iso")),
        "focal_length_mm": _canonical_decimal(exif.get("focalLength")),
        "aperture_f_number": _canonical_decimal(exif.get("fNumber")),
        "orientation": _clean_string(exif.get("orientation")),
        "city": _clean_string(exif.get("city")),
        "state": _clean_string(exif.get("state")),
        "country": _clean_string(exif.get("country")),
        "location_precision": "withheld" if gps_present else "absent",
        "favorite": asset.get("isFavorite") if isinstance(asset.get("isFavorite"), bool) else None,
        "archived": asset.get("isArchived") if isinstance(asset.get("isArchived"), bool) else None,
        "trashed": asset.get("isTrashed") if isinstance(asset.get("isTrashed"), bool) else None,
        "visibility": _clean_string(asset.get("visibility")),
        "duplicate_id": _uuid_string(asset.get("duplicateId")),
        "live_photo_video_id": _uuid_string(asset.get("livePhotoVideoId")),
    }
    raw_stack = asset.get("stack")
    if isinstance(raw_stack, dict):
        metadata["stack_id"] = _uuid_string(raw_stack.get("id"))
        primary = raw_stack.get("primaryAssetId")
        if primary is None and isinstance(raw_stack.get("primaryAsset"), dict):
            primary = raw_stack["primaryAsset"].get("id")
        metadata["stack_primary_asset_id"] = _uuid_string(primary)
    else:
        metadata["stack_id"] = None
        metadata["stack_primary_asset_id"] = None

    # I null sono espliciti e stabili. Coordinate esatte, path, owner/device e payload grezzo esclusi.
    canonical_hash = compute_immich_canonical_hash(
        external_id=raw_id,
        occurred_at=occurred_at,
        date_source=date_source,
        asset_type=asset_type,
        albums=normalized_albums,
        description=description,
        people=normalized_people,
        metadata=metadata,
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
        metadata=metadata,
        provenance_payload={
            "provider": IMMICH_SOURCE_KIND,
            "external_id": raw_id,
            "selected_date": occurred_at.isoformat(),
            "selected_date_source": date_source.value,
            "selected_date_raw": raw_selected_date,
            "metadata_only": True,
        },
    )


def extract_semantic_v2_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Estrae solo i metadati semantici v2 stabili, escludendo campi volatili e tecnici."""
    if not metadata:
        return {}
    cleaned: dict[str, Any] = {}
    for key in sorted(metadata):
        if key in IMMICH_HASH_EXCLUDED_KEYS:
            continue
        val = metadata[key]
        if val is not None and val != "":
            cleaned[key] = val
    return cleaned


def has_new_v2_semantic_metadata(asset: ImmichNormalizedAsset) -> bool:
    """Verifica se l'asset normalizzato contiene almeno un metadato semantico v2 valorizzato."""
    semantic = extract_semantic_v2_metadata(asset.metadata)
    return any(k in IMMICH_V2_SEMANTIC_METADATA_KEYS for k in semantic)


def compute_immich_v1_canonical_hash(
    *,
    external_id: str,
    occurred_at: datetime,
    date_source: ImmichDateSource,
    asset_type: ImmichAssetType,
    albums: Sequence[ImmichAlbumInfo] = (),
    description: str | None = None,
    people: Sequence[ImmichPerson] = (),
) -> str:
    """Calcola l'hash canonico secondo l'esatta funzione v1."""
    utc_date = (
        occurred_at.astimezone(UTC).isoformat() if occurred_at.tzinfo else occurred_at.isoformat()
    )
    album_tuples = sorted((a.id, a.name) for a in albums)
    people_tuples = sorted((p.id, p.name) for p in people)
    data: dict[str, Any] = {
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


def compute_immich_canonical_hash(
    *,
    external_id: str,
    occurred_at: datetime,
    date_source: ImmichDateSource,
    asset_type: ImmichAssetType,
    albums: Sequence[ImmichAlbumInfo] = (),
    description: str | None = None,
    people: Sequence[ImmichPerson] = (),
    metadata: dict[str, Any] | None = None,
) -> str:
    utc_date = (
        occurred_at.astimezone(UTC).isoformat() if occurred_at.tzinfo else occurred_at.isoformat()
    )
    album_tuples = sorted((a.id, a.name) for a in albums)
    people_tuples = sorted((p.id, p.name) for p in people)
    data: dict[str, Any] = {
        "external_id": external_id,
        "occurred_at": utc_date,
        "date_source": date_source.value,
        "asset_type": asset_type.value,
        "albums": album_tuples,
        "description": description or "",
        "people": people_tuples,
    }
    semantic_meta = extract_semantic_v2_metadata(metadata)
    if semantic_meta:
        data["metadata_v2"] = semantic_meta
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
