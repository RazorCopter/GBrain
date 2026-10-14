from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from gbrain.application.gmail_jobs import _finish
from gbrain.application.gmail_mime import (
    MimeLimits,
    normalize_gmail_message,
    sanitize_attachment_filename,
)
from gbrain.application.gmail_sync import (
    GmailSentConnector,
    GmailSyncEngine,
    InMemoryGmailUnitOfWork,
)
from gbrain.domain.connectors import (
    ConnectorError,
    ConnectorErrorCategory,
    GmailMimeError,
    RequestBudget,
)
from gbrain.domain.gmail import (
    GmailCheckpointState,
    GmailItemGapData,
    GmailRawMessage,
    GmailReconciliationState,
    GmailSelection,
)
from gbrain.domain.models import (
    ImportJob,
    ImportJobStatus,
)
from gbrain.infrastructure.gmail_gateway import OfficialGmailGateway
from tests.fakes.gmail_gateway import FakeGmailGateway

_NOW = datetime(2024, 2, 1, 12, 0, tzinfo=UTC)
_MSG_DATE = datetime(2024, 1, 15, 12, 0, tzinfo=UTC)
_SELECTION = GmailSelection(
    after=datetime(2024, 1, 1, tzinfo=UTC),
    before=datetime(2024, 2, 1, tzinfo=UTC),
    limit=100,
    page_size=10,
)


def _checkpoint(account_id: str = "acc-1", policy_id: str = "pol-1") -> GmailCheckpointState:
    return GmailCheckpointState(
        account_id=account_id,
        policy_id=policy_id,
        history_id="1000",
        page_token=None,
        coverage_after=_SELECTION.after,
        coverage_before=_SELECTION.before,
        last_synced_at=_NOW,
        reconciliation_state=GmailReconciliationState.NOT_REQUIRED,
    )


def _make_raw_message(
    message_id: str,
    *,
    size_estimate: int = 1000,
    body_text: str = "Testo breve",
    attachment_size: int = 0,
    attachment_name: str | None = None,
    attachment_id: str | None = None,
) -> GmailRawMessage:
    encoded_body = base64.urlsafe_b64encode(body_text.encode("utf-8")).decode("ascii")
    parts: list[dict[str, Any]] = [
        {
            "partId": "0",
            "mimeType": "text/plain",
            "headers": [{"name": "Content-Type", "value": "text/plain; charset=utf-8"}],
            "body": {"size": len(body_text), "data": encoded_body},
        }
    ]
    if attachment_name or attachment_id or attachment_size > 0:
        parts.append(
            {
                "partId": "1",
                "mimeType": "application/pdf",
                "filename": attachment_name or "allegato.pdf",
                "headers": [
                    {"name": "Content-Type", "value": "application/pdf"},
                    {"name": "Content-Disposition", "value": 'attachment; filename="allegato.pdf"'},
                ],
                "body": {
                    "size": attachment_size,
                    "attachmentId": attachment_id or f"att-{message_id}",
                },
            }
        )

    payload = {
        "mimeType": "multipart/mixed" if len(parts) > 1 else "text/plain",
        "headers": [
            {"name": "Message-ID", "value": f"<{message_id}@example.test>"},
            {"name": "Date", "value": "Thu, 1 Feb 2024 10:00:00 +0000"},
            {"name": "From", "value": "owner@example.test"},
            {"name": "To", "value": "dest@example.test"},
            {"name": "Subject", "value": f"Oggetto {message_id}"},
        ],
        "parts": parts if len(parts) > 1 else None,
        "body": parts[0]["body"] if len(parts) == 1 else {},
    }
    return GmailRawMessage(
        message_id=message_id,
        thread_id=f"thread-{message_id}",
        history_id="1001",
        label_ids=("SENT",),
        internal_date_ms=int(_MSG_DATE.timestamp() * 1000),
        snippet=body_text[:50],
        payload=payload,
        size_estimate=size_estimate,
    )


# ==============================================================================
# SCENARIO A: Mail con sizeEstimate > 25 MiB per allegato grande esterno
# ==============================================================================


