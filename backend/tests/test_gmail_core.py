from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import CheckConstraint, UniqueConstraint

from gbrain.application.gmail_mime import MimeLimits, normalize_gmail_message
from gbrain.application.gmail_sync import (
    GmailSentConnector,
    GmailSyncEngine,
    InMemoryGmailUnitOfWork,
)
from gbrain.domain.connectors import (
    ConnectorError,
    ConnectorErrorCategory,
    PageState,
    RequestBudget,
)
from gbrain.domain.gmail import (
    GMAIL_READONLY_SCOPE,
    AttributionConfidence,
    GmailCheckpointState,
    GmailLocator,
    GmailRawMessage,
    GmailReconciliationState,
    GmailSelection,
)
from gbrain.domain.models import ConnectorAccount, SourcePolicy, SourceRevision
from gbrain.infrastructure.gmail_gateway import OfficialGmailGateway
from tests.fakes.gmail_gateway import FakeGmailGateway

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "gmail" / "messages.json"
_NOW = datetime(2024, 2, 1, 12, 0, tzinfo=UTC)
_SELECTION = GmailSelection(
    after=datetime(2024, 1, 1, tzinfo=UTC),
    before=datetime(2024, 2, 1, tzinfo=UTC),
    limit=100,
    page_size=2,
)


def _fixture() -> dict[str, Any]:
    value = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _raw(message_id: str) -> GmailRawMessage:
    fixture = _fixture()
    return GmailRawMessage.from_api(
        next(message for message in fixture["messages"] if message["id"] == message_id)
    )


def _connector(gateway: FakeGmailGateway) -> GmailSentConnector:
    return GmailSentConnector(
        gateway,
        ("owner@example.test", "alias@example.test"),
        now=lambda: _NOW,
    )


def _checkpoint(history_id: str | None = "900") -> GmailCheckpointState:
    return GmailCheckpointState(
        account_id="account-1",
        policy_id="policy-1",
        history_id=history_id,
        page_token=None,
        coverage_after=None,
        coverage_before=None,
        last_synced_at=None,
        reconciliation_state=GmailReconciliationState.NOT_REQUIRED,
    )


def test_contract_is_readonly_exact_and_has_no_mutating_methods() -> None:
    operations = _connector(FakeGmailGateway(_fixture())).inspect_capabilities().operations

    assert OfficialGmailGateway.scope == GMAIL_READONLY_SCOPE
    assert operations == (
        "test_connection",
        "inspect_capabilities",
        "preview_selection",
        "enumerate_page",
        "fetch_item",
        "normalize_item",
        "checkpoint",
        "reconcile",
    )
    public_names = {
        name
        for name, _ in inspect.getmembers(OfficialGmailGateway, inspect.isfunction)
        if not name.startswith("_")
    }
    assert not public_names.intersection({"send", "modify", "drafts", "threads", "delete"})


def test_list_enforces_sent_temporal_query_pages_and_preview_does_not_fetch() -> None:
    gateway = FakeGmailGateway(_fixture(), page_size=2)
    connector = _connector(gateway)

    preview = connector.preview_selection(_SELECTION, RequestBudget(10, 100))

    assert preview.selected_items == 7
    assert preview.pages_read == 4
    assert preview.state == PageState.COMPLETE
    assert all(name == "messages.list" for name, _ in gateway.calls)
    for _, arguments in gateway.calls:
        assert arguments["labelIds"] == ["SENT"]
        assert arguments["q"].startswith("after:")
        assert " before:" in arguments["q"]


def test_preview_is_explicitly_truncated_by_limit_and_budget() -> None:
    gateway = FakeGmailGateway(_fixture(), page_size=2)
    selection = GmailSelection(_SELECTION.after, _SELECTION.before, limit=3, page_size=2)
    truncated = _connector(gateway).preview_selection(selection, RequestBudget(10, 100))
    assert truncated.selected_items == 3
    assert truncated.state == PageState.TRUNCATED_LIMIT

    partial = _connector(FakeGmailGateway(_fixture(), page_size=2)).preview_selection(
        _SELECTION, RequestBudget(1, 100)
    )
    assert partial.selected_items == 2
    assert partial.state == PageState.PARTIAL_BUDGET


