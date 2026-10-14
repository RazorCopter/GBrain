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
from email.errors import HeaderParseError, MessageDefect
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, cast

from gbrain.domain.connectors import GmailMimeError
from gbrain.domain.gmail import (
    PIPELINE_VERSION,
    AttachmentReference,
    AttributionConfidence,
    BodyEvidence,
    DatePrecision,
    GmailHeader,
    GmailRawMessage,
    NormalizedGmailMessage,
    build_gmail_link,
    confirmed_aliases,
    millisecond_datetime,
    parse_address_header,
    stable_revision_hash,
)

# ``sizeEstimate`` include anche gli allegati esterni e non e' un limite di parsing.
# I limiti seguenti si applicano invece ai dati effettivamente materializzati dal parser.
_MAX_BODY_BYTES = 5 * 1024 * 1024
_MAX_PART_BODY_BYTES = 5 * 1024 * 1024
_MAX_PARTS = 100
_MAX_DEPTH = 10
_MAX_ATTACHMENTS = 100
_MAX_ATTACHMENT_NAME_LEN = 255
_MAX_HEADERS_PER_PART = 100
_MAX_HEADER_NAME_LEN = 128
_MAX_HEADER_VALUE_LEN = 16 * 1024
_MAX_METADATA_CHARS = 256 * 1024
_MAX_IDENTIFIER_LEN = 512
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


def sanitize_attachment_filename(filename: str, max_len: int = _MAX_ATTACHMENT_NAME_LEN) -> str:
    # Non conservare path forniti dal mittente; oltre a non essere utili, possono
    # risultare ingannevoli nella UI o nei futuri export.
    leaf = re.split(r"[/\\]+", filename)[-1]
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", leaf).strip().strip(".")
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip()
    return cleaned or "unnamed_attachment"


@dataclass(frozen=True, slots=True)
class MimeLimits:
    max_body_bytes: int = _MAX_BODY_BYTES
    max_part_body_bytes: int = _MAX_PART_BODY_BYTES
    max_parts: int = _MAX_PARTS
    max_depth: int = _MAX_DEPTH
    max_attachments: int = _MAX_ATTACHMENTS
    max_attachment_name_len: int = _MAX_ATTACHMENT_NAME_LEN
    max_headers_per_part: int = _MAX_HEADERS_PER_PART
    max_header_name_len: int = _MAX_HEADER_NAME_LEN
    max_header_value_len: int = _MAX_HEADER_VALUE_LEN
    max_metadata_chars: int = _MAX_METADATA_CHARS
    max_identifier_len: int = _MAX_IDENTIFIER_LEN
    timeout_seconds: float = _PARSE_TIMEOUT_SECONDS


