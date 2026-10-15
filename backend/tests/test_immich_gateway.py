from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from gbrain.domain.immich import (
    ImmichAssetType,
    ImmichDateSource,
    ImmichErrorCategory,
    ImmichGatewayError,
    ImmichSelection,
)
from gbrain.infrastructure.immich_gateway import (
    ImmichGateway,
    validate_immich_url,
)


def test_immich_selection_validation() -> None:
    # Il limite totale è distinto dal batch remoto da 25 e arriva fino a 50.000.
    with pytest.raises(ValueError, match="item_limit"):
        ImmichSelection(album_id="album-1", item_limit=50_001)

    # 2. Mutua esclusione album e intervallo temporale
    with pytest.raises(ValueError, match="mutuamente esclusive"):
        ImmichSelection(
            album_id="album-1",
            after=datetime(2026, 1, 1, tzinfo=UTC),
        )

    # 3. Intervallo temporale after >= before non valido
    with pytest.raises(ValueError, match="antecedente"):
        ImmichSelection(
            after=datetime(2026, 2, 1, tzinfo=UTC),
            before=datetime(2026, 1, 1, tzinfo=UTC),
        )

    # 4. Selezione valida per album
    sel_album = ImmichSelection(album_id="album-uuid-1", item_limit=25)
    assert sel_album.album_id == "album-uuid-1"
    assert sel_album.item_limit == 25

    # 5. Selezione valida per periodo
    sel_period = ImmichSelection(
        after=datetime(2026, 1, 1, tzinfo=UTC),
        before=datetime(2026, 2, 1, tzinfo=UTC),
        item_limit=10,
    )
    assert sel_period.album_id is None
    assert sel_period.item_limit == 10


def test_validate_immich_url_rules() -> None:
    # HTTPS sempre consentito su qualsiasi host
    clean_https, is_lan = validate_immich_url("https://immich.mydomain.com/api/../")
    assert clean_https.startswith("https://immich.mydomain.com")
    assert not is_lan

    # HTTP consentito su loopback
    clean_loopback, is_lan_lb = validate_immich_url("http://127.0.0.1:2283/")
    assert clean_loopback == "http://127.0.0.1:2283"
    assert is_lan_lb is True

    # HTTP consentito su IP privato LAN RFC 1918
    clean_lan, is_lan_priv = validate_immich_url("http://192.168.1.50:2283")
    assert clean_lan == "http://192.168.1.50:2283"
    assert is_lan_priv is True

    # HTTP rifiutato su IP pubblico (es. 8.8.8.8)
    with pytest.raises(ImmichGatewayError) as exc_pub:
        validate_immich_url("http://8.8.8.8:2283")
    assert exc_pub.value.reason_code == "insecure_http_rejected"

    # Userinfo incorporato nell'URL rifiutato
    with pytest.raises(ImmichGatewayError) as exc_user:
        validate_immich_url("http://admin:secret@192.168.1.50:2283")
    assert exc_user.value.reason_code == "userinfo_forbidden"

    # Query string o fragment nell'URL di base rifiutati
    with pytest.raises(ImmichGatewayError) as exc_query:
        validate_immich_url("https://photos.example.com/?token=secret")
    assert exc_query.value.reason_code == "query_fragment_forbidden"