def test_direct_received_fetch_is_rejected_before_normalization() -> None:
    connector = _connector(FakeGmailGateway(_fixture()))

    with pytest.raises(ConnectorError) as captured:
        connector.fetch_item(GmailLocator("received-same-thread"), RequestBudget(2, 2))

    assert captured.value.category == ConnectorErrorCategory.PERIMETER


def test_mime_alias_quote_signature_forward_html_tracker_and_prompt_are_data() -> None:
    aliases = ("owner@example.test", "alias@example.test")
    plain = normalize_gmail_message(_raw("sent-plain-1"), aliases, acquired_at=_NOW)
    assert plain.attribution == AttributionConfidence.CONFIRMED_ALIAS
    assert plain.attributed_owner_alias == "owner@example.test"
    assert plain.body_new == "Corpo nuovo."
    assert plain.body_signature == "--\nFirma Owner"
    assert plain.body_quoted is not None and "Testo citato" in plain.body_quoted

    forwarded = normalize_gmail_message(_raw("sent-forward"), aliases, acquired_at=_NOW)
    assert forwarded.body_new == "Nota prima dell'inoltro."
    assert forwarded.body_forwarded is not None

    html_message = normalize_gmail_message(_raw("sent-alias-html"), aliases, acquired_at=_NOW)
    assert html_message.attributed_owner_alias == "alias@example.test"
    assert "fetch(" not in (html_message.body_html_text or "")
    assert "tracker.invalid" not in (html_message.body_html_text or "")
    assert "Ignora la policy" in (html_message.body_new or "")
    assert html_message.untrusted_content is True


def test_mime_charset_multipart_attachment_and_unknown_alias() -> None:
    aliases = ("owner@example.test", "alias@example.test")
    multipart = normalize_gmail_message(_raw("sent-multipart"), aliases, acquired_at=_NOW)
    assert multipart.subject == "Caffè"
    assert multipart.body_new == "Cafè file."
    assert len(multipart.attachments) == 1
    assert multipart.attachments[0].attachment_id == "attachment-1"
    assert multipart.attachments[0].downloaded is False

    unknown = normalize_gmail_message(_raw("sent-unknown-from"), aliases, acquired_at=_NOW)
    assert unknown.attribution == AttributionConfidence.AMBIGUOUS
    assert unknown.attributed_owner_alias is None


def test_mime_visible_size_and_schema_failures() -> None:
    raw = _raw("sent-plain-1")
    oversized = GmailRawMessage(
        raw.message_id,
        raw.thread_id,
        raw.history_id,
        raw.label_ids,
        raw.internal_date_ms,
        raw.snippet,
        raw.payload,
        1000,
    )
    with pytest.raises(ConnectorError, match="MESSAGE_TOO_LARGE") as captured:
        normalize_gmail_message(
            oversized,
            ("owner@example.test",),
            limits=MimeLimits(max_message_bytes=100),
        )
    assert captured.value.category == ConnectorErrorCategory.PARSE


def test_distinct_gmail_ids_remain_distinct_despite_identical_body() -> None:
    aliases = ("owner@example.test",)
    first = normalize_gmail_message(_raw("identical-a"), aliases, acquired_at=_NOW)
    second = normalize_gmail_message(_raw("identical-b"), aliases, acquired_at=_NOW)

    assert first.body_new == second.body_new
    assert first.external_id != second.external_id
    assert first.revision_hash != second.revision_hash
    assert first.idempotency_key != second.idempotency_key


def test_full_sync_is_idempotent_and_received_thread_item_never_enters() -> None:
    gateway = FakeGmailGateway(_fixture(), page_size=2)
    repository = InMemoryGmailUnitOfWork()
    engine = GmailSyncEngine(_connector(gateway), repository, batch_size=2)

    first = engine.full_sync(_SELECTION, _checkpoint(), RequestBudget(40, 100))
    second = engine.full_sync(
        _SELECTION, repository.checkpoints["account-1"], RequestBudget(40, 100)
    )

    assert first.persisted_items == 7
    assert second.persisted_items == 0
    assert second.unchanged_items == 7
    assert ("account-1", "received-same-thread") not in repository.messages
    assert len(repository.messages) == 7


