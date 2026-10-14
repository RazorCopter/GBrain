from __future__ import annotations

import base64
import logging
import sys
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

import gbrain.domain.gmail as gmail_domain
from gbrain.application.gmail_jobs import (
    GmailJobTransientError,
    best_effort_terminate_job,
    execute_gmail_job,
)
from gbrain.application.gmail_mime import (
    normalize_gmail_message,
)
from gbrain.application.gmail_sync import (
    GmailSentConnector,
    GmailSyncEngine,
    InMemoryGmailUnitOfWork,
)
from gbrain.config import get_settings
from gbrain.domain.connectors import (
    ConnectorError,
    ConnectorErrorCategory,
    RequestBudget,
)
from gbrain.domain.gmail import (
    AttributionConfidence,
    GmailCheckpointState,
    GmailLocator,
    GmailRawMessage,
    GmailReconciliationState,
    GmailSelection,
    address_header_values,
    parse_address_header,
)
from gbrain.domain.models import (
    ImportJobStatus,
)
from tests.fakes.gmail_gateway import FakeGmailGateway

_NOW = datetime(2024, 2, 1, 12, 0, tzinfo=UTC)
_MSG_DATE = datetime(2024, 1, 15, 12, 0, tzinfo=UTC)
_SELECTION = GmailSelection(
    after=datetime(2024, 1, 1, tzinfo=UTC),
    before=datetime(2024, 2, 1, tzinfo=UTC),
    limit=100,
    page_size=10,
)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    monkeypatch.setenv("GBRAIN_POSTGRES_PASSWORD", "test-secret")
    monkeypatch.setenv("GBRAIN_AUTH_SECRET", "0" * 40)
    monkeypatch.setenv("GBRAIN_NEO4J_PASSWORD", "test-secret")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    sys.modules.pop("gbrain.worker", None)


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


def _make_msg(
    msg_id: str,
    *,
    from_addr: str = "owner@example.test",
    to_addr: str = "recipient@example.test",
    cc_addr: str | None = None,
    body_text: str = "Contenuto del messaggio valido",
    label_ids: tuple[str, ...] = ("SENT",),
) -> GmailRawMessage:
    encoded = base64.urlsafe_b64encode(body_text.encode("utf-8")).decode("ascii")
    headers = [
        {"name": "From", "value": from_addr},
        {"name": "To", "value": to_addr},
        {"name": "Subject", "value": f"Oggetto {msg_id}"},
    ]
    if cc_addr is not None:
        headers.append({"name": "Cc", "value": cc_addr})
    payload = {
        "mimeType": "text/plain",
        "headers": headers,
        "body": {"size": len(body_text), "data": encoded},
    }
    return GmailRawMessage(
        message_id=msg_id,
        thread_id=f"thread-{msg_id}",
        history_id="1001",
        label_ids=label_ids,
        internal_date_ms=int(_MSG_DATE.timestamp() * 1000),
        snippet=body_text[:50],
        payload=payload,
        size_estimate=len(body_text) + 200,
    )


# ==============================================================================
# 1. Test: destinatario vuoto / trailing comma
# ==============================================================================


def test_corrupted_headers_lost_by_permissive_parser_are_detected() -> None:
    """Verifica che i tre header corrotti segnalati dall'audit conservino i validi con warning."""
    cases = [
        ("Name <good@example.test>, malformed <", ("good@example.test",)),
        ("good@example.test, broken <", ("good@example.test",)),
        ("good@example.test, bad@@", ("good@example.test",)),
    ]
    for raw_header, expected_addrs in cases:
        addrs, malformed = parse_address_header(raw_header)
        assert addrs == expected_addrs, f"Expected {expected_addrs} for {raw_header}"
        assert malformed is True, f"Expected malformed=True for {raw_header}"

        # Normalizzazione del messaggio deve conservare l'indirizzo valido ed emettere il warning
        raw = _make_msg(f"msg-{abs(hash(raw_header))}", to_addr=raw_header)
        norm = normalize_gmail_message(raw, ["owner@example.test"])
        assert norm.to_addresses == expected_addrs
        payload = norm.provenance_payload()
        truncation_codes = payload.get("evidence", {}).get("truncation_codes", [])
        assert "recipient_header_parse_warning" in truncation_codes


