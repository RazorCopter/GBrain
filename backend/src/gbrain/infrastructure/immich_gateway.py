from __future__ import annotations

import ipaddress
import logging
import socket
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from gbrain.domain.immich import (
    IMMICH_MAX_ITEMS_LIMIT,
    ImmichAlbumInfo,
    ImmichCapabilities,
    ImmichErrorCategory,
    ImmichGatewayError,
    ImmichNormalizedAsset,
    normalize_immich_asset,
)

logger = logging.getLogger(__name__)

# Contratto API verificato per la famiglia v1 che espone /api/server-info/version,
# /api/users/me, /api/albums, /api/search/metadata e /api/people.
SUPPORTED_MIN_VERSION = (1, 106, 0)
SUPPORTED_MAX_TESTED_VERSION = (1, 140, 0)


def is_ip_private_or_loopback(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
        return (
            ip.is_private
            or ip.is_loopback
            or (ip.version == 4 and ip_str.startswith("100."))  # CGNAT 100.64.0.0/10
        )
    except ValueError:
        return False


def validate_immich_url(url: str) -> tuple[str, bool]:
    """Valida e normalizza l'URL dell'istanza Immich.

    Regole di sicurezza approvate:
    - HTTPS è sempre consentito su qualsiasi host.
    - HTTP è consentito SOLO se l'host risolve esclusivamente su IP privati o loopback (LAN).
    - Nessun userinfo, query o fragment consentito nell'URL di base.
    """
    clean = url.strip().rstrip("/")
    if not clean:
        raise ImmichGatewayError(
            ImmichErrorCategory.AUTH,
            "url_empty",
            "L'URL dell'istanza Immich non può essere vuoto",
        )

    parsed = urlparse(clean)
    if parsed.scheme not in ("http", "https"):
        raise ImmichGatewayError(
            ImmichErrorCategory.AUTH,
            "invalid_scheme",
            "Lo schema dell'URL Immich deve essere http o https",
        )

    if parsed.username or parsed.password:
        raise ImmichGatewayError(
            ImmichErrorCategory.AUTH,
            "userinfo_forbidden",
            "Credenziali incorporate nell'URL non consentite per ragioni di sicurezza",
        )

    if parsed.query or parsed.fragment:
        raise ImmichGatewayError(
            ImmichErrorCategory.AUTH,
            "query_fragment_forbidden",
            "Query string o fragment non consentiti nell'URL di base dell'istanza",
        )

    hostname = parsed.hostname
    if not hostname:
        raise ImmichGatewayError(
            ImmichErrorCategory.AUTH,
            "missing_hostname",
            "Hostname mancante nell'URL Immich specificato",
        )

    # Validazione di sicurezza per schema HTTP
    is_lan_http = False
    if parsed.scheme == "http":
        try:
            addr_info = socket.getaddrinfo(hostname, None)
            resolved_ips = {str(item[4][0]) for item in addr_info}
            if not resolved_ips or not all(is_ip_private_or_loopback(ip) for ip in resolved_ips):
                raise ImmichGatewayError(
                    ImmichErrorCategory.AUTH,
                    "insecure_http_rejected",
                    "HTTP non crittografato non è consentito su host pubblici o risolvibili su Internet",
                )
            is_lan_http = True
        except socket.gaierror as exc:
            raise ImmichGatewayError(
                ImmichErrorCategory.AUTH,
                "dns_resolution_failed",
                f"Risoluzione DNS fallita per l'host '{hostname}'",
            ) from exc

    return clean, is_lan_http


class ImmichGateway:
    """Gateway ufficiale HTTP client per API REST Immich.

    Tutte le chiamate sono strettamente read-only. Nessun upload, mutazione o download
    degli originali viene eseguito.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url, self.is_lan_http = validate_immich_url(base_url)
        clean_key = api_key.strip()
        if not clean_key:
            raise ImmichGatewayError(
                ImmichErrorCategory.AUTH,
                "api_key_empty",
                "L'API key Immich non può essere vuota",
            )
        self._api_key = clean_key
        self._custom_client = client

    def _client(self) -> httpx.AsyncClient:
        if self._custom_client is not None:
            return self._custom_client
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "x-api-key": self._api_key,
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(15.0, connect=5.0, read=15.0, write=10.0, pool=5.0),
            follow_redirects=False,
        )

    async def _handle_request(self, method: str, path: str, **kwargs: Any) -> Any:
        client = self._client()
        should_close = self._custom_client is None
        req_headers = dict(kwargs.pop("headers", {}) or {})
        req_headers.setdefault("x-api-key", self._api_key)
        req_headers.setdefault("Accept", "application/json")
        try:
            resp = await client.request(method, path, headers=req_headers, **kwargs)
            if resp.status_code in (401, 403):
                raise ImmichGatewayError(
                    ImmichErrorCategory.AUTH,
                    "invalid_api_key",
                    "API key Immich non valida o priva dei permessi necessari",
                    status_code=resp.status_code,
                )
            if resp.status_code == 404:
                raise ImmichGatewayError(
                    ImmichErrorCategory.MISSING_CAPABILITY,
                    "endpoint_not_found",
                    f"Endpoint Immich non trovato: {path}",
                    status_code=404,
                )
            if resp.status_code == 429:
                raise ImmichGatewayError(
                    ImmichErrorCategory.TRANSIENT,
                    "rate_limited",
                    "Rate limit superato sull'istanza Immich",
                    status_code=429,
                    retryable=True,
                )
            if resp.status_code >= 500:
                raise ImmichGatewayError(
                    ImmichErrorCategory.TRANSIENT,
                    "server_error",
                    f"Errore interno del server Immich ({resp.status_code})",
                    status_code=resp.status_code,
                    retryable=True,
                )
            resp.raise_for_status()
            return resp.json()
        except httpx.TimeoutException as exc:
            raise ImmichGatewayError(
                ImmichErrorCategory.TRANSIENT,
                "network_timeout",
                "Timeout di connessione o lettura verso l'istanza Immich",
                retryable=True,
            ) from exc
        except httpx.RequestError as exc:
            raise ImmichGatewayError(
                ImmichErrorCategory.TRANSIENT,
                "network_error",
                "Errore di rete durante la comunicazione con l'istanza Immich",
                retryable=True,
            ) from exc
        finally:
            if should_close:
                await client.aclose()

    async def get_server_version(self) -> str:
        """Legge la versione tramite il contratto ufficiale della famiglia API supportata."""
        data = await self._handle_request("GET", "/api/server-info/version")
        if isinstance(data, dict):
            major = data.get("major")
            minor = data.get("minor")
            patch = data.get("patch")
            if isinstance(major, int) and isinstance(minor, int):
                return f"{major}.{minor}.{patch if isinstance(patch, int) else 0}"
        raise ImmichGatewayError(
            ImmichErrorCategory.PARSE,
            "server_version_malformed",
            "Risposta versione Immich non conforme al contratto atteso",
        )

    async def get_current_user(self) -> dict[str, Any]:
        """Verifica l'autenticazione leggendo il profilo dell'utente corrispondente all'API key."""
        data = await self._handle_request("GET", "/api/users/me")
        if not isinstance(data, dict) or "id" not in data:
            raise ImmichGatewayError(
                ImmichErrorCategory.PARSE,
                "user_me_malformed",
                "Risposta del profilo utente Immich non conforme",
            )
        return {
            "id": str(data["id"]),
            "email": str(data.get("email") or ""),
            "name": str(data.get("name") or data.get("email") or "Utente Immich"),
        }

    async def get_albums(self) -> list[ImmichAlbumInfo]:
        """Enumera gli album accessibili all'utente autenticato."""
        data = await self._handle_request("GET", "/api/albums")
        if not isinstance(data, list):
            return []
        albums: list[ImmichAlbumInfo] = []
        for item in data:
            if isinstance(item, dict) and "id" in item and "albumName" in item:
                albums.append(ImmichAlbumInfo(id=str(item["id"]), name=str(item["albumName"])))
        return albums

    @staticmethod
    def _parse_version(version: str) -> tuple[int, int, int]:
        try:
            parts = tuple(int(part) for part in version.split(".")[:3])
        except ValueError as exc:
            raise ImmichGatewayError(
                ImmichErrorCategory.PARSE,
                "server_version_malformed",
                "Versione Immich non interpretabile",
            ) from exc
        if len(parts) != 3:
            raise ImmichGatewayError(
                ImmichErrorCategory.PARSE,
                "server_version_malformed",
                "Versione Immich non interpretabile",
            )
        return parts

    async def inspect_capabilities(self) -> ImmichCapabilities:
        """Valida la versione e prova i soli endpoint read-only richiesti dal percorso."""
        version = await self.get_server_version()
        parsed_version = self._parse_version(version)
        if parsed_version < SUPPORTED_MIN_VERSION or parsed_version > SUPPORTED_MAX_TESTED_VERSION:
            raise ImmichGatewayError(
                ImmichErrorCategory.UNSUPPORTED_VERSION,
                "unsupported_server_version",
                f"Versione Immich {version} fuori dall'intervallo supportato "
                f"{'.'.join(map(str, SUPPORTED_MIN_VERSION))}-"
                f"{'.'.join(map(str, SUPPORTED_MAX_TESTED_VERSION))}",
            )

        supports_albums = await self._probe_capability("GET", "/api/albums")
        supports_search = await self._probe_capability(
            "POST", "/api/search/metadata", json={"page": 1, "size": 1}
        )
        supports_people = await self._probe_people_capability(parsed_version)
        return ImmichCapabilities(
            server_version=version,
            version_supported=True,
            supports_albums=supports_albums,
            supports_people=supports_people,
            supports_search=supports_search,
        )

    async def _probe_capability(self, method: str, path: str, **kwargs: Any) -> bool:
        try:
            await self._handle_request(method, path, **kwargs)
        except ImmichGatewayError as exc:
            if exc.category == ImmichErrorCategory.MISSING_CAPABILITY:
                return False
            raise
        return True

    async def _probe_people_capability(self, version: tuple[int, int, int]) -> bool:
        try:
            data = await self._handle_request("GET", "/api/people")
        except ImmichGatewayError as exc:
            if exc.category == ImmichErrorCategory.MISSING_CAPABILITY:
                return False
            raise
        if version >= (1, 106, 0):
            if not isinstance(data, dict) or not isinstance(data.get("people"), list):
                raise ImmichGatewayError(
                    ImmichErrorCategory.PARSE,
                    "people_wrapper_malformed",
                    "Risposta persone Immich non conforme al wrapper versionato",
                )
            return True
        if not isinstance(data, list):
            raise ImmichGatewayError(
                ImmichErrorCategory.PARSE,
                "people_list_malformed",
                "Risposta persone Immich non conforme alla lista versionata",
            )
        return True

    def normalize_asset(
        self,
        asset: dict[str, Any],
        *,
        albums: tuple[ImmichAlbumInfo, ...] = (),
    ) -> ImmichNormalizedAsset:
        """Compatibilità sottile: la normalizzazione pura appartiene al dominio."""
        try:
            return normalize_immich_asset(asset, albums=albums)
        except ValueError as exc:
            raise ImmichGatewayError(
                ImmichErrorCategory.PERMANENT,
                "asset_malformed",
                str(exc),
            ) from exc

    async def fetch_album_assets(
        self,
        album_id: str,
        limit: int = IMMICH_MAX_ITEMS_LIMIT,
    ) -> tuple[ImmichAlbumInfo, list[ImmichNormalizedAsset]]:
        """Recupera e normalizza i metadati degli asset di un determinato album (max 25)."""
        data = await self._handle_request("GET", f"/api/albums/{album_id}")
        if not isinstance(data, dict):
            raise ImmichGatewayError(
                ImmichErrorCategory.PARSE,
                "album_data_invalid",
                f"Risposta album {album_id} non valida",
            )
        album_info = ImmichAlbumInfo(
            id=str(data.get("id", album_id)), name=str(data.get("albumName", "Album"))
        )
        raw_assets = data.get("assets") or []
        if not isinstance(raw_assets, list):
            raw_assets = []

        bounded = raw_assets[: min(max(limit, 1), IMMICH_MAX_ITEMS_LIMIT)]
        normalized = [self.normalize_asset(a, albums=(album_info,)) for a in bounded]
        return album_info, normalized

    async def search_period_assets(
        self,
        after: datetime | None,
        before: datetime | None,
        limit: int = IMMICH_MAX_ITEMS_LIMIT,
    ) -> list[ImmichNormalizedAsset]:
        """Cerca asset in base all'intervallo temporale specificato (max 25)."""
        bounded_limit = min(max(limit, 1), IMMICH_MAX_ITEMS_LIMIT)
        payload: dict[str, Any] = {"page": 1, "size": bounded_limit}
        if after:
            payload["takenAfter"] = after.astimezone(UTC).isoformat()
        if before:
            payload["takenBefore"] = before.astimezone(UTC).isoformat()

        try:
            data = await self._handle_request("POST", "/api/search/metadata", json=payload)
        except ImmichGatewayError as err:
            if err.category == ImmichErrorCategory.MISSING_CAPABILITY:
                # Se l'endpoint di ricerca non è disponibile, segnala l'incompatibilità
                raise ImmichGatewayError(
                    ImmichErrorCategory.MISSING_CAPABILITY,
                    "metadata_search_unavailable",
                    "L'istanza Immich non supporta la ricerca metadati per periodo",
                ) from err
            raise

        assets_container = data.get("assets", {}) if isinstance(data, dict) else {}
        items = assets_container.get("items") or [] if isinstance(assets_container, dict) else []
        if not isinstance(items, list):
            items = []

        bounded = items[:bounded_limit]
        return [self.normalize_asset(a) for a in bounded if isinstance(a, dict)]
