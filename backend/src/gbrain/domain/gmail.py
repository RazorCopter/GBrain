from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.headerregistry import Address
from email.utils import getaddresses
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from gbrain.domain.connectors import PageState

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SENT_LABEL = "SENT"
GMAIL_SOURCE_KIND = "gmail_sent"
PIPELINE_VERSION = "gmail-sent-v1"
MAX_PAGE_SIZE = 500

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class GmailSyncMode(StrEnum):
    FULL = "full"
    INCREMENTAL = "incremental"
    RECONCILIATION = "reconciliation"


class GmailReconciliationState(StrEnum):
    NOT_REQUIRED = "not_required"
    REQUIRED_HISTORY_EXPIRED = "required_history_expired"
    RUNNING = "running"
    COMPLETE = "complete"


class GmailItemState(StrEnum):
    ACTIVE = "active"
    REMOVED = "removed"
    OUT_OF_SCOPE = "out_of_scope"
    EXCLUDED = "excluded"


class AttributionConfidence(StrEnum):
    CONFIRMED_ALIAS = "confirmed_alias"
    AMBIGUOUS = "ambiguous"


class DatePrecision(StrEnum):
    SECOND = "second"
    MILLISECOND = "millisecond"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class GmailSelection:
    after: datetime
    before: datetime
    limit: int
    page_size: int = 100

    def __post_init__(self) -> None:
        if self.after.tzinfo is None or self.before.tzinfo is None:
            raise ValueError("L'intervallo Gmail richiede datetime timezone-aware")
        if self.after >= self.before:
            raise ValueError("after deve precedere before")
        if self.limit < 1:
            raise ValueError("limit deve essere positivo")
        if not 1 <= self.page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size deve essere compreso tra 1 e {MAX_PAGE_SIZE}")


@dataclass(frozen=True, slots=True)
class GmailCursor:
    page_token: str | None = None
    emitted_items: int = 0

    def __post_init__(self) -> None:
        if self.emitted_items < 0:
            raise ValueError("emitted_items non può essere negativo")


@dataclass(frozen=True, slots=True)
class GmailLocator:
    message_id: str
    thread_id: str | None = None
    history_id: str | None = None

    def __post_init__(self) -> None:
        if not self.message_id:
            raise ValueError("message_id Gmail mancante")


@dataclass(frozen=True, slots=True)
class GmailHistoryCursor:
    start_history_id: str
    page_token: str | None = None


@dataclass(frozen=True, slots=True)
class GmailHistoryPage:
    locators: tuple[GmailLocator, ...]
    deleted_message_ids: tuple[str, ...]
    next_cursor: GmailHistoryCursor | None
    latest_history_id: str
    state: PageState


@dataclass(frozen=True, slots=True)
class GmailRawMessage:
    message_id: str
    thread_id: str | None
    history_id: str | None
    label_ids: tuple[str, ...]
    internal_date_ms: int | None
    snippet: str | None
    payload: Mapping[str, Any]
    size_estimate: int | None

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> GmailRawMessage:
        message_id = str(data.get("id") or "")
        if not message_id:
            raise ValueError("Risposta Gmail priva di id")
        payload = data.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("Risposta Gmail priva di payload MIME strutturato")
        label_ids_value = data.get("labelIds", ())
        if not isinstance(label_ids_value, (list, tuple)):
            raise ValueError("labelIds Gmail non valido")
        internal_date_value = data.get("internalDate")
        size_value = data.get("sizeEstimate")
        return cls(
            message_id=message_id,
            thread_id=_optional_string(data.get("threadId")),
            history_id=_optional_string(data.get("historyId")),
            label_ids=tuple(str(item) for item in label_ids_value),
            internal_date_ms=int(internal_date_value) if internal_date_value is not None else None,
            snippet=_optional_string(data.get("snippet")),
            payload=MappingProxyType(dict(payload)),
            size_estimate=int(size_value) if size_value is not None else None,
        )


@dataclass(frozen=True, slots=True)
class GmailHeader:
    name: str
    value: str


@dataclass(frozen=True, slots=True)
class AttachmentReference:
    part_id: str
    filename: str
    mime_type: str
    size: int
    attachment_id: str | None
    inline: bool
    content_id: str | None
    downloaded: bool = False