def test_recipient_empty_and_trailing_comma() -> None:
    # Trailing comma
    addrs, malformed = parse_address_header("alice@example.test,")
    assert addrs == ("alice@example.test",)
    assert malformed is False
    assert address_header_values("alice@example.test,") == ("alice@example.test",)

    # Stringa vuota o whitespace
    assert parse_address_header("") == ((), False)
    assert parse_address_header("   ") == ((), False)
    assert parse_address_header(None) == ((), False)
    assert address_header_values("") == ()

    # Virgole consecutive senza indirizzi
    assert parse_address_header(",,,") == ((), False)
    assert parse_address_header("  ,  ,  ") == ((), False)

    # Display name quotato contenente virgola non deve generare falso warning
    quoted_addrs, quoted_malformed = parse_address_header('"Doe, John" <john.doe@example.test>')
    assert quoted_addrs == ("john.doe@example.test",)
    assert quoted_malformed is False

    # Commento con virgola non deve generare falso warning
    comment_addrs, comment_malformed = parse_address_header(
        "alice@example.test (comment with comma, here)"
    )
    assert comment_addrs == ("alice@example.test",)
    assert comment_malformed is False

    # Normalizzazione messaggio con trailing comma
    raw = _make_msg("msg-trailing", to_addr="recipient@example.test,")
    norm = normalize_gmail_message(raw, ["owner@example.test"])
    assert norm.to_addresses == ("recipient@example.test",)
    assert norm.body_new == "Contenuto del messaggio valido"
    truncation_codes = norm.provenance_payload().get("evidence", {}).get("truncation_codes", [])
    assert "recipient_header_parse_warning" not in truncation_codes


# ==============================================================================
# 2. Test: gruppo senza indirizzi
# ==============================================================================


def test_recipient_group_without_addresses() -> None:
    # Gruppo vuoto RFC standard
    addrs, malformed = parse_address_header("undisclosed-recipients:;")
    assert addrs == ()
    assert malformed is False
    assert address_header_values("undisclosed-recipients:;") == ()

    # Altri gruppi vuoti
    addrs2, malformed2 = parse_address_header("Dev-Team: ;")
    assert addrs2 == ()
    assert malformed2 is False

    # Gruppo vuoto semplice
    addrs_empty, malformed_empty = parse_address_header("group:;")
    assert addrs_empty == ()
    assert malformed_empty is False

    # Gruppo con indirizzi validi
    group_addrs, group_malformed = parse_address_header(
        "group: alice@example.test, bob@example.test;"
    )
    assert group_addrs == ("alice@example.test", "bob@example.test")
    assert group_malformed is False

    # Gruppo vuoto separato da virgola assieme a indirizzo valido (sintassi RFC standard)
    addrs3, malformed3 = parse_address_header("Team:;, alice@example.test")
    assert addrs3 == ("alice@example.test",)
    assert malformed3 is False

    # Gruppo vuoto senza virgola separatrice: indirizzo recuperato ma segnalato malformed
    addrs_no_comma, malformed_no_comma = parse_address_header("Team:; alice@example.test")
    assert addrs_no_comma == ("alice@example.test",)
    assert malformed_no_comma is True

    # Normalizzazione con gruppo vuoto non deve fallire
    raw = _make_msg("msg-group", to_addr="undisclosed-recipients:;")
    norm = normalize_gmail_message(raw, ["owner@example.test"])
    assert norm.to_addresses == ()
    assert norm.body_new == "Contenuto del messaggio valido"