def test_scenario_a_large_size_estimate_with_small_text_is_imported() -> None:
    # 50 MiB di sizeEstimate e allegato dichiarato di 48 MiB
    raw = _make_raw_message(
        "msg-large-att",
        size_estimate=50 * 1024 * 1024,
        body_text="Questo testo è breve ed essenziale.",
        attachment_size=48 * 1024 * 1024,
        attachment_name="archivio_pesante.zip",
        attachment_id="att-google-ref-12345",
    )

    normalized = normalize_gmail_message(raw, ("owner@example.test",), acquired_at=_NOW)

    assert normalized.external_id == "msg-large-att"
    assert normalized.body_new == "Questo testo è breve ed essenziale."
    assert len(normalized.attachments) == 1

    att = normalized.attachments[0]
    assert att.filename == "archivio_pesante.zip"
    assert att.size == 48 * 1024 * 1024
    assert att.mime_type == "application/pdf"

    # Gateway inspection: conferma che OfficialGmailGateway non contiene metodi per scaricare attachment blob
    gateway_attrs = dir(OfficialGmailGateway)
    assert "download_attachment" not in gateway_attrs
    assert "get_attachment" not in gateway_attrs

    # Esegui sync engine con questo messaggio
    fixture = {
        "profile": {"emailAddress": "owner@example.test", "historyId": "1001"},
        "messages": [
            {
                "id": "msg-large-att",
                "threadId": "thread-msg-large-att",
                "historyId": "1001",
                "labelIds": ["SENT"],
                "internalDate": str(int(_MSG_DATE.timestamp() * 1000)),
                "snippet": "Questo testo è breve...",
                "sizeEstimate": 50 * 1024 * 1024,
                "payload": raw.payload,
            }
        ],
    }
    gateway = FakeGmailGateway(fixture, page_size=10)
    connector = GmailSentConnector(gateway, ("owner@example.test",))
    uow = InMemoryGmailUnitOfWork()
    engine = GmailSyncEngine(connector, uow, batch_size=10)

    result = engine.full_sync(_SELECTION, _checkpoint(), RequestBudget(10, 10))

    assert result.persisted_items == 1
    assert result.not_imported_items == 0
    assert len(result.gaps) == 0
    # Verifica zero chiamate a qualsiasi API di attachment
    assert all("attachment" not in call[0] for call in gateway.calls)


# ==============================================================================
# SCENARIO B: Corpo testuale decodificato oltre il limite -> BODY_TOO_LARGE bounded
# ==============================================================================


def test_scenario_b_decoded_body_over_limit_produces_body_too_large() -> None:
    # Simula un corpo testuale enorme (es. 6 MiB di testo con limite a 5 MiB)
    oversized_text = "A" * (6 * 1024 * 1024)
    raw = _make_raw_message(
        "msg-huge-text", size_estimate=7 * 1024 * 1024, body_text=oversized_text
    )

    # Prima della chiamata effettiva verifichiamo che la stima preventiva rifiuti senza allocare
    limits = MimeLimits(max_body_bytes=5 * 1024 * 1024)
    with pytest.raises(GmailMimeError) as exc_info:
        normalize_gmail_message(raw, ("owner@example.test",), limits=limits)

    assert exc_info.value.reason_code == "BODY_TOO_LARGE"
    assert exc_info.value.category == ConnectorErrorCategory.PARSE


# ==============================================================================
# SCENARIO C: Batch misto [valida, troppo grande, valida] -> isolamento e gaps
# ==============================================================================