@dataclass(frozen=True, slots=True)
class BodyEvidence:
    representation: str
    source_part_ids: tuple[str, ...]
    content_hash: str
    truncation_codes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class NormalizedGmailMessage:
    external_id: str
    thread_id: str | None
    history_id: str | None
    rfc_message_id: str | None
    subject: str | None
    from_addresses: tuple[str, ...]
    to_addresses: tuple[str, ...]
    cc_addresses: tuple[str, ...]
    original_date: datetime | None
    original_date_precision: DatePrecision
    original_date_provenance: str
    internal_date: datetime | None
    acquired_at: datetime
    snippet: str | None
    body_new: str | None
    body_signature: str | None
    body_quoted: str | None
    body_forwarded: str | None
    body_plain_original: str | None
    body_html_text: str | None
    attachments: tuple[AttachmentReference, ...]
    useful_headers: tuple[GmailHeader, ...]
    attribution: AttributionConfidence
    attributed_owner_alias: str | None
    attribution_evidence: tuple[str, ...]
    evidence: BodyEvidence
    revision_hash: str
    pipeline_version: str
    gmail_link: str
    untrusted_content: bool = True

    @property
    def idempotency_key(self) -> str:
        material = (
            f"{GMAIL_SOURCE_KIND}\0{self.external_id}\0{self.revision_hash}"
            f"\0{self.pipeline_version}"
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def provenance_payload(self) -> dict[str, Any]:
        return {
            "source_kind": GMAIL_SOURCE_KIND,
            "external_id": self.external_id,
            "thread_id": self.thread_id,
            "history_id": self.history_id,
            "rfc_message_id": self.rfc_message_id,
            "subject": self.subject,
            "from": list(self.from_addresses),
            "to": list(self.to_addresses),
            "cc": list(self.cc_addresses),
            "original_date": self.original_date.isoformat() if self.original_date else None,
            "original_date_precision": self.original_date_precision,
            "original_date_provenance": self.original_date_provenance,
            "internal_date": self.internal_date.isoformat() if self.internal_date else None,
            "acquired_at": self.acquired_at.isoformat(),
            "snippet": self.snippet,
            "body_parts": {
                "new": self.body_new,
                "signature": self.body_signature,
                "quoted": self.body_quoted,
                "forwarded": self.body_forwarded,
                "plain_original": self.body_plain_original,
                "html_text": self.body_html_text,
            },
            "attachments": [
                {
                    "part_id": item.part_id,
                    "filename": item.filename,
                    "mime_type": item.mime_type,
                    "size": item.size,
                    "attachment_id": item.attachment_id,
                    "inline": item.inline,
                    "content_id": item.content_id,
                    "downloaded": item.downloaded,
                }
                for item in self.attachments
            ],
            "useful_headers": [
                {"name": header.name, "value": header.value} for header in self.useful_headers
            ],
            "attribution": self.attribution,
            "attributed_owner_alias": self.attributed_owner_alias,
            "attribution_evidence": list(self.attribution_evidence),
            "evidence": {
                "representation": self.evidence.representation,
                "source_part_ids": list(self.evidence.source_part_ids),
                "content_hash": self.evidence.content_hash,
                "truncation_codes": list(self.evidence.truncation_codes),
            },
            "revision_hash": self.revision_hash,
            "pipeline_version": self.pipeline_version,
            "gmail_link": self.gmail_link,
            "untrusted_content": True,
        }


@dataclass(frozen=True, slots=True)
class GmailCheckpointState:
    account_id: str
    policy_id: str
    history_id: str | None
    page_token: str | None
    coverage_after: datetime | None
    coverage_before: datetime | None
    last_synced_at: datetime | None
    reconciliation_state: GmailReconciliationState
    last_error_code: str | None = None
    version: int = 1

    def require_full_reconciliation(self, error_code: str) -> GmailCheckpointState:
        return GmailCheckpointState(
            account_id=self.account_id,
            policy_id=self.policy_id,
            history_id=self.history_id,
            page_token=self.page_token,
            coverage_after=self.coverage_after,
            coverage_before=self.coverage_before,
            last_synced_at=self.last_synced_at,
            reconciliation_state=GmailReconciliationState.REQUIRED_HISTORY_EXPIRED,
            last_error_code=error_code,
            version=self.version,
        )


@dataclass(frozen=True, slots=True)
class GmailPersistOutcome:
    inserted: bool
    revised: bool
    unchanged: bool
    source_item_id: str
    revision_id: str | None


@dataclass(frozen=True, slots=True)
class GmailSyncResult:
    mode: GmailSyncMode
    persisted_items: int
    revised_items: int
    unchanged_items: int
    rejected_items: int
    removed_items: int
    pages: int
    state: PageState
    checkpoint: GmailCheckpointState
    detail_codes: tuple[str, ...] = field(default_factory=tuple)


def normalize_email_address(value: str) -> str | None:
    parsed = getaddresses([value])
    if len(parsed) != 1:
        return None
    address = parsed[0][1].strip().casefold()
    if not _EMAIL_RE.fullmatch(address):
        return None
    return address


def confirmed_aliases(values: tuple[str, ...] | list[str]) -> frozenset[str]:
    normalized = {normalized for value in values if (normalized := normalize_email_address(value))}
    if not normalized:
        raise ValueError("È richiesto almeno un alias owner confermato")
    return frozenset(normalized)


def stable_revision_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_gmail_link(message_id: str) -> str:
    if not message_id or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        for character in message_id
    ):
        raise ValueError("Identificativo Gmail non valido per il link")
    return f"https://mail.google.com/mail/u/0/#sent/{message_id}"


def millisecond_datetime(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def address_header_values(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    result: list[str] = []
    for _, raw_address in getaddresses([value]):
        try:
            normalized = str(Address(addr_spec=raw_address)).casefold()
        except (TypeError, ValueError):
            continue
        if _EMAIL_RE.fullmatch(normalized):
            result.append(normalized)
    return tuple(dict.fromkeys(result))


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    result = str(value)
    return result if result else None