def test_getaddresses_compat_runtime_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifica il comportamento del fallback di getaddresses compatibile sui runtime privi di strict."""
    real_getaddresses = __import__("email.utils").utils.getaddresses

    # 1. Simula runtime Python legacy (pre-3.12.4) in cui la firma non accetta strict
    # e il comportamento di default era permissivo (equivalente a strict=False)
    def fake_legacy_getaddresses(fieldvalues: Any) -> list[tuple[str, str]]:
        return cast(list[tuple[str, str]], real_getaddresses(fieldvalues, strict=False))

    monkeypatch.setattr(gmail_domain, "getaddresses", fake_legacy_getaddresses)
    assert gmail_domain._supports_strict_getaddresses() is False

    res = gmail_domain._getaddresses_compat(["alice@example.test,"])
    assert res == [("", "alice@example.test")]

    addrs, malformed = parse_address_header("alice@example.test,")
    assert addrs == ("alice@example.test",)
    assert malformed is False

    addrs_corrupt, malformed_corrupt = parse_address_header("Name <good@example.test>, malformed <")
    assert addrs_corrupt == ("good@example.test",)
    assert malformed_corrupt is True

    # 2. Capability detection attiva: se getaddresses solleva TypeError interno contenente
    # anche la parola 'strict', l'eccezione non deve essere mascherata né generare una seconda chiamata
    calls: list[dict[str, Any]] = []

    def fake_strict_internal_error_getaddresses(
        fieldvalues: Any, *, strict: bool = False
    ) -> list[tuple[str, str]]:
        calls.append({"strict": strict})
        raise TypeError("strict token validation failed internally in email parser")

    monkeypatch.setattr(gmail_domain, "getaddresses", fake_strict_internal_error_getaddresses)
    assert gmail_domain._supports_strict_getaddresses() is True

    with pytest.raises(TypeError, match="strict token validation failed internally"):
        gmail_domain._getaddresses_compat(["test@example.test"])
    assert len(calls) == 1  # Nessun secondo tentativo o fallback con keyword rimossa

    # 3. Verifica che TypeError per parametri errati nel ramo legacy si propaghi senza retry
    legacy_calls: list[Any] = []

    def buggy_legacy_getaddresses(fieldvalues: Any) -> list[tuple[str, str]]:
        legacy_calls.append(fieldvalues)
        raise TypeError("expected sequence of strings, got int")

    monkeypatch.setattr(gmail_domain, "getaddresses", buggy_legacy_getaddresses)
    assert gmail_domain._supports_strict_getaddresses() is False
    with pytest.raises(TypeError, match="expected sequence of strings"):
        gmail_domain._getaddresses_compat(["test@example.test"])
    assert len(legacy_calls) == 1


# ==============================================================================
# 3. Test: header misto valido + malformato, conservando valido e testo
# ==============================================================================


def test_mixed_header_preserves_valid_and_body() -> None:
    mixed_to = "alice@example.test, invalid[at]broken, bob@example.test"
    addrs, malformed = parse_address_header(mixed_to)
    assert "alice@example.test" in addrs
    assert "bob@example.test" in addrs
    assert malformed is True

    raw = _make_msg("msg-mixed", to_addr=mixed_to, body_text="Corpo conservato intatto")
    norm = normalize_gmail_message(raw, ["owner@example.test"])
    assert norm.to_addresses == ("alice@example.test", "bob@example.test")
    assert norm.body_new == "Corpo conservato intatto"
    assert "recipient_header_parse_warning" in norm.evidence.truncation_codes


# ==============================================================================
# 4. Test: diagnostica incompleta senza PII
# ==============================================================================


def test_diagnostics_sanitized_no_pii() -> None:
    malformed_value = "Sensitive Name <sensitive.email@secret.test>, invalid@@syntax"
    raw = _make_msg("msg-pii", to_addr=malformed_value, body_text="Testo privato")
    norm = normalize_gmail_message(raw, ["owner@example.test"])

    payload = norm.provenance_payload()
    evidence = payload.get("evidence", {})
    truncation_codes = evidence.get("truncation_codes", [])

    # Deve contenere solo codici statici sanitizzati
    assert "recipient_header_parse_warning" in truncation_codes
    for code in truncation_codes:
        assert "sensitive" not in code.lower()
        assert "@" not in code
        assert "secret" not in code.lower()


@pytest.mark.asyncio
async def test_no_pii_in_provenance_error_detail_and_caplog(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verifica che nessuna PII appaia in provenance, error_detail o caplog."""
    from gbrain.worker import gmail_sync

    sentinel_pii = "SECRET_USER_PII_john.doe@confidential.bank"
    db_sentinel = "DATABASE_PII_PASSWORD_SECRET_123"

    # 1. Nessuna PII nel payload di provenance anche in presenza di header malevolo/corrotto
    raw = _make_msg("msg-pii-sentinel", to_addr=f"Confidential <{sentinel_pii}>, invalid@@syntax")
    norm = normalize_gmail_message(raw, ["owner@example.test"])
    prov = norm.provenance_payload()
    evidence = prov.get("evidence", {})
    assert sentinel_pii not in str(evidence.get("truncation_codes", []))
    assert "@" not in str(evidence.get("truncation_codes", []))

    # 2. Eccezione primaria con sentinel PII gestita da worker senza alcuna esposizione nei log
    job_id = uuid4()
    account_id = uuid4()
    mock_job = MagicMock()
    mock_job.id = job_id
    mock_job.status = ImportJobStatus.RUNNING
    mock_job.error_code = None
    mock_job.error_detail = None
    mock_job.finished_at = None
    mock_job.checkpoint = {}

    mock_session = MagicMock()
    mock_session.get = AsyncMock(return_value=mock_job)
    mock_session.commit = AsyncMock()
    mock_session.rollback = AsyncMock()

    class FakeSessionContext:
        async def __aenter__(self) -> MagicMock:
            return mock_session

        async def __aexit__(self, *args: Any) -> None:
            pass

    mock_db = MagicMock()
    mock_db.session_factory.return_value = FakeSessionContext()
    mock_db.close = AsyncMock()

    with (
        patch("gbrain.worker.Database", return_value=mock_db),
        patch(
            "gbrain.worker.execute_gmail_job",
            side_effect=KeyError(f"BUG_{sentinel_pii}"),
        ),
    ):
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(KeyError, match=sentinel_pii):
                await gmail_sync.func(job_id=str(job_id), account_id=str(account_id), mode="full")

        assert sentinel_pii not in caplog.text
        assert mock_job.error_code == "unexpected_error"
        assert sentinel_pii not in (mock_job.error_detail or "")

    # 3. Fallimento persistenza con sentinel PII nei log di DB non deve esporre PII
    caplog.clear()
    mock_session.commit.side_effect = RuntimeError(f"COMMIT_CRASH_{db_sentinel}")
    with (
        patch("gbrain.worker.Database", return_value=mock_db),
        patch(
            "gbrain.worker.execute_gmail_job",
            side_effect=KeyError(f"BUG_{sentinel_pii}"),
        ),
    ):
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(KeyError, match=sentinel_pii):
                await gmail_sync.func(job_id=str(job_id), account_id=str(account_id), mode="full")

        assert sentinel_pii not in caplog.text
        assert db_sentinel not in caplog.text