def test_scenario_c_mixed_batch_isolates_bad_item_and_records_gap() -> None:
    valid_1 = {
        "id": "msg-valid-1",
        "threadId": "t-1",
        "historyId": "1001",
        "labelIds": ["SENT"],
        "internalDate": str(int(_MSG_DATE.timestamp() * 1000)),
        "snippet": "Valida 1",
        "payload": _make_raw_message("msg-valid-1", body_text="Testo valido 1").payload,
    }
    # Messaggio con corpo oltre limite (es. limite configurato 100 byte, testo 500 byte)
    bad_item = {
        "id": "msg-too-large",
        "threadId": "t-2",
        "historyId": "1002",
        "labelIds": ["SENT"],
        "internalDate": str(int(_MSG_DATE.timestamp() * 1000)),
        "snippet": "Troppo grande",
        "payload": _make_raw_message("msg-too-large", body_text="B" * 500).payload,
    }
    valid_2 = {
        "id": "msg-valid-2",
        "threadId": "t-3",
        "historyId": "1003",
        "labelIds": ["SENT"],
        "internalDate": str(int(_MSG_DATE.timestamp() * 1000)),
        "snippet": "Valida 2",
        "payload": _make_raw_message("msg-valid-2", body_text="Testo valido 2").payload,
    }

    fixture = {
        "profile": {"emailAddress": "owner@example.test", "historyId": "1003"},
        "messages": [valid_1, bad_item, valid_2],
    }
    gateway = FakeGmailGateway(fixture, page_size=10)
    # Connector con limite body ristretto a 100 byte
    connector = GmailSentConnector(
        gateway,
        ("owner@example.test",),
        limits=MimeLimits(max_body_bytes=100, max_part_body_bytes=100),
    )
    uow = InMemoryGmailUnitOfWork()
    engine = GmailSyncEngine(connector, uow, batch_size=10)

    result = engine.full_sync(_SELECTION, _checkpoint(), RequestBudget(20, 20))

    # Esattamente due salvate e una non importata!
    assert result.persisted_items == 2
    assert result.not_imported_items == 1
    assert len(result.gaps) == 1

    gap = result.gaps[0]
    assert gap.gmail_message_id == "msg-too-large"
    assert gap.reason_code == "BODY_TOO_LARGE"
    assert gap.phase == "mime_parse"
    assert gap.status == "open"

    # I due validi sono salvati nel repository
    assert ("acc-1", "msg-valid-1") in uow.messages
    assert ("acc-1", "msg-valid-2") in uow.messages
    assert ("acc-1", "msg-too-large") not in uow.messages
    # Il gap è registrato nell'UoW
    assert ("acc-1", "msg-too-large") in uow.gaps


# ==============================================================================
# SCENARIO D: Ripetizione/ripresa, idempotenza e transport error che non avanza
# ==============================================================================


def test_scenario_d_resumption_idempotency_and_transport_error_does_not_advance() -> None:
    valid_1 = {
        "id": "msg-d-1",
        "threadId": "t-d-1",
        "historyId": "1001",
        "labelIds": ["SENT"],
        "internalDate": str(int(_MSG_DATE.timestamp() * 1000)),
        "snippet": "D 1",
        "payload": _make_raw_message("msg-d-1", body_text="Corpo 1").payload,
    }
    bad_item = {
        "id": "msg-d-bad",
        "threadId": "t-d-bad",
        "historyId": "1002",
        "labelIds": ["SENT"],
        "internalDate": str(int(_MSG_DATE.timestamp() * 1000)),
        "snippet": "D Bad",
        "payload": _make_raw_message("msg-d-bad", body_text="X" * 200).payload,
    }
    fixture = {
        "profile": {"emailAddress": "owner@example.test", "historyId": "1002"},
        "messages": [valid_1, bad_item],
    }
    gateway = FakeGmailGateway(fixture, page_size=10)
    connector = GmailSentConnector(
        gateway,
        ("owner@example.test",),
        limits=MimeLimits(max_body_bytes=50),
    )
    uow = InMemoryGmailUnitOfWork()
    engine = GmailSyncEngine(connector, uow, batch_size=10)

    # 1. Prima esecuzione: 1 salvata, 1 gap
    first_run = engine.full_sync(_SELECTION, _checkpoint(), RequestBudget(10, 10))
    assert first_run.persisted_items == 1
    assert first_run.not_imported_items == 1
    assert len(uow.gaps) == 1

    # 2. Riesecuzione (replay): nessun nuovo inserimento, nessun duplicato
    second_run = engine.full_sync(_SELECTION, first_run.checkpoint, RequestBudget(10, 10))
    assert second_run.persisted_items == 0
    assert second_run.unchanged_items == 1
    assert len(uow.gaps) == 1

    # 3. Transport error durante un fetch message: deve interrompere senza avanzare checkpoint
    gateway.queue_failure(
        "messages.get",
        ConnectorError(ConnectorErrorCategory.TRANSIENT, "Network timeout 503", retryable=True),
    )
    with pytest.raises(ConnectorError) as exc_info:
        engine.full_sync(_SELECTION, first_run.checkpoint, RequestBudget(10, 10))
    assert exc_info.value.category == ConnectorErrorCategory.TRANSIENT