@pytest.mark.asyncio
async def test_immich_gateway_discovery_and_auth_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Verifica che l'API key sia presente nell'header corretto
        assert request.headers.get("x-api-key") == "test-immich-api-key"
        path = request.url.path

        if path == "/api/server/version":
            return httpx.Response(200, json={"major": 1, "minor": 106, "patch": 4})
        if path == "/api/server-info/version":
            pytest.fail(
                "Il fallback legacy non deve essere chiamato se l'endpoint attuale risponde"
            )
        if path == "/api/users/me":
            return httpx.Response(
                200,
                json={"id": "user-uuid-1", "email": "gian@example.test", "name": "Gian"},
            )
        if path == "/api/albums":
            return httpx.Response(
                200,
                json=[
                    {"id": "alb-1", "albumName": "Vacanze 2026"},
                    {"id": "alb-2", "albumName": "Famiglia"},
                ],
            )
        if path == "/api/search/metadata":
            assert request.method == "POST"
            return httpx.Response(200, json={"assets": {"items": []}})
        if path == "/api/people":
            return httpx.Response(
                200,
                json={"people": [{"id": "person-1", "name": "Alice"}], "total": 1},
            )
        return httpx.Response(404)

    mock_transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=mock_transport, base_url="https://immich.test"
    ) as client:
        gw = ImmichGateway("https://immich.test", "test-immich-api-key", client=client)

        version = await gw.get_server_version()
        assert version == "1.106.4"

        user = await gw.get_current_user()
        assert user["id"] == "user-uuid-1"
        assert user["email"] == "gian@example.test"

        albums = await gw.get_albums()
        assert len(albums) == 2
        assert albums[0].name == "Vacanze 2026"

        caps = await gw.inspect_capabilities()
        assert caps.server_version == "1.106.4"
        assert caps.supports_albums is True
        assert caps.supports_people is True


@pytest.mark.asyncio
async def test_immich_gateway_version_uses_legacy_fallback_only_after_404() -> None:
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/api/server/version":
            return httpx.Response(404)
        if request.url.path == "/api/server-info/version":
            return httpx.Response(200, json={"major": 1, "minor": 106, "patch": 4})
        return httpx.Response(500)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://immich.test"
    ) as client:
        gateway = ImmichGateway("https://immich.test", "test-api-key", client=client)
        assert await gateway.get_server_version() == "1.106.4"

    assert requested_paths == ["/api/server/version", "/api/server-info/version"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected_category", "expected_reason"),
    [
        (httpx.Response(401), ImmichErrorCategory.AUTH, "invalid_api_key"),
        (
            httpx.Response(200, json={"major": 1, "minor": "106", "patch": 4}),
            ImmichErrorCategory.PARSE,
            "server_version_malformed",
        ),
    ],
)
async def test_immich_gateway_version_does_not_mask_non_404_errors(
    response: httpx.Response,
    expected_category: ImmichErrorCategory,
    expected_reason: str,
) -> None:
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        return response

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://immich.test"
    ) as client:
        gateway = ImmichGateway("https://immich.test", "test-api-key", client=client)
        with pytest.raises(ImmichGatewayError) as exc_info:
            await gateway.get_server_version()

    assert exc_info.value.category == expected_category
    assert exc_info.value.reason_code == expected_reason
    assert requested_paths == ["/api/server/version"]