# ==============================================================================
# 5. Test: guardie SENT e mittente proprietario invariate
# ==============================================================================


def test_security_guards_sent_and_owner_preserved() -> None:
    # 1. Messaggio senza label SENT viene bloccato alla fetch con errore di perimetro
    gateway = FakeGmailGateway(
        {
            "profile": {"emailAddress": "owner@example.test", "historyId": "1001"},
            "messages": [
                {
                    "id": "msg-inbox-only",
                    "threadId": "t-inbox",
                    "historyId": "1001",
                    "labelIds": ["INBOX"],  # Manca la label SENT!
                    "internalDate": str(int(_MSG_DATE.timestamp() * 1000)),
                    "snippet": "Ricevuto non inviato",
                    "payload": _make_msg("msg-inbox-only", label_ids=("INBOX",)).payload,
                }
            ],
        }
    )
    connector = GmailSentConnector(gateway, ("owner@example.test",))
    with pytest.raises(ConnectorError) as captured:
        connector.fetch_item(GmailLocator("msg-inbox-only"), RequestBudget(2, 2))
    assert captured.value.category == ConnectorErrorCategory.PERIMETER

    # 2. Verifica mittente proprietario: se il mittente non è confermato, attribuzione AMBIGUOUS
    raw_external = _make_msg("msg-ext", from_addr="attacker@other.test")
    norm_external = normalize_gmail_message(raw_external, ["owner@example.test"])
    assert norm_external.attribution == AttributionConfidence.AMBIGUOUS
    assert norm_external.attributed_owner_alias is None

    # Se invece From corrisponde all'owner confermato, attribution CONFIRMED_ALIAS
    raw_owner = _make_msg("msg-owner", from_addr="owner@example.test")
    norm_owner = normalize_gmail_message(raw_owner, ["owner@example.test"])
    assert norm_owner.attribution == AttributionConfidence.CONFIRMED_ALIAS
    assert norm_owner.attributed_owner_alias == "owner@example.test"

    # 3. From completamente malformato (HeaderParseError) non deve consentire bypass dell'owner
    raw_bad_from = _make_msg("msg-bad-from", from_addr="<broken@@syntax>")
    norm_bad_from = normalize_gmail_message(raw_bad_from, ["owner@example.test"])
    assert norm_bad_from.attribution == AttributionConfidence.AMBIGUOUS
    assert norm_bad_from.attributed_owner_alias is None
    assert "from_header_parse_warning" in norm_bad_from.evidence.truncation_codes


