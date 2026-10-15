from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final


@dataclass(frozen=True, slots=True)
class SourceQueueIdentity:
    """Identità infrastrutturale immutabile assegnata a un tipo di sorgente."""

    queue_name: str
    task_name: str


GMAIL_SOURCE_KIND: Final = "gmail"
IMMICH_SOURCE_KIND: Final = "immich"

GMAIL_QUEUE_IDENTITY: Final = SourceQueueIdentity(
    queue_name="gmail",
    task_name="gbrain.gmail_sync",
)
IMMICH_QUEUE_IDENTITY: Final = SourceQueueIdentity(
    queue_name="immich",
    task_name="gbrain.immich_sync",
)

SOURCE_QUEUE_IDENTITIES: Final = MappingProxyType(
    {
        GMAIL_SOURCE_KIND: GMAIL_QUEUE_IDENTITY,
        IMMICH_SOURCE_KIND: IMMICH_QUEUE_IDENTITY,
    }
)


def get_source_queue_identity(source_kind: str) -> SourceQueueIdentity | None:
    """Restituisce l'identità nota; una sorgente ignota non riceve fallback permissivi."""

    return SOURCE_QUEUE_IDENTITIES.get(source_kind)