@pytest.mark.asyncio
async def test_immich_gateway_supports_verified_v3_3_3() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/server/version":
            return httpx.Response(
                200, json={"major": 3, "minor": 3, "patch": 3, "prerelease": None}
            )
        if request.url.path == "/api/users/me":
            return httpx.Response(
                200, json={"id": "user-v3", "email": "v3@immich.test", "name": "V3 User"}
            )
        if request.url.path == "/api/albums":
            return httpx.Response(200, json=[{"id": "alb-v3", "albumName": "V3 Album"}])
        if request.url.path == "/api/search/metadata":
            assert request.method == "POST"
            # Verifica che il payload utilizzi lo schema Zod strutturato di v3.3.3
            body = json.loads(request.content.decode("utf-8"))
            assert "filter" in body
            assert "orderBy" in body
            assert body["orderBy"]["field"] == "localDateTime"
            return httpx.Response(
                200,
                json={
                    "assets": {
                        "count": 1,
                        "items": [
                            {
                                "id": "asset-v3-1",
                                "type": "IMAGE",
                                "fileCreatedAt": "2026-05-01T10:00:00Z",
                                "localDateTime": "2026-05-01T12:00:00",
                                "originalFileName": "IMG_v3.jpg",
                                "exifInfo": {
                                    "dateTimeOriginal": "2026-05-01T10:00:00Z",
                                    "description": "V3 Spec",
                                },
                                "people": [{"id": "p-1", "name": "Elena"}],
                            }
                        ],
                    }
                },
            )
        if request.url.path == "/api/people":
            return httpx.Response(
                200,
                json={
                    "people": [{"id": "p-1", "name": "Elena"}],
                    "total": 1,
                    "hidden": 0,
                    "hasNextPage": False,
                },
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://immich.test"
    ) as client:
        gw = ImmichGateway("https://immich.test", "test-immich-api-key", client=client)
        version = await gw.get_server_version()
        assert version == "3.3.3"

        caps = await gw.inspect_capabilities()
        assert caps.server_version == "3.3.3"
        assert caps.version_supported is True
        assert caps.supports_albums is True
        assert caps.supports_search is True
        assert caps.supports_people is True

        assets, next_cursor = await gw.search_period_assets(
            after=datetime(2026, 1, 1, tzinfo=UTC),
            before=datetime(2026, 6, 1, tzinfo=UTC),
            limit=10,
        )
        assert next_cursor is None
        assert len(assets) == 1
        assert assets[0].external_id == "asset-v3-1"
        assert len(assets[0].people) == 1
        assert assets[0].people[0].name == "Elena"
        assert assets[0].people[0].id == "p-1"


@pytest.mark.asyncio
async def test_immich_gateway_rejects_unsupported_version() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/server/version"
        # Release 3.3.4 non verificata o 2.0.0 devono essere esplicitamente rifiutate
        return httpx.Response(200, json={"major": 3, "minor": 3, "patch": 4})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://immich.test"
    ) as client:
        gateway = ImmichGateway("https://immich.test", "test-api-key", client=client)
        with pytest.raises(ImmichGatewayError) as exc_info:
            await gateway.get_server_version()

    assert exc_info.value.category == ImmichErrorCategory.UNSUPPORTED_VERSION
    assert exc_info.value.reason_code == "unsupported_server_version"
    assert exc_info.value.phase == "version_validation"
    assert exc_info.value.server_version == "3.3.4"


@pytest.mark.asyncio
async def test_immich_gateway_transport_error_logs_sanitized_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "api-key-must-not-leak"  # noqa: S105 - synthetic PII sentinel

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"connection failed with {secret}",
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://immich.test"
    ) as client:
        gateway = ImmichGateway("https://immich.test", secret, client=client)
        with caplog.at_level("WARNING"):
            with pytest.raises(ImmichGatewayError) as exc_info:
                await gateway.get_current_user()

    assert exc_info.value.category == ImmichErrorCategory.TRANSIENT
    assert exc_info.value.reason_code == "network_error"
    assert exc_info.value.phase == "transport"
    assert "method=GET" in caplog.text
    assert "path=/api/users/me" in caplog.text
    assert "exception=ConnectError" in caplog.text
    assert secret not in caplog.text
    assert "connection failed" not in caplog.text


@pytest.mark.asyncio
async def test_immich_gateway_invalid_json_is_parse_not_network() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/server/version"
        return httpx.Response(200, content=b"not-json")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://immich.test"
    ) as client:
        gateway = ImmichGateway("https://immich.test", "test-key", client=client)
        with pytest.raises(ImmichGatewayError) as exc_info:
            await gateway.get_server_version()

    assert exc_info.value.category == ImmichErrorCategory.PARSE
    assert exc_info.value.reason_code == "invalid_json_response"
    assert exc_info.value.phase == "decode_response"