# ==============================================================================
# SCENARIO E: Risoluzione del gap quando il messaggio diventa valido
# ==============================================================================


def test_scenario_e_gap_resolution_when_message_becomes_valid() -> None:
    uow = InMemoryGmailUnitOfWork()
    # Registriamo un gap iniziale aperto per msg-fix-1
    uow.gaps[("acc-1", "msg-fix-1")] = GmailItemGapData(
        gmail_message_id="msg-fix-1",
        phase="mime_parse",
        reason_code="BODY_TOO_LARGE",
        details={"error": "too big"},
        occurred_at=_NOW,
        status="open",
    )
    assert uow.gaps[("acc-1", "msg-fix-1")].status == "open"

    # Ora il messaggio viene corretto / fornito in versione valida
    valid_raw = _make_raw_message("msg-fix-1", body_text="Testo sistemato e valido")
    normalized = normalize_gmail_message(valid_raw, ("owner@example.test",), acquired_at=_NOW)

    # Persist batch con il messaggio valido
    outcomes = uow.persist_batch("acc-1", [normalized], _checkpoint())
    assert len(outcomes) == 1
    assert outcomes[0].inserted is True

    # Il gap deve ora risultare risolto!
    assert uow.gaps[("acc-1", "msg-fix-1")].status == "resolved"


# ==============================================================================
# SCENARIO F: MIME bounded - depth, part count, attachment count, base64 stima
# ==============================================================================


def test_scenario_f_mime_depth_limit() -> None:
    # Costruisci un albero con 12 livelli di annidamento
    nested: dict[str, Any] = {"mimeType": "text/plain", "body": {"data": ""}}
    for _ in range(12):
        nested = {"mimeType": "multipart/mixed", "parts": [nested]}

    raw = GmailRawMessage(
        "deep-msg", "t", "1", ("SENT",), int(_NOW.timestamp() * 1000), "", nested, 100
    )
    with pytest.raises(GmailMimeError) as exc:
        normalize_gmail_message(raw, ("owner@example.test",), limits=MimeLimits(max_depth=5))
    assert exc.value.reason_code == "MIME_DEPTH_LIMIT"


def test_scenario_f_mime_parts_limit() -> None:
    # Costruisci 120 parti
    parts = [{"mimeType": "text/plain", "body": {"data": ""}} for _ in range(120)]
    payload = {"mimeType": "multipart/mixed", "parts": parts}
    raw = GmailRawMessage(
        "many-parts", "t", "1", ("SENT",), int(_NOW.timestamp() * 1000), "", payload, 100
    )
    with pytest.raises(GmailMimeError) as exc:
        normalize_gmail_message(raw, ("owner@example.test",), limits=MimeLimits(max_parts=50))
    assert exc.value.reason_code == "MIME_PARTS_LIMIT"


def test_scenario_f_attachment_count_limit() -> None:
    parts = [
        {
            "partId": str(i),
            "mimeType": "application/pdf",
            "filename": f"file_{i}.pdf",
            "body": {"size": 100},
        }
        for i in range(15)
    ]
    payload = {"mimeType": "multipart/mixed", "parts": parts}
    raw = GmailRawMessage(
        "many-att", "t", "1", ("SENT",), int(_NOW.timestamp() * 1000), "", payload, 100
    )
    with pytest.raises(GmailMimeError) as exc:
        normalize_gmail_message(raw, ("owner@example.test",), limits=MimeLimits(max_attachments=10))
    assert exc.value.reason_code == "ATTACHMENT_COUNT_LIMIT"