# ==============================================================================
# 6. Test: contenuto permanente confinato al singolo item e successivi importati
# ==============================================================================


def test_permanent_parse_error_isolated_to_single_item() -> None:
    valid_1 = {
        "id": "msg-val-1",
        "threadId": "t-1",
        "historyId": "1001",
        "labelIds": ["SENT"],
        "internalDate": str(int(_MSG_DATE.timestamp() * 1000)),
        "snippet": "Valida 1",
        "payload": _make_msg("msg-val-1", body_text="Valida 1").payload,
    }
    # Item con errore di parsing permanente (corpo non disponibile)
    bad_payload = {
        "mimeType": "multipart/mixed",
        "headers": [{"name": "From", "value": "owner@example.test"}],
        "parts": [],
    }
    bad_item = {
        "id": "msg-bad",
        "threadId": "t-2",
        "historyId": "1002",
        "labelIds": ["SENT"],
        "internalDate": str(int(_MSG_DATE.timestamp() * 1000)),
        "snippet": "MIME corrotto",
        "payload": bad_payload,
    }
    valid_2 = {
        "id": "msg-val-2",
        "threadId": "t-3",
        "historyId": "1003",
        "labelIds": ["SENT"],
        "internalDate": str(int(_MSG_DATE.timestamp() * 1000)),
        "snippet": "Valida 2",
        "payload": _make_msg("msg-val-2", body_text="Valida 2").payload,
    }

    gateway = FakeGmailGateway(
        {
            "profile": {"emailAddress": "owner@example.test", "historyId": "1003"},
            "messages": [valid_1, bad_item, valid_2],
        }
    )
    connector = GmailSentConnector(gateway, ("owner@example.test",))
    uow = InMemoryGmailUnitOfWork()
    engine = GmailSyncEngine(connector, uow, batch_size=10)
    cp = _checkpoint()
    result = engine.full_sync(_SELECTION, cp, RequestBudget(20, 20))

    # Esattamente 2 importati e 1 con gap registrato
    assert result.persisted_items == 2
    assert result.not_imported_items == 1
    assert len(result.gaps) == 1
    assert result.gaps[0].gmail_message_id == "msg-bad"
    assert ("acc-1", "msg-val-1") in uow.messages
    assert ("acc-1", "msg-val-2") in uow.messages
    assert ("acc-1", "msg-bad") not in uow.messages