@dataclass(slots=True)
class _WalkState:
    started_at: float
    parts_seen: int = 0
    bytes_seen: int = 0
    metadata_chars_seen: int = 0


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
    try:
        parsed = _parse_payload(raw.payload, effective_limits, monotonic)
    except (HeaderParseError, MessageDefect) as exc:
        raise _parse_error("HEADER_PARSE_ERROR") from exc
    header_map = {header.name.casefold(): header.value for header in parsed.headers}
    from_addresses, from_malformed = parse_address_header(header_map.get("from"))
    to_addresses, to_malformed = parse_address_header(header_map.get("to"))
    cc_addresses, cc_malformed = parse_address_header(header_map.get("cc"))
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
    truncation_codes: list[str] = []
    if to_malformed or cc_malformed:
        truncation_codes.append("recipient_header_parse_warning")
    if from_malformed:
        truncation_codes.append("from_header_parse_warning")

    evidence = BodyEvidence(
        representation="text/plain" if plain_original else "text/html-derived",
        source_part_ids=tuple(part.part_id for part in source_parts),
        content_hash=hashlib.sha256(evidence_text.encode("utf-8")).hexdigest(),
        truncation_codes=tuple(truncation_codes),
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
    if not isinstance(payload, Mapping):
        raise _parse_error("MIME_MALFORMED")
    state = _WalkState(started_at=float(monotonic()))
    plain_parts: list[_TextPart] = []
    html_parts: list[_TextPart] = []
    attachments: list[AttachmentReference] = []
    headers: tuple[GmailHeader, ...] = ()

    def walk(part: Mapping[str, Any], depth: int, fallback_id: str) -> None:
        nonlocal headers
        _check_limits(state, limits, monotonic, depth)
        state.parts_seen += 1
        part_id = _bounded_metadata(part.get("partId") or fallback_id, limits, state)
        mime_type = _bounded_metadata(
            part.get("mimeType") or "application/octet-stream", limits, state
        ).casefold()
        body = part.get("body", {})
        body_mapping = cast(Mapping[str, Any], body) if isinstance(body, Mapping) else {}
        raw_filename = _bounded_metadata(part.get("filename") or "", limits, state)
        filename = _decode_header_value(raw_filename)
        attachment_id = _optional_bounded(
            body_mapping.get("attachmentId"), limits.max_identifier_len
        )
        size = _safe_nonnegative_size(body_mapping.get("size"))
        part_headers = _decode_headers(part.get("headers"), limits, state)
        if depth == 0:
            headers = part_headers
        part_header_map = {item.name.casefold(): item.value for item in part_headers}
        content_id = part_header_map.get("content-id")
        disposition = part_header_map.get("content-disposition", "").casefold()

        is_attachment = bool(
            filename
            or attachment_id
            or "attachment" in disposition
            or (
                mime_type
                not in {
                    "text/plain",
                    "text/html",
                    "multipart/alternative",
                    "multipart/mixed",
                    "multipart/related",
                    "multipart/digest",
                }
                and not part.get("parts")
            )
        )

        if is_attachment:
            if len(attachments) >= limits.max_attachments:
                raise _parse_error("ATTACHMENT_COUNT_LIMIT")
            sanitized_name = sanitize_attachment_filename(filename, limits.max_attachment_name_len)
            attachments.append(
                AttachmentReference(
                    part_id=part_id,
                    filename=sanitized_name,
                    mime_type=mime_type,
                    size=max(0, size),
                    attachment_id=attachment_id,
                    inline="inline" in disposition or bool(content_id),
                    content_id=content_id,
                )
            )
        elif mime_type in {"text/plain", "text/html"}:
            data = body_mapping.get("data")
            if data is not None:
                if not isinstance(data, str):
                    raise _parse_error("BASE64URL_DECODE_ERROR")
                raw_str = data.strip()
                # Controllo sulla rappresentazione encoded prima del decode: evita
                # sia l'allocazione del blob sia copie non necessarie di input enormi.
                max_encoded_chars = 4 * ((limits.max_part_body_bytes + 2) // 3)
                if len(raw_str) > max_encoded_chars:
                    raise _parse_error("BODY_TOO_LARGE")
                estimated_bytes = (len(raw_str) * 3) // 4
                if estimated_bytes > limits.max_part_body_bytes:
                    raise _parse_error("BODY_TOO_LARGE")
                if state.bytes_seen + estimated_bytes > limits.max_body_bytes:
                    raise _parse_error("BODY_TOO_LARGE")
                decoded = _decode_base64url(raw_str)
                if len(decoded) > limits.max_part_body_bytes:
                    raise _parse_error("BODY_TOO_LARGE")
                if state.bytes_seen + len(decoded) > limits.max_body_bytes:
                    raise _parse_error("BODY_TOO_LARGE")
                state.bytes_seen += len(decoded)
                text = _decode_text(decoded, part_header_map.get("content-type") or mime_type)
                if mime_type == "text/html":
                    html_parts.append(_TextPart(part_id, mime_type, html_to_safe_text(text)))
                else:
                    plain_parts.append(_TextPart(part_id, mime_type, text.strip()))

        child_parts = part.get("parts", ())
        if child_parts:
            if not isinstance(child_parts, list):
                raise _parse_error("MIME_MALFORMED")
            for index, child in enumerate(child_parts):
                if not isinstance(child, Mapping):
                    raise _parse_error("MIME_MALFORMED")
                walk(cast(Mapping[str, Any], child), depth + 1, f"{part_id}.{index}")

    walk(payload, 0, "0")
    return _ParsedMime(headers, tuple(plain_parts), tuple(html_parts), tuple(attachments))


def _check_limits(state: _WalkState, limits: MimeLimits, monotonic: Any, depth: int) -> None:
    if depth > limits.max_depth:
        raise _parse_error("MIME_DEPTH_LIMIT")
    if state.parts_seen >= limits.max_parts:
        raise _parse_error("MIME_PARTS_LIMIT")
    if float(monotonic()) - state.started_at > limits.timeout_seconds:
        raise _parse_error("MIME_TIMEOUT")


def _decode_headers(
    value: object, limits: MimeLimits, state: _WalkState
) -> tuple[GmailHeader, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise _parse_error("MIME_HEADERS_SCHEMA_INVALID")
    if len(value) > limits.max_headers_per_part:
        raise _parse_error("HEADER_COUNT_LIMIT")
    result: list[GmailHeader] = []
    for raw_header in value:
        if not isinstance(raw_header, Mapping):
            raise _parse_error("MIME_HEADER_SCHEMA_INVALID")
        raw_name = raw_header.get("name") or ""
        raw_value = raw_header.get("value") or ""
        if not isinstance(raw_name, str) or not isinstance(raw_value, str):
            raise _parse_error("MIME_HEADER_SCHEMA_INVALID")
        if (
            len(raw_name) > limits.max_header_name_len
            or len(raw_value) > limits.max_header_value_len
        ):
            raise _parse_error("METADATA_TOO_LARGE")
        state.metadata_chars_seen += len(raw_name) + len(raw_value)
        if state.metadata_chars_seen > limits.max_metadata_chars:
            raise _parse_error("METADATA_TOO_LARGE")
        name = raw_name.strip()
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
        except (TypeError, ValueError, OverflowError, HeaderParseError, MessageDefect):
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


def _parse_error(code: str) -> GmailMimeError:
    return GmailMimeError(code, f"Parsing MIME fallito: {code}")


def _optional_bounded(value: object, max_len: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > max_len:
        raise _parse_error("METADATA_TOO_LARGE")
    result = value.strip()
    return result or None


def _bounded_metadata(value: object, limits: MimeLimits, state: _WalkState) -> str:
    if not isinstance(value, str):
        raise _parse_error("MIME_MALFORMED")
    if len(value) > limits.max_identifier_len:
        raise _parse_error("METADATA_TOO_LARGE")
    state.metadata_chars_seen += len(value)
    if state.metadata_chars_seen > limits.max_metadata_chars:
        raise _parse_error("METADATA_TOO_LARGE")
    return value


def _safe_nonnegative_size(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise _parse_error("MIME_MALFORMED")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _parse_error("MIME_MALFORMED") from exc
    return max(0, parsed)