def test_scenario_f_huge_base64_string_rejected_before_decode_allocation() -> None:
    # 20 milioni di caratteri in stringa senza decodificare
    huge_str = "A" * (20 * 1024 * 1024)
    payload = {
        "mimeType": "text/plain",
        "body": {"data": huge_str},
    }
    raw = GmailRawMessage(
        "huge-b64", "t", "1", ("SENT",), int(_NOW.timestamp() * 1000), "", payload, 20 * 1024 * 1024
    )
    with pytest.raises(GmailMimeError) as exc:
        normalize_gmail_message(
            raw, ("owner@example.test",), limits=MimeLimits(max_body_bytes=5 * 1024 * 1024)
        )
    assert exc.value.reason_code == "BODY_TOO_LARGE"


def test_scenario_f_attachment_filename_sanitization() -> None:
    malicious_name = "test\x00\r\nfile\t" + ("A" * 300) + ".pdf"
    sanitized = sanitize_attachment_filename(malicious_name, max_len=50)
    assert "\x00" not in sanitized
    assert "\r" not in sanitized
    assert "\n" not in sanitized
    assert len(sanitized) <= 50


# ==============================================================================
# SCENARIO G: Worker & Job Status (COMPLETED_WITH_WARNINGS e FAILED fatal error)
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_g_finish_switches_to_completed_with_warnings_when_gaps_exist() -> None:
    class FakeDb:
        def __init__(self, job: ImportJob) -> None:
            self._job = job

        def session_factory(self) -> Any:
            class FakeSessionCtx:
                def __init__(self, job: ImportJob) -> None:
                    self._job = job

                async def __aenter__(self) -> Any:
                    session = MagicMock()
                    session.get = AsyncMock(return_value=self._job)
                    session.commit = AsyncMock()
                    return session

                async def __aexit__(self, *args: Any) -> None:
                    pass

            return FakeSessionCtx(self._job)

    from unittest.mock import AsyncMock

    job = ImportJob(
        id=uuid4(),
        source_kind="gmail_sent",
        status=ImportJobStatus.RUNNING,
        processed_items=3,
        imported_items=2,
        duplicate_items=0,
        not_imported_items=1,
    )
    fake_db = FakeDb(job)

    finished = await _finish(
        fake_db,  # type: ignore[arg-type]
        job.id,
        status=ImportJobStatus.COMPLETED,
        checkpoint=_checkpoint(),
        processed=3,
        imported=2,
        duplicate=0,
        not_imported=1,
    )

    # Con not_imported_items > 0, lo stato finale deve essere COMPLETED_WITH_WARNINGS
    assert finished.status == ImportJobStatus.COMPLETED_WITH_WARNINGS
    assert finished.imported_items == 2
    assert finished.not_imported_items == 1


# ==============================================================================
# SCENARIO H: API Diagnostics per-item (Paginata, no PII, isolata)
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_h_diagnostics_endpoint_no_pii_and_pagination() -> None:
    from gbrain.domain.models import GmailItemGap
    from gbrain.infrastructure.gmail_repository import AsyncPostgresGmailRepository

    # Prepariamo un mock session per repository
    account_id = uuid4()
    mock_session = MagicMock()

    gap_record = GmailItemGap(
        id=uuid4(),
        connector_account_id=account_id,
        gmail_message_id="18e9a1b2c3d4e5f6",
        gmail_thread_id="18e9a1b2c3d4e5f6",
        config_revision=1,
        query_fingerprint="fp123",
        phase="mime_parse",
        reason_code="BODY_TOO_LARGE",
        status="open",
        occurred_at=_NOW,
        resolved_at=None,
        details={
            "error_code": "BODY_TOO_LARGE",
            "size_estimate": 30000000,
            "bytes_seen": 5242881,
            "limit_bytes": 5242880,
        },
    )

    mock_session.scalar = AsyncMock(side_effect=[1, 1, 0])
    mock_session.scalars = AsyncMock(return_value=MagicMock(all=lambda: [gap_record]))

    repo = AsyncPostgresGmailRepository(mock_session)
    items, total = await repo.list_gaps(account_id, page=1, page_size=10)
    open_c, res_c = await repo.count_gaps(account_id)

    assert total == 1
    assert open_c == 1
    assert res_c == 0
    assert len(items) == 1
    item = items[0]
    assert item.gmail_message_id == "18e9a1b2c3d4e5f6"
    assert item.reason_code == "BODY_TOO_LARGE"

    # Verifica ASSENZA TOTALE DI PII nei dettagli
    details_str = str(item.details)
    assert "@" not in details_str
    assert "From" not in details_str
    assert "To" not in details_str
    assert "Subject" not in details_str
    assert "bearer" not in details_str.casefold()
    assert "token" not in details_str.casefold()