# ==============================================================================
# 7. Test: errore infrastrutturale/programmazione propaga
# ==============================================================================


def test_infrastructure_and_programming_errors_propagate() -> None:
    class FailingGateway(FakeGmailGateway):
        def fetch_sent_message(self, locator: Any, budget: Any, format: str = "full") -> Any:
            raise ConnectorError(
                ConnectorErrorCategory.TRANSIENT,
                "Connessione socket interrotta",
                retryable=True,
            )

    gateway = FailingGateway(
        {
            "profile": {"emailAddress": "owner@example.test", "historyId": "1001"},
            "messages": [
                {
                    "id": "msg-1",
                    "threadId": "t-1",
                    "historyId": "1001",
                    "labelIds": ["SENT"],
                    "internalDate": str(int(_MSG_DATE.timestamp() * 1000)),
                    "snippet": "Test",
                    "payload": _make_msg("msg-1").payload,
                }
            ],
        }
    )
    connector = GmailSentConnector(gateway, ("owner@example.test",))
    uow = InMemoryGmailUnitOfWork()
    engine = GmailSyncEngine(connector, uow)
    cp = _checkpoint()

    # L'errore infrastrutturale transitorio NON deve essere scartato come item gap, DEVE propagare!
    with pytest.raises(ConnectorError) as exc_info:
        engine.full_sync(_SELECTION, cp, RequestBudget(10, 10))
    assert exc_info.value.category == ConnectorErrorCategory.TRANSIENT


# ==============================================================================
# 8. Test: eccezione inattesa terminalizza ImportJob coerentemente
# ==============================================================================


@pytest.mark.asyncio
async def test_unexpected_exception_terminates_import_job() -> None:
    from gbrain.worker import gmail_sync

    job_id = uuid4()
    account_id = uuid4()

    mock_job = MagicMock()
    mock_job.id = job_id
    mock_job.status = ImportJobStatus.RUNNING
    mock_job.checkpoint = {"phase": "running", "step": 1}
    mock_job.error_code = None
    mock_job.error_detail = None
    mock_job.finished_at = None

    mock_session = MagicMock()
    mock_session.get = AsyncMock(return_value=mock_job)
    mock_session.commit = AsyncMock()

    class FakeSessionContext:
        async def __aenter__(self) -> MagicMock:
            return mock_session

        async def __aexit__(self, *args: Any) -> None:
            pass

    mock_db = MagicMock()
    mock_db.session_factory.return_value = FakeSessionContext()
    mock_db.close = AsyncMock()

    # Simuliamo un bug imprevisto o errore non gestito in execute_gmail_job
    with (
        patch("gbrain.worker.Database", return_value=mock_db),
        patch(
            "gbrain.worker.execute_gmail_job",
            side_effect=KeyError("unhandled_programming_bug"),
        ),
    ):
        with pytest.raises(KeyError, match="unhandled_programming_bug"):
            await gmail_sync.func(
                job_id=str(job_id),
                account_id=str(account_id),
                mode="full",
            )

    # Il job applicativo DEVE essere stato terminalizzato a FAILED
    assert mock_job.status == ImportJobStatus.FAILED
    assert mock_job.finished_at is not None
    assert mock_job.error_code == "unexpected_error"
    assert "Errore inatteso" in mock_job.error_detail
    # Il checkpoint deve aver preservato lo stato esistente
    assert mock_job.checkpoint.get("phase") == "failed_unexpected"
    assert mock_job.checkpoint.get("step") == 1
    mock_session.commit.assert_awaited()


