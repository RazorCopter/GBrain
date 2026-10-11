from __future__ import annotations

import base64
import binascii
import hashlib
import html
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, cast

from gbrain.domain.connectors import ConnectorError, ConnectorErrorCategory
from gbrain.domain.gmail import (
    PIPELINE_VERSION,
    AttachmentReference,
    AttributionConfidence,
    BodyEvidence,
    DatePrecision,
    GmailHeader,
    GmailRawMessage,
    NormalizedGmailMessage,
    address_header_values,
    build_gmail_link,
    confirmed_aliases,
    millisecond_datetime,
    stable_revision_hash,
)

_MAX_MESSAGE_BYTES = 25 * 1024 * 1024
_MAX_BODY_BYTES = 5 * 1024 * 1024
_MAX_PARTS = 100
_MAX_DEPTH = 10
_PARSE_TIMEOUT_SECONDS = 30.0
_USEFUL_HEADERS = frozenset(
    {"message-id", "date", "from", "to", "cc", "subject", "reply-to", "in-reply-to", "references"}
)
_FORWARD_MARKERS = (
    "---------- forwarded message ---------",
    "begin forwarded message:",
    "messaggio inoltrato",
)
_SIGNATURE_RE = re.compile(r"(?m)^-- ?\r?\n")
_QUOTE_HEADER_RE = re.compile(
    r"(?im)^(on .+wrote:|il .+ha scritto:|from:\s.+\n(?:sent|date|inviato):\s.+)$"
)


@dataclass(frozen=True, slots=True)
class MimeLimits:
    max_message_bytes: int = _MAX_MESSAGE_BYTES
    max_body_bytes: int = _MAX_BODY_BYTES
    max_parts: int = _MAX_PARTS
    max_depth: int = _MAX_DEPTH
    timeout_seconds: float = _PARSE_TIMEOUT_SECONDS


@dataclass(slots=True)
class _WalkState:
    started_at: float
    parts_seen: int = 0
    bytes_seen: int = 0


@dataclass(frozen=True, slots=True)
class _TextPart:
    part_id: str
    mime_type: str
    text: str


@dataclass(frozen=True, slots=True)
class _ParsedMime:
    headers: tuple[GmailHeader, ...]
    plain_parts: tuple[_TextPart, ...]
    html_parts: tuple[_TextPart, ...]
    attachments: tuple[AttachmentReference, ...]