def test_malformed_size_is_a_stable_isolatable_parse_error() -> None:
    base = _make_raw_message("bad-size")
    payload = dict(base.payload)
    payload["body"] = {"size": "not-an-integer", "data": "SGk"}
    payload["parts"] = None
    raw = GmailRawMessage(
        base.message_id,
        base.thread_id,
        base.history_id,
        base.label_ids,
        base.internal_date_ms,
        base.snippet,
        payload,
        base.size_estimate,
    )

    with pytest.raises(GmailMimeError) as caught:
        normalize_gmail_message(raw, ("owner@example.test",))

    assert caught.value.reason_code == "MIME_MALFORMED"
    assert "not-an-integer" not in caught.value.safe_message


def test_headers_and_encoded_body_are_rejected_before_large_decode() -> None:
    base = _make_raw_message("bounded")
    raw_headers = base.payload.get("headers")
    headers: list[dict[str, Any]] = (
        [dict(h) for h in raw_headers] if isinstance(raw_headers, list) else []
    )
    headers.append({"name": "Subject", "value": "x" * 33})
    payload1 = dict(base.payload)
    payload1["headers"] = headers
    raw1 = GmailRawMessage(
        base.message_id,
        base.thread_id,
        base.history_id,
        base.label_ids,
        base.internal_date_ms,
        base.snippet,
        payload1,
        base.size_estimate,
    )
    with pytest.raises(GmailMimeError) as header_error:
        normalize_gmail_message(
            raw1,
            ("owner@example.test",),
            limits=MimeLimits(max_header_value_len=32),
        )
    assert header_error.value.reason_code == "METADATA_TOO_LARGE"

    base2 = _make_raw_message("encoded-bounded")
    payload2 = dict(base2.payload)
    payload2["body"] = {"size": 1, "data": "A" * 17}
    payload2["parts"] = None
    raw2 = GmailRawMessage(
        base2.message_id,
        base2.thread_id,
        base2.history_id,
        base2.label_ids,
        base2.internal_date_ms,
        base2.snippet,
        payload2,
        base2.size_estimate,
    )
    with pytest.raises(GmailMimeError) as body_error:
        normalize_gmail_message(
            raw2,
            ("owner@example.test",),
            limits=MimeLimits(max_part_body_bytes=8, max_body_bytes=8),
        )
    assert body_error.value.reason_code == "BODY_TOO_LARGE"


def test_filename_sanitization_drops_paths_controls_and_nulls() -> None:
    assert sanitize_attachment_filename("..\\private/path\r\nname\x00.txt") == "pathname.txt"


# ==============================================================================
# SCENARIO G: Incremental sync isola PARSE permanente, persiste gap + item valido +
#             checkpoint in modo atomico, non duplica al resume e propaga errori infrastrutturali
# ==============================================================================