@pytest.mark.asyncio
async def test_termination_failure_preserves_primary_exception() -> None:
    """Verifica che il fallimento della persistenza durante la terminalizzazione non mascheri l'errore primario."""
    from gbrain.worker import gmail_sync

    job_id = uuid4()
    account_id = uuid4()

    mock_job = MagicMock()
    mock_job.id = job_id
    mock_job.status = ImportJobStatus.RUNNING
    mock_job.checkpoint = {}
    mock_job.error_code = None

    mock_session = MagicMock()
    mock_session.get = AsyncMock(return_value=mock_job)
    mock_session.commit = AsyncMock(side_effect=RuntimeError("DB_CONNECTION_LOST"))
    mock_session.rollback = AsyncMock()

    class FakeSessionContext:
        async def __aenter__(self) -> MagicMock:
            return mock_session

        async def __aexit__(self, *args: Any) -> None:
            pass

    mock_db = MagicMock()
    mock_db.session_factory.return_value = FakeSessionContext()
    mock_db.close = AsyncMock()

    with (
        patch("gbrain.worker.Database", return_value=mock_db),
        patch(
            "gbrain.worker.execute_gmail_job",
            side_effect=ValueError("primary_business_error"),
        ),
    ):
        with pytest.raises(ValueError, match="primary_business_error"):
            await gmail_sync.func(job_id=str(job_id), account_id=str(account_id), mode="full")

    mock_session.rollback.assert_awaited()


@pytest.mark.asyncio
async def test_terminal_jobs_not_overwritten() -> None:
    """Verifica che un job già in stato terminale non venga sovrascritto né alterato."""
    for terminal_status in (
        ImportJobStatus.COMPLETED,
        ImportJobStatus.COMPLETED_WITH_WARNINGS,
        ImportJobStatus.CANCELLED,
        ImportJobStatus.SUPERSEDED,
        ImportJobStatus.FAILED,
    ):
        mock_job = MagicMock()
        mock_job.id = uuid4()
        mock_job.status = terminal_status
        mock_job.error_code = "original_specific_error"
        mock_job.finished_at = datetime(2024, 1, 1, tzinfo=UTC)
        mock_job.checkpoint = {"phase": "original_phase"}

        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_job)
        mock_session.commit = AsyncMock()

        class FakeSessionContext:
            def __init__(self, s: MagicMock) -> None:
                self._s = s

            async def __aenter__(self) -> MagicMock:
                return self._s

            async def __aexit__(self, *args: Any) -> None:
                pass

        mock_db = MagicMock()
        mock_db.session_factory.return_value = FakeSessionContext(mock_session)

        await best_effort_terminate_job(
            mock_db,
            mock_job.id,
            status=ImportJobStatus.FAILED,
            error_code="unexpected_error",
            phase="failed_unexpected",
        )

        assert mock_job.status == terminal_status
        assert mock_job.error_code == "original_specific_error"
        assert mock_job.checkpoint == {"phase": "original_phase"}
        mock_session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_retryable_connector_error_propagates_without_premature_failed() -> None:
    """Verifica che un errore transitorio (retryable) non terminalizzi prematuramente il job a FAILED."""
    job_id = uuid4()
    account_id = uuid4()

    mock_job = MagicMock()
    mock_job.id = job_id
    mock_job.status = ImportJobStatus.RUNNING
    mock_job.mode = "full"
    mock_job.checkpoint = {"phase": "running"}
    mock_job.error_code = None
    mock_job.error_detail = None
    mock_job.source_policy_id = uuid4()
    mock_job.processed_items = 0
    mock_job.policy_snapshot = {
        "request_budget": 100,
        "item_limit": 50,
        "batch_size": 20,
        "after": "2024-01-01T00:00:00Z",
        "before": "2024-02-01T00:00:00Z",
    }

    mock_account = MagicMock()
    mock_account.id = account_id
    mock_account.confirmed_aliases = ["owner@example.test"]

    mock_session = MagicMock()
    mock_session.get = AsyncMock(return_value=mock_job)
    mock_session.scalar = AsyncMock(return_value=None)
    mock_session.commit = AsyncMock()

    class FakeSessionContext:
        async def __aenter__(self) -> MagicMock:
            return mock_session

        async def __aexit__(self, *args: Any) -> None:
            pass

    mock_db = MagicMock()
    mock_db.session_factory.return_value = FakeSessionContext()

    settings = get_settings()

    transient_exc = ConnectorError(
        ConnectorErrorCategory.TRANSIENT,
        "Temporary network issue",
        retryable=True,
        retry_after_seconds=5,
    )

    gateway = FakeGmailGateway({"messages": []})
    gateway.queue_failure("messages.list", transient_exc)

    with (
        patch(
            "gbrain.application.gmail_jobs._load_job_context",
            return_value=(mock_job, mock_account, _checkpoint()),
        ),
        patch(
            "gbrain.application.gmail_jobs.decrypt_credentials", return_value=MagicMock(valid=True)
        ),
    ):
        with pytest.raises(GmailJobTransientError, match="Temporary network issue"):
            await execute_gmail_job(
                mock_db,
                settings,
                job_id=str(job_id),
                account_id=str(account_id),
                mode="full",
                gateway_builder=cast(Any, lambda acc, sett: gateway),
            )

        # Il job NON deve essere stato marcato FAILED prematuramente in execute_gmail_job!
        assert mock_job.status != ImportJobStatus.FAILED
        assert mock_job.status == ImportJobStatus.RUNNING
        assert mock_job.error_code == "gmail_transient"