@pytest.mark.asyncio
async def test_immich_capability_failure_preserves_validated_version() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/albums":
            raise httpx.ReadError("read failed", request=request)
        return httpx.Response(500)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://immich.test"
    ) as client:
        gateway = ImmichGateway("https://immich.test", "test-key", client=client)
        with pytest.raises(ImmichGatewayError) as exc_info:
            await gateway.inspect_capabilities(server_version="1.106.4")

    assert exc_info.value.category == ImmichErrorCategory.TRANSIENT
    assert exc_info.value.reason_code == "network_error"
    assert exc_info.value.phase == "transport"
    assert exc_info.value.server_version == "1.106.4"


@pytest.mark.asyncio
async def test_immich_gateway_error_mapping() -> None:
    # 1. Autenticazione non valida (401)
    def auth_err_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Unauthorized"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(auth_err_handler), base_url="https://immich.test"
    ) as client:
        gw = ImmichGateway("https://immich.test", "bad-key", client=client)
        with pytest.raises(ImmichGatewayError) as exc_auth:
            await gw.get_current_user()
        assert exc_auth.value.category == ImmichErrorCategory.AUTH
        assert exc_auth.value.status_code == 401

    # 2. Rate limit 429 (retryable)
    def rate_limit_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"message": "Too Many Requests"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(rate_limit_handler), base_url="https://immich.test"
    ) as client:
        gw = ImmichGateway("https://immich.test", "key", client=client)
        with pytest.raises(ImmichGatewayError) as exc_rl:
            await gw.get_albums()
        assert exc_rl.value.category == ImmichErrorCategory.TRANSIENT
        assert exc_rl.value.retryable is True


def test_immich_asset_normalization_and_provenance() -> None:
    gw = ImmichGateway("https://immich.test", "fake-key")

    raw_asset = {
        "id": "asset-uuid-1234",
        "type": "IMAGE",
        "dateTimeOriginal": "2026-06-15T14:30:00.000Z",
        "fileCreatedAt": "2026-06-15T15:00:00.000Z",
        "originalFileName": "IMG_20260615.jpg",
        "exifInfo": {
            "description": "Tramonto sul mare",
            "model": "Pixel 8 Pro",
        },
        "people": [
            {"id": "p-1", "name": "Mario Rossi"},
            {"id": "p-2", "name": ""},  # Nome vuoto -> deve essere escluso
        ],
    }

    normalized = gw.normalize_asset(raw_asset)

    assert normalized.external_id == "asset-uuid-1234"
    assert normalized.occurred_at == datetime(2026, 6, 15, 14, 30, tzinfo=UTC)
    assert normalized.date_source == ImmichDateSource.DATE_TIME_ORIGINAL
    assert normalized.asset_type == ImmichAssetType.IMAGE
    assert normalized.description == "Tramonto sul mare"
    # Solo persone etichettate con nome
    assert len(normalized.people) == 1
    assert normalized.people[0].name == "Mario Rossi"
    # Hash canonico calcolato
    assert len(normalized.canonical_hash) == 64
    assert "original_filename" not in normalized.provenance_payload
    assert normalized.provenance_payload["selected_date_source"] == "dateTimeOriginal"
    assert normalized.provenance_payload["metadata_only"] is True


@pytest.mark.asyncio
async def test_immich_fetch_album_bounded_to_limit() -> None:
    # Genera 30 asset fittizi
    sample_assets = [
        {
            "id": f"asset-{i}",
            "type": "IMAGE",
            "fileCreatedAt": "2026-05-01T10:00:00.000Z",
        }
        for i in range(30)
    ]

    def album_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "alb-1",
                "albumName": "Test Album",
                "assets": sample_assets,
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(album_handler), base_url="https://immich.test"
    ) as client:
        gw = ImmichGateway("https://immich.test", "key", client=client)
        album_info, assets = await gw.fetch_album_assets("alb-1", limit=25)

        assert album_info.name == "Test Album"
        # Deve essere tassativamente limitato a max 25
        assert len(assets) == 25
        assert assets[0].external_id == "asset-0"
        assert assets[24].external_id == "asset-24"