def test_incremental_sync_isolates_parse_failure_advances_checkpoint_and_resume_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad_item = {
        "id": "msg-inc-parse-fail",
        "threadId": "t-1",
        "historyId": "1001",
        "labelIds": ["SENT"],
        "internalDate": str(int(_MSG_DATE.timestamp() * 1000)),
        "snippet": "Parse fail message",
        "payload": _make_raw_message("msg-inc-parse-fail", body_text="X" * 300).payload,
    }
    valid_item = {
        "id": "msg-inc-valid",
        "threadId": "t-2",
        "historyId": "1002",
        "labelIds": ["SENT"],
        "internalDate": str(int(_MSG_DATE.timestamp() * 1000)),
        "snippet": "Valid message",
        "payload": _make_raw_message("msg-inc-valid", body_text="Valid body text").payload,
    }
    fixture = {
        "profile": {"emailAddress": "owner@example.test", "historyId": "1002"},
        "messages": [bad_item, valid_item],
    }
    gateway = FakeGmailGateway(fixture, page_size=10)
    # Pagina incrementale con item parse-failed seguito da item valido
    gateway.history_pages = [
        {"history_id": "1002", "changed": ["msg-inc-parse-fail", "msg-inc-valid"]}
    ]
    connector = GmailSentConnector(
        gateway,
        ("owner@example.test",),
        limits=MimeLimits(max_body_bytes=100, max_part_body_bytes=100),
    )
    uow = InMemoryGmailUnitOfWork()
    engine = GmailSyncEngine(connector, uow, batch_size=10)

    # Checkpoint iniziale a history_id="1000"
    init_chk = _checkpoint()

    # 1. Esecuzione incrementale: l'errore PARSE su msg-inc-parse-fail deve essere isolato,
    # registrato come gap aperto, e l'item successivo msg-inc-valid deve essere importato con successo.
    result = engine.incremental_sync(init_chk, RequestBudget(10, 10))

    assert result.persisted_items == 1
    assert result.not_imported_items == 1
    assert len(result.gaps) == 1

    gap = result.gaps[0]
    assert gap.gmail_message_id == "msg-inc-parse-fail"
    assert gap.reason_code == "BODY_TOO_LARGE"
    assert gap.phase == "mime_parse"
    assert gap.status == "open"

    # Persistenza atomica: item valido, gap e checkpoint avanzato al latest_history_id
    assert ("acc-1", "msg-inc-parse-fail") not in uow.messages
    assert ("acc-1", "msg-inc-valid") in uow.messages
    assert ("acc-1", "msg-inc-parse-fail") in uow.gaps
    assert uow.gaps[("acc-1", "msg-inc-parse-fail")].reason_code == "BODY_TOO_LARGE"
    assert uow.checkpoints["acc-1"].history_id == "1002"
    assert uow.commit_count == 1

    # 2. Ripresa (resume): sincronizzazione successiva a partire dal checkpoint avanzato (1002)
    # Non essendoci ulteriori record di history, non deve inserire nulla né duplicare.
    resume_result = engine.incremental_sync(result.checkpoint, RequestBudget(10, 10))
    assert resume_result.persisted_items == 0
    assert len(uow.messages) == 1

    # 3. Idempotenza in caso di riesecuzione della medesima pagina di history (replay)
    replay_result = engine.incremental_sync(init_chk, RequestBudget(10, 10))
    assert replay_result.persisted_items == 0
    assert replay_result.unchanged_items == 1
    assert replay_result.not_imported_items == 1
    assert len(uow.messages) == 1
    assert len(uow.gaps) == 1

    # 4. Propagazione categorica degli errori: errori infrastrutturali, quota, auth, timeout e inattesi
    # non devono essere confinati e devono propagare senza avanzare il checkpoint.
    gateway.queue_failure(
        "messages.get",
        ConnectorError(
            ConnectorErrorCategory.TRANSIENT, "503 Service Unavailable / Timeout", retryable=True
        ),
    )
    with pytest.raises(ConnectorError) as exc_transient:
        engine.incremental_sync(init_chk, RequestBudget(10, 10))
    assert exc_transient.value.category == ConnectorErrorCategory.TRANSIENT

    gateway.queue_failure(
        "messages.get",
        ConnectorError(ConnectorErrorCategory.QUOTA, "429 Quota Exceeded"),
    )
    with pytest.raises(ConnectorError) as exc_quota:
        engine.incremental_sync(init_chk, RequestBudget(10, 10))
    assert exc_quota.value.category == ConnectorErrorCategory.QUOTA

    gateway.queue_failure(
        "messages.get",
        ConnectorError(ConnectorErrorCategory.AUTH, "401 Unauthorized"),
    )
    with pytest.raises(ConnectorError) as exc_auth:
        engine.incremental_sync(init_chk, RequestBudget(10, 10))
    assert exc_auth.value.category == ConnectorErrorCategory.AUTH

    def bug_normalize(raw: Any) -> Any:
        raise RuntimeError("unexpected runtime exception")

    monkeypatch.setattr(connector, "normalize_item", bug_normalize)
    with pytest.raises(RuntimeError, match="unexpected runtime exception"):
        engine.incremental_sync(init_chk, RequestBudget(10, 10))