# ==============================================================================
# 9. Test: multipagina, checkpoint e ripresa senza duplicati dopo failure
# ==============================================================================


def test_multipage_checkpoint_and_resumption_no_duplicates() -> None:
    msg_1 = {
        "id": "msg-p-1",
        "threadId": "t-p-1",
        "historyId": "1001",
        "labelIds": ["SENT"],
        "internalDate": str(int(_MSG_DATE.timestamp() * 1000)),
        "snippet": "Msg 1",
        "payload": _make_msg("msg-p-1", body_text="Corpo 1").payload,
    }
    msg_2 = {
        "id": "msg-p-2",
        "threadId": "t-p-2",
        "historyId": "1002",
        "labelIds": ["SENT"],
        "internalDate": str(int((_MSG_DATE.timestamp() + 5) * 1000)),
        "snippet": "Msg 2",
        "payload": _make_msg("msg-p-2", body_text="Corpo 2").payload,
    }
    fixture = {
        "profile": {"emailAddress": "owner@example.test", "historyId": "1002"},
        "messages": [msg_1, msg_2],
    }
    gateway = FakeGmailGateway(fixture, page_size=1)
    connector = GmailSentConnector(gateway, ("owner@example.test",))
    uow = InMemoryGmailUnitOfWork()
    engine = GmailSyncEngine(connector, uow, batch_size=1)

    initial_cp = _checkpoint()

    # 1. Prima esecuzione con budget di richieste pari a 2 (1 list + 1 fetch)
    first_run = engine.full_sync(_SELECTION, initial_cp, RequestBudget(2, 2))
    assert first_run.persisted_items == 1
    assert ("acc-1", "msg-p-1") in uow.messages
    assert first_run.checkpoint.page_token == "1"  # noqa: S105

    # 2. Ripresa dal checkpoint per la seconda pagina con budget adeguato
    second_run = engine.full_sync(_SELECTION, first_run.checkpoint, RequestBudget(10, 10))
    assert second_run.persisted_items == 1
    assert ("acc-1", "msg-p-2") in uow.messages
    assert second_run.checkpoint.page_token is None

    # 3. Terza esecuzione (replay): nessun nuovo inserimento, entrambi presenti
    third_run = engine.full_sync(_SELECTION, second_run.checkpoint, RequestBudget(10, 10))
    assert third_run.persisted_items == 0
    assert third_run.unchanged_items == 2
    assert len(uow.messages) == 2