class _SafeHTMLTextExtractor(HTMLParser):
    _BLOCK_TAGS = frozenset(
        {
            "address",
            "article",
            "aside",
            "blockquote",
            "br",
            "div",
            "footer",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "header",
            "hr",
            "li",
            "main",
            "ol",
            "p",
            "pre",
            "section",
            "table",
            "tr",
            "ul",
        }
    )
    _IGNORED_TAGS = frozenset(
        {"script", "style", "svg", "template", "noscript", "iframe", "object"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        normalized = tag.casefold()
        if normalized in self._IGNORED_TAGS:
            self._ignored_depth += 1
        elif self._ignored_depth == 0 and normalized in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.casefold() in self._IGNORED_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.casefold()
        if normalized in self._IGNORED_TAGS:
            self._ignored_depth = max(0, self._ignored_depth - 1)
        elif self._ignored_depth == 0 and normalized in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0:
            self._chunks.append(data)

    def text(self) -> str:
        joined = "".join(self._chunks).replace("\xa0", " ")
        joined = re.sub(r"[ \t]+", " ", joined)
        joined = re.sub(r" *\n *", "\n", joined)
        joined = re.sub(r"\n{3,}", "\n\n", joined)
        return joined.strip()


def normalize_gmail_message(
    raw: GmailRawMessage,
    owner_aliases: Sequence[str],
    *,
    acquired_at: datetime | None = None,
    limits: MimeLimits | None = None,
    monotonic: Any = time.monotonic,
) -> NormalizedGmailMessage:
    effective_limits = limits or MimeLimits()
    if raw.size_estimate is not None and raw.size_estimate > effective_limits.max_message_bytes:
        raise _parse_error("MESSAGE_TOO_LARGE")
    parsed = _parse_payload(raw.payload, effective_limits, monotonic)
    header_map = {header.name.casefold(): header.value for header in parsed.headers}
    from_addresses = address_header_values(header_map.get("from"))
    to_addresses = address_header_values(header_map.get("to"))
    cc_addresses = address_header_values(header_map.get("cc"))
    aliases = confirmed_aliases(list(owner_aliases))
    matched_aliases = tuple(address for address in from_addresses if address in aliases)
    if len(from_addresses) == 1 and len(matched_aliases) == 1:
        attribution = AttributionConfidence.CONFIRMED_ALIAS
        attributed_alias = matched_aliases[0]
        attribution_evidence = ("from_matches_confirmed_alias",)
    else:
        attribution = AttributionConfidence.AMBIGUOUS
        attributed_alias = None
        attribution_evidence = ("from_not_uniquely_confirmed",)

    plain_original = _join_parts(parsed.plain_parts)
    html_text = _join_parts(parsed.html_parts)
    preferred = plain_original or html_text
    if preferred is None:
        raise _parse_error("BODY_TEXT_UNAVAILABLE")
    new_text, signature, quoted, forwarded = split_authored_text(preferred)
    if not new_text and not any((signature, quoted, forwarded)):
        raise _parse_error("BODY_TEXT_EMPTY_AFTER_NORMALIZATION")

    date, precision, provenance = _original_date(
        header_map.get("date"), millisecond_datetime(raw.internal_date_ms)
    )
    internal_date = millisecond_datetime(raw.internal_date_ms)
    useful_headers = tuple(
        header for header in parsed.headers if header.name.casefold() in _USEFUL_HEADERS
    )
    source_parts = parsed.plain_parts if plain_original else parsed.html_parts
    evidence_text = plain_original or html_text or ""
    evidence = BodyEvidence(
        representation="text/plain" if plain_original else "text/html-derived",
        source_part_ids=tuple(part.part_id for part in source_parts),
        content_hash=hashlib.sha256(evidence_text.encode("utf-8")).hexdigest(),
    )
    normalized_for_hash: dict[str, Any] = {
        "external_id": raw.message_id,
        "thread_id": raw.thread_id,
        "history_id": raw.history_id,
        "rfc_message_id": header_map.get("message-id"),
        "headers": [(header.name.casefold(), header.value) for header in useful_headers],
        "internal_date_ms": raw.internal_date_ms,
        "snippet": raw.snippet,
        "plain": plain_original,
        "html_text": html_text,
        "attachments": [
            (
                attachment.part_id,
                attachment.filename,
                attachment.mime_type,
                attachment.size,
                attachment.attachment_id,
            )
            for attachment in parsed.attachments
        ],
        "pipeline_version": PIPELINE_VERSION,
    }
    return NormalizedGmailMessage(
        external_id=raw.message_id,
        thread_id=raw.thread_id,
        history_id=raw.history_id,
        rfc_message_id=header_map.get("message-id"),
        subject=header_map.get("subject"),
        from_addresses=from_addresses,
        to_addresses=to_addresses,
        cc_addresses=cc_addresses,
        original_date=date,
        original_date_precision=precision,
        original_date_provenance=provenance,
        internal_date=internal_date,
        acquired_at=acquired_at or datetime.now(UTC),
        snippet=raw.snippet,
        body_new=new_text or None,
        body_signature=signature,
        body_quoted=quoted,
        body_forwarded=forwarded,
        body_plain_original=plain_original,
        body_html_text=html_text,
        attachments=parsed.attachments,
        useful_headers=useful_headers,
        attribution=attribution,
        attributed_owner_alias=attributed_alias,
        attribution_evidence=attribution_evidence,
        evidence=evidence,
        revision_hash=stable_revision_hash(normalized_for_hash),
        pipeline_version=PIPELINE_VERSION,
        gmail_link=build_gmail_link(raw.message_id),
    )


def split_authored_text(text: str) -> tuple[str, str | None, str | None, str | None]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    forwarded: str | None = None
    folded = normalized.casefold()
    forward_indexes = [folded.find(marker) for marker in _FORWARD_MARKERS]
    forward_indexes = [index for index in forward_indexes if index >= 0]
    if forward_indexes:
        index = min(forward_indexes)
        forwarded = normalized[index:].strip() or None
        normalized = normalized[:index].rstrip()

    quoted: str | None = None
    quote_match = _QUOTE_HEADER_RE.search(normalized)
    if quote_match:
        quoted = normalized[quote_match.start() :].strip() or None
        normalized = normalized[: quote_match.start()].rstrip()
    else:
        lines = normalized.splitlines()
        first_quote = next(
            (index for index, line in enumerate(lines) if line.lstrip().startswith(">")), None
        )
        if first_quote is not None:
            quoted = "\n".join(lines[first_quote:]).strip() or None
            normalized = "\n".join(lines[:first_quote]).rstrip()

    signature: str | None = None
    signature_match = _SIGNATURE_RE.search(normalized)
    if signature_match:
        signature = normalized[signature_match.start() :].strip() or None
        normalized = normalized[: signature_match.start()].rstrip()
    return normalized.strip(), signature, quoted, forwarded


def html_to_safe_text(value: str) -> str:
    parser = _SafeHTMLTextExtractor()
    try:
        parser.feed(value)
        parser.close()
    except (ValueError, html.parser.HTMLParseError) as exc:  # type: ignore[attr-defined]
        raise _parse_error("HTML_PARSE_ERROR") from exc
    return parser.text()


def _parse_payload(payload: Mapping[str, Any], limits: MimeLimits, monotonic: Any) -> _ParsedMime:
    state = _WalkState(started_at=float(monotonic()))
    plain_parts: list[_TextPart] = []
    html_parts: list[_TextPart] = []
    attachments: list[AttachmentReference] = []
    headers: tuple[GmailHeader, ...] = ()

    def walk(part: Mapping[str, Any], depth: int, fallback_id: str) -> None:
        nonlocal headers
        _check_limits(state, limits, monotonic, depth)
        state.parts_seen += 1
        part_id = str(part.get("partId") or fallback_id)
        mime_type = str(part.get("mimeType") or "application/octet-stream").casefold()
        if depth == 0:
            headers = _decode_headers(part.get("headers"))
        body = part.get("body", {})
        body_mapping = cast(Mapping[str, Any], body) if isinstance(body, Mapping) else {}
        filename = _decode_header_value(str(part.get("filename") or ""))
        attachment_id = _optional(body_mapping.get("attachmentId"))
        size = int(body_mapping.get("size") or 0)
        part_headers = _decode_headers(part.get("headers"))
        part_header_map = {item.name.casefold(): item.value for item in part_headers}
        content_id = part_header_map.get("content-id")
        disposition = part_header_map.get("content-disposition", "").casefold()
        if filename or attachment_id:
            if size > limits.max_message_bytes:
                raise _parse_error("ATTACHMENT_METADATA_SIZE_LIMIT")
            attachments.append(
                AttachmentReference(
                    part_id=part_id,
                    filename=filename,
                    mime_type=mime_type,
                    size=size,
                    attachment_id=attachment_id,
                    inline="inline" in disposition,
                    content_id=content_id,
                )
            )
        elif mime_type in {"text/plain", "text/html"}:
            data = body_mapping.get("data")
            if data is not None:
                decoded = _decode_base64url(str(data))
                state.bytes_seen += len(decoded)
                if state.bytes_seen > limits.max_body_bytes:
                    raise _parse_error("BODY_SIZE_LIMIT")
                text = _decode_text(decoded, part_header_map.get("content-type") or mime_type)
                if mime_type == "text/html":
                    html_parts.append(_TextPart(part_id, mime_type, html_to_safe_text(text)))
                else:
                    plain_parts.append(_TextPart(part_id, mime_type, text.strip()))
        child_parts = part.get("parts", ())
        if child_parts:
            if not isinstance(child_parts, list):
                raise _parse_error("MIME_PARTS_SCHEMA_INVALID")
            for index, child in enumerate(child_parts):
                if not isinstance(child, Mapping):
                    raise _parse_error("MIME_PART_SCHEMA_INVALID")
                walk(cast(Mapping[str, Any], child), depth + 1, f"{part_id}.{index}")

    walk(payload, 0, "0")
    return _ParsedMime(headers, tuple(plain_parts), tuple(html_parts), tuple(attachments))


def _check_limits(state: _WalkState, limits: MimeLimits, monotonic: Any, depth: int) -> None:
    if depth > limits.max_depth:
        raise _parse_error("MIME_DEPTH_LIMIT")
    if state.parts_seen >= limits.max_parts:
        raise _parse_error("MIME_PART_COUNT_LIMIT")
    if float(monotonic()) - state.started_at > limits.timeout_seconds:
        raise _parse_error("MIME_PARSE_TIMEOUT")


def _decode_headers(value: object) -> tuple[GmailHeader, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise _parse_error("MIME_HEADERS_SCHEMA_INVALID")
    result: list[GmailHeader] = []
    for raw_header in value:
        if not isinstance(raw_header, Mapping):
            raise _parse_error("MIME_HEADER_SCHEMA_INVALID")
        name = str(raw_header.get("name") or "").strip()
        raw_value = str(raw_header.get("value") or "")
        if name and "\n" not in name and "\r" not in name:
            result.append(GmailHeader(name, _decode_header_value(raw_value)))
    return tuple(result)


def _decode_header_value(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeError, ValueError) as exc:
        raise _parse_error("HEADER_DECODE_ERROR") from exc


def _decode_base64url(value: str) -> bytes:
    try:
        encoded = value.encode("ascii")
        encoded += b"=" * (-len(encoded) % 4)
        return base64.b64decode(encoded, altchars=b"-_", validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise _parse_error("BASE64URL_DECODE_ERROR") from exc


def _decode_text(value: bytes, content_type: str) -> str:
    match = re.search(r"charset\s*=\s*[\"']?([^;\s\"']+)", content_type, re.IGNORECASE)
    declared = match.group(1) if match else None
    charsets = [item for item in (declared, "utf-8", "latin-1") if item]
    seen: set[str] = set()
    for charset in charsets:
        normalized = charset.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        try:
            return value.decode(charset)
        except (LookupError, UnicodeDecodeError):
            continue
    raise _parse_error("CHARSET_ERROR")


def _original_date(
    header_value: str | None, internal_date: datetime | None
) -> tuple[datetime | None, DatePrecision, str]:
    if header_value:
        try:
            parsed = parsedate_to_datetime(header_value)
        except (TypeError, ValueError, OverflowError):
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                return (
                    parsed.replace(tzinfo=UTC),
                    DatePrecision.SECOND,
                    "date_header_timezone_assumed_utc",
                )
            return parsed, DatePrecision.SECOND, "date_header"
    if internal_date is not None:
        return internal_date, DatePrecision.MILLISECOND, "gmail_internal_date"
    return None, DatePrecision.UNKNOWN, "missing"


def _join_parts(parts: Sequence[_TextPart]) -> str | None:
    values = [part.text.strip() for part in parts if part.text.strip()]
    return "\n\n".join(values) if values else None


def _parse_error(code: str) -> ConnectorError:
    return ConnectorError(ConnectorErrorCategory.PARSE, f"Parsing MIME fallito: {code}")


def _optional(value: object) -> str | None:
    if value is None:
        return None
    result = str(value)
    return result or None