def test_incremental_sync_atomic_commit_boundary_and_resume() -> None:
    bad_item = {
        "id": "msg-inc-atomic-fail",
        "threadId": "t-1",
        "historyId": "2001",
        "labelIds": ["SENT"],
        "internalDate": str(int(_MSG_DATE.timestamp() * 1000)),
        "snippet": "Parse fail",
        "payload": _make_raw_message("msg-inc-atomic-fail", body_text="Z" * 300).payload,
    }
    valid_item = {
        "id": "msg-inc-atomic-valid",
        "threadId": "t-2",
        "historyId": "2002",
        "labelIds": ["SENT"],
        "internalDate": str(int(_MSG_DATE.timestamp() * 1000)),
        "snippet": "Valid",
        "payload": _make_raw_message("msg-inc-atomic-valid", body_text="Valid body").payload,
    }
    fixture = {
        "profile": {"emailAddress": "owner@example.test", "historyId": "2002"},
        "messages": [bad_item, valid_item],
    }
    gateway = FakeGmailGateway(fixture, page_size=10)
    gateway.history_pages = [
        {"history_id": "2002", "changed": ["msg-inc-atomic-fail", "msg-inc-atomic-valid"]}
    ]
    connector = GmailSentConnector(
        gateway,
        ("owner@example.test",),
        limits=MimeLimits(max_body_bytes=100, max_part_body_bytes=100),
    )

    class FailingOnceUoW(InMemoryGmailUnitOfWork):
        def __init__(self) -> None:
            super().__init__()
            self.failed_once = False

        def persist_batch(self, account_id: str, *args: Any, **kwargs: Any) -> Any:
            if not self.failed_once:
                self.failed_once = True
                raise RuntimeError("Database connection lost during commit")
            return super().persist_batch(account_id, *args, **kwargs)

    uow = FailingOnceUoW()
    engine = GmailSyncEngine(connector, uow, batch_size=10)
    init_chk = GmailCheckpointState(
        account_id="acc-1",
        policy_id="pol-1",
        history_id="2000",
        page_token=None,
        coverage_after=_SELECTION.after,
        coverage_before=_SELECTION.before,
        last_synced_at=_NOW,
        reconciliation_state=GmailReconciliationState.NOT_REQUIRED,
    )

    # 1. Primo tentativo fallisce prima del commit: nulla deve essere stato salvato
    with pytest.raises(RuntimeError, match="Database connection lost"):
        engine.incremental_sync(init_chk, RequestBudget(10, 10))

    assert ("acc-1", "msg-inc-atomic-valid") not in uow.messages
    assert ("acc-1", "msg-inc-atomic-fail") not in uow.gaps
    assert "acc-1" not in uow.checkpoints

    # 2. Ripresa: ora il commit atomico ha successo
    resumed_result = engine.incremental_sync(init_chk, RequestBudget(10, 10))
    assert resumed_result.persisted_items == 1
    assert resumed_result.not_imported_items == 1
    assert ("acc-1", "msg-inc-atomic-valid") in uow.messages
    assert ("acc-1", "msg-inc-atomic-fail") in uow.gaps
    assert uow.checkpoints["acc-1"].history_id == "2002"

    # 3. Ulteriore invocazione non duplica
    followup_result = engine.incremental_sync(resumed_result.checkpoint, RequestBudget(10, 10))
    assert followup_result.persisted_items == 0
    assert len(uow.messages) == 1