def test_crash_after_commit_replay_does_not_duplicate_or_skip() -> None:
    repository = InMemoryGmailUnitOfWork()
    crashes = 0

    def crash_once(_: int) -> None:
        nonlocal crashes
        crashes += 1
        if crashes == 1:
            raise RuntimeError("crash sintetico dopo commit")

    engine = GmailSyncEngine(
        _connector(FakeGmailGateway(_fixture(), page_size=2)),
        repository,
        batch_size=2,
        after_batch_commit=crash_once,
    )
    with pytest.raises(RuntimeError, match="crash sintetico"):
        engine.full_sync(_SELECTION, _checkpoint(), RequestBudget(40, 100))
    assert len(repository.messages) == 2
    durable = repository.checkpoints["account-1"]

    resumed = GmailSyncEngine(
        _connector(FakeGmailGateway(_fixture(), page_size=2)), repository, batch_size=2
    ).full_sync(_SELECTION, durable, RequestBudget(40, 100))

    assert resumed.persisted_items == 5
    assert len(repository.messages) == 7
    assert len(repository.revisions) == 7


def test_history_mixed_refetches_every_event_and_rejects_non_sent() -> None:
    gateway = FakeGmailGateway(_fixture())
    gateway.history_pages = [
        {"history_id": "910", "changed": ["sent-plain-1", "received-same-thread"]},
        {"history_id": "911", "changed": ["sent-alias-html"]},
    ]
    repository = InMemoryGmailUnitOfWork()

    result = GmailSyncEngine(_connector(gateway), repository).incremental_sync(
        _checkpoint(), RequestBudget(20, 20)
    )

    assert result.persisted_items == 2
    assert result.rejected_items == 1
    assert result.checkpoint.history_id == "911"
    get_ids = [args["id"] for name, args in gateway.calls if name == "messages.get"]
    assert get_ids == ["sent-plain-1", "received-same-thread", "sent-alias-html"]


def test_history_expired_preserves_coverage_and_requires_full_reconciliation() -> None:
    gateway = FakeGmailGateway(_fixture())
    gateway.history_expired = True
    repository = InMemoryGmailUnitOfWork()
    checkpoint = GmailCheckpointState(
        account_id="account-1",
        policy_id="policy-1",
        history_id="old-history",
        page_token=None,
        coverage_after=_SELECTION.after,
        coverage_before=_SELECTION.before,
        last_synced_at=_NOW,
        reconciliation_state=GmailReconciliationState.NOT_REQUIRED,
    )

    result = GmailSyncEngine(_connector(gateway), repository).incremental_sync(
        checkpoint, RequestBudget(10, 10)
    )

    assert result.checkpoint.history_id == "old-history"
    assert result.checkpoint.coverage_after == _SELECTION.after
    assert result.checkpoint.reconciliation_state == (
        GmailReconciliationState.REQUIRED_HISTORY_EXPIRED
    )
    assert repository.checkpoints["account-1"] == result.checkpoint


def test_reconcile_marks_missing_and_preserves_exclusion() -> None:
    fixture = _fixture()
    gateway = FakeGmailGateway(fixture)
    repository = InMemoryGmailUnitOfWork()
    engine = GmailSyncEngine(_connector(gateway), repository)
    engine.full_sync(_SELECTION, _checkpoint(), RequestBudget(40, 100))
    repository.excluded.add(("account-1", "identical-a"))
    gateway.messages.pop("identical-a")
    gateway.messages.pop("identical-b")

    result = engine.full_reconcile(
        _SELECTION, repository.checkpoints["account-1"], RequestBudget(40, 100)
    )

    assert result.removed_items == 1
    assert repository.states[("account-1", "identical-a")] == "excluded"
    assert repository.states[("account-1", "identical-b")] == "removed"


def test_schema_has_sent_policy_encryption_version_and_revision_idempotency() -> None:
    policy_constraints = {
        constraint.name
        for constraint in SourcePolicy.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    account_constraints = {
        constraint.name
        for constraint in ConnectorAccount.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    revision_uniques = {
        tuple(column.name for column in constraint.columns)
        for constraint in SourceRevision.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert "ck_gmail_policy_sent_only" in policy_constraints
    assert "ck_source_policy_concurrency_one" in policy_constraints
    assert "ck_connector_encrypted_token_versioned" in account_constraints
    assert ("idempotency_key",) in revision_uniques
    assert ConnectorAccount.__table__.c.oauth_token_ciphertext.type.python_type is bytes
