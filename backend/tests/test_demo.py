from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from typing import Any, cast

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from gbrain.application.auth import _new_session, _session_model
from gbrain.application.demo import (
    DEMO_CASES,
    DEMO_EXTERNAL_ID_PREFIX,
    DEMO_MARKER,
    DEMO_SOURCE_KIND,
    DemoModeDisabledError,
    build_demo_dataset,
    seed_demo_fixtures,
)
from gbrain.cli import _seed_demo
from gbrain.config import Settings
from gbrain.domain.models import ImportJob, OwnerAccount, SourceItem, SourceRevision
from gbrain.presentation.api import api_router
from gbrain.presentation.dependencies import Principal, get_db_session, require_principal


def _settings(*, demo_mode: bool = False) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        demo_mode=demo_mode,
        postgres_password=SecretStr("pg-secret"),
        auth_secret=SecretStr("a" * 40),
        neo4j_password=SecretStr("neo-secret"),
    )


class _FakeTransaction(AbstractAsyncContextManager[None]):
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        return None


class _FakeSession:
    def __init__(self) -> None:
        self.records: dict[tuple[type[Any], uuid.UUID], Any] = {}

    def begin(self) -> _FakeTransaction:
        return _FakeTransaction()

    async def get(self, model: type[Any], identifier: uuid.UUID) -> Any | None:
        return self.records.get((model, identifier))

    def add(self, value: Any) -> None:
        self.records[(type(value), value.id)] = value

    async def delete(self, value: Any) -> None:
        self.records.pop((type(value), value.id), None)

    async def flush(self) -> None:
        return None

    async def scalar(self, statement: object) -> Any | None:
        sql = str(statement).lower()
        if "count(" not in sql:
            return None
        if "source_revisions" in sql:
            return sum(model is SourceRevision for model, _ in self.records)
        if "source_items" in sql:
            return sum(
                model is SourceItem
                and value.source_kind == DEMO_SOURCE_KIND
                and value.external_id.startswith(DEMO_EXTERNAL_ID_PREFIX)
                for (model, _), value in self.records.items()
            )
        if "import_jobs" in sql:
            return sum(
                model is ImportJob and value.source_kind == DEMO_SOURCE_KIND
                for (model, _), value in self.records.items()
            )
        return 0


def test_demo_mode_is_disabled_by_default() -> None:
    assert _settings().demo_mode is False


async def test_seed_is_refused_outside_demo_without_touching_session() -> None:
    class _ForbiddenSession:
        def begin(self) -> None:
            raise AssertionError("Il database non deve essere toccato fuori modalità demo")

    try:
        await seed_demo_fixtures(
            cast(Any, _ForbiddenSession()),
            _settings(demo_mode=False),
        )
    except DemoModeDisabledError:
        pass
    else:
        raise AssertionError("Il seed doveva essere rifiutato")


async def test_cli_refuses_demo_before_opening_database(monkeypatch: Any) -> None:
    class _ForbiddenDatabase:
        def __init__(self, settings: Settings) -> None:
            del settings
            raise AssertionError("La CLI non deve aprire il database fuori modalità demo")

    monkeypatch.setattr("gbrain.cli.get_settings", lambda: _settings(demo_mode=False))
    monkeypatch.setattr("gbrain.cli.Database", _ForbiddenDatabase)

    try:
        await _seed_demo(reset=False)
    except DemoModeDisabledError:
        pass
    else:
        raise AssertionError("La CLI doveva rifiutare il seed")


async def test_seed_is_deterministic_idempotent_and_reset_preserves_non_demo() -> None:
    session = _FakeSession()
    non_demo_id = uuid.uuid4()
    non_demo = SourceItem(
        id=non_demo_id,
        source_kind="document",
        external_id="record:non-demo",
        canonical_hash="f" * 64,
    )
    session.add(non_demo)

    first = await seed_demo_fixtures(cast(Any, session), _settings(demo_mode=True))
    keys_after_first = set(session.records)
    second = await seed_demo_fixtures(cast(Any, session), _settings(demo_mode=True))

    assert first.seeded and second.seeded
    assert first.item_count == first.expected_item_count == 12
    assert first.revision_count == first.expected_revision_count == 14
    assert set(session.records) == keys_after_first
    assert session.records[(SourceItem, non_demo_id)] is non_demo

    reset = await seed_demo_fixtures(cast(Any, session), _settings(demo_mode=True), reset=True)
    assert reset.seeded
    assert set(session.records) == keys_after_first
    assert session.records[(SourceItem, non_demo_id)] is non_demo


def test_dataset_uses_reserved_namespace_and_explicit_markers() -> None:
    first = build_demo_dataset()
    second = build_demo_dataset()

    assert first == second
    assert {
        revision.normalized_metadata["_demo"]["case"]
        for item in first
        for revision in item.revisions
    } <= set(DEMO_CASES)
    assert all(item.external_id.startswith(DEMO_EXTERNAL_ID_PREFIX) for item in first)
    assert all(
        revision.normalized_metadata["_demo"]["marker"] == DEMO_MARKER
        and revision.normalized_metadata["_demo"]["synthetic"] is True
        and revision.normalized_metadata["representation"] == "fixture_observation"
        for item in first
        for revision in item.revisions
    )
    assert all(
        "example.test" in str(revision.normalized_metadata)
        or revision.normalized_metadata["_demo"]["case"]
        not in {"thread_ricevuta_e_inviata", "inoltro", "messaggi_identici_distinti"}
        for item in first
        for revision in item.revisions
    )


def test_demo_mode_does_not_bypass_authentication_or_csrf() -> None:
    settings = _settings(demo_mode=True)
    app = FastAPI()
    app.state.settings = settings
    app.include_router(api_router)

    async def empty_session() -> AsyncIterator[None]:
        yield None

    app.dependency_overrides[get_db_session] = empty_session
    with TestClient(app) as client:
        assert client.get("/api/demo/status").status_code == 401

        owner_id = uuid.uuid4()
        issued = _new_session(owner_id, settings)
        principal = Principal(
            owner=OwnerAccount(
                id=owner_id,
                owner_slot=1,
                username="owner-demo",
                display_name="Proprietario Demo Fittizio",
                password_hash="not-used-in-test",  # noqa: S106 - modello in-memory non autenticato
            ),
            session=_session_model(owner_id, issued, settings),
        )

        async def authenticated_principal() -> Principal:
            return principal

        app.dependency_overrides[require_principal] = authenticated_principal
        assert client.post("/api/auth/logout").status_code == 403
        client.cookies.set("gbrain_csrf", issued.csrf_token)
        assert (
            client.post(
                "/api/auth/logout",
                headers={"X-CSRF-Token": "header-diverso-dal-cookie"},
            ).status_code
            == 403
        )
