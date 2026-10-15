from __future__ import annotations

import ipaddress
import logging
import socket
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from gbrain.domain.immich import (
    IMMICH_API_BATCH_SIZE,
    ImmichAlbumInfo,
    ImmichCapabilities,
    ImmichErrorCategory,
    ImmichGatewayError,
    ImmichNormalizedAsset,
    normalize_immich_asset,
)

logger = logging.getLogger(__name__)


def _sanitized_transport_detail(exc: httpx.RequestError) -> str:
    """Restituisce solo informazioni tecniche non sensibili sulla catena di errore."""
    cause = exc.__cause__
    if cause is None:
        return "cause=unavailable"
    return f"cause={type(cause).__name__}"


# Contratti verificati sullo spec e sul codice del tag immich-app/immich v3.3.3.
# Le sole versioni dichiarate sono la precedente famiglia testata e l'esatta 3.3.3:
# non si presume compatibilità con versioni intermedie o successive.
SUPPORTED_LEGACY_MIN_VERSION = (1, 106, 0)
SUPPORTED_LEGACY_MAX_VERSION = (1, 140, 0)
SUPPORTED_V3_VERSION = (3, 3, 3)


def _is_supported_version(version: tuple[int, int, int]) -> bool:
    """Verifica se la versione corrisponde a uno dei contratti formalmente verificati."""
    if SUPPORTED_LEGACY_MIN_VERSION <= version <= SUPPORTED_LEGACY_MAX_VERSION:
        return True
    return version == SUPPORTED_V3_VERSION


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
        self.last_invalid_count: int = 0

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
            try:
                return resp.json()
            except (ValueError, UnicodeDecodeError) as exc:
                logger.warning(
                    "Risposta JSON Immich non valida method=%s path=%s exception=%s",
                    method.upper(),
                    path.split("?", 1)[0],
                    type(exc).__name__,
                )
                raise ImmichGatewayError(
                    ImmichErrorCategory.PARSE,
                    "invalid_json_response",
                    "Risposta JSON Immich non valida",
                    status_code=resp.status_code,
                    phase="decode_response",
                ) from exc
        except httpx.TimeoutException as exc:
            logger.warning(
                "Trasporto Immich fallito method=%s path=%s exception=%s detail=%s",
                method.upper(),
                path.split("?", 1)[0],
                type(exc).__name__,
                _sanitized_transport_detail(exc),
            )
            raise ImmichGatewayError(
                ImmichErrorCategory.TRANSIENT,
                "network_timeout",
                "Timeout durante invio o lettura della risposta Immich",
                retryable=True,
                phase="transport",
            ) from exc
        except httpx.RequestError as exc:
            logger.warning(
                "Trasporto Immich fallito method=%s path=%s exception=%s detail=%s",
                method.upper(),
                path.split("?", 1)[0],
                type(exc).__name__,
                _sanitized_transport_detail(exc),
            )
            raise ImmichGatewayError(
                ImmichErrorCategory.TRANSIENT,
                "network_error",
                "Errore durante invio o lettura della risposta Immich",
                retryable=True,
                phase="transport",
            ) from exc
        finally:
            if should_close:
                await client.aclose()

    async def get_server_version(self) -> str:
        """Legge e valida la versione tramite il contratto API Immich supportato."""
        try:
            data = await self._handle_request("GET", "/api/server/version")
        except ImmichGatewayError as exc:
            if exc.status_code != 404:
                raise
            # Compatibilità circoscritta alle versioni legacy nell'intervallo supportato.
            # Il fallback non viene mai tentato per auth, rete, timeout o payload errati.
            data = await self._handle_request("GET", "/api/server-info/version")

        if not isinstance(data, dict):
            raise ImmichGatewayError(
                ImmichErrorCategory.PARSE,
                "server_version_malformed",
                "Risposta versione Immich non conforme al contratto atteso",
            )

        major_raw = data.get("major")
        minor_raw = data.get("minor")
        patch_raw = data.get("patch")
        if (
            type(major_raw) is not int
            or major_raw < 0
            or type(minor_raw) is not int
            or minor_raw < 0
            or type(patch_raw) is not int
            or patch_raw < 0
        ):
            raise ImmichGatewayError(
                ImmichErrorCategory.PARSE,
                "server_version_malformed",
                "Risposta versione Immich non conforme al contratto atteso",
            )

        major = major_raw
        minor = minor_raw
        patch = patch_raw
        version_tuple: tuple[int, int, int] = (major, minor, patch)
        version = f"{major}.{minor}.{patch}"
        if not _is_supported_version(version_tuple):
            raise ImmichGatewayError(
                ImmichErrorCategory.UNSUPPORTED_VERSION,
                "unsupported_server_version",
                "Versione Immich non compresa nell'intervallo verificato",
                phase="version_validation",
                server_version=version,
            )
        return version

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

    async def inspect_capabilities(
        self, *, server_version: str | None = None
    ) -> ImmichCapabilities:
        """Valida la versione e prova i soli endpoint read-only richiesti dal percorso."""
        version = server_version or await self.get_server_version()
        parsed_version = self._parse_version(version)
        if not _is_supported_version(parsed_version):
            raise ImmichGatewayError(
                ImmichErrorCategory.UNSUPPORTED_VERSION,
                "unsupported_server_version",
                f"Versione Immich {version} non inclusa nei contratti verificati",
                phase="version_validation",
                server_version=version,
            )

        try:
            supports_albums = await self._probe_capability("GET", "/api/albums")
            search_probe = (
                {
                    "size": 1,
                    "filter": {"takenAt": {"gte": "1970-01-01T00:00:00Z"}},
                    "orderBy": {"field": "localDateTime", "direction": "desc"},
                }
                if parsed_version == SUPPORTED_V3_VERSION
                else {"page": 1, "size": 1}
            )
            supports_search = await self._probe_capability(
                "POST", "/api/search/metadata", json=search_probe
            )
            supports_people = await self._probe_people_capability(parsed_version)
        except ImmichGatewayError as exc:
            if exc.phase is None:
                exc.phase = "capability_probe"
            if exc.server_version is None:
                exc.server_version = version
            raise
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
        limit: int | None = None,
    ) -> tuple[ImmichAlbumInfo, list[ImmichNormalizedAsset]]:
        """Recupera i metadati inline di un album; il job li suddivide in batch locali."""
        data = await self._handle_request(
            "GET", f"/api/albums/{album_id}", params={"withoutAssets": "false"}
        )
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

        selected_assets = raw_assets if limit is None else raw_assets[: min(max(limit, 1), 25)]
        normalized: list[ImmichNormalizedAsset] = []
        invalid_count = 0
        for asset in selected_assets:
            if not isinstance(asset, dict):
                invalid_count += 1
                continue
            try:
                normalized.append(self.normalize_asset(asset, albums=(album_info,)))
            except (ValueError, ImmichGatewayError) as exc:
                logger.warning("Scartato asset Immich non valido nell'album %s: %s", album_id, exc)
                invalid_count += 1
        self.last_invalid_count = invalid_count
        return album_info, normalized

    async def search_period_assets(
        self,
        after: datetime | None,
        before: datetime | None,
        limit: int = IMMICH_API_BATCH_SIZE,
        *,
        cursor: str | None = None,
    ) -> tuple[list[ImmichNormalizedAsset], str | None]:
        """Cerca una pagina di asset, con request remota sempre limitata a 25 elementi."""
        bounded_limit = min(max(limit, 1), IMMICH_API_BATCH_SIZE)
        version = await self.get_server_version()
        parsed_version = self._parse_version(version)

        payload: dict[str, Any]
        if parsed_version == SUPPORTED_V3_VERSION:
            taken_at: dict[str, str] = {}
            if after is not None:
                taken_at["gte"] = after.astimezone(UTC).isoformat()
            if before is not None:
                taken_at["lt"] = before.astimezone(UTC).isoformat()
            payload = {
                "size": bounded_limit,
                "withExif": True,
                "withPeople": True,
                "filter": {"takenAt": taken_at} if taken_at else {},
                "orderBy": {"field": "localDateTime", "direction": "desc"},
            }
            if cursor is not None:
                payload["cursor"] = cursor
        else:
            legacy_page = 1
            if cursor is not None:
                try:
                    legacy_page = int(cursor)
                except ValueError as exc:
                    raise ImmichGatewayError(
                        ImmichErrorCategory.PARSE,
                        "legacy_cursor_invalid",
                        "Checkpoint di paginazione legacy Immich non valido",
                    ) from exc
            payload = {
                "page": legacy_page,
                "size": bounded_limit,
                "withExif": True,
                "withPeople": True,
            }
            if after is not None:
                payload["takenAfter"] = after.astimezone(UTC).isoformat()
            if before is not None:
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

        if not isinstance(data, dict):
            raise ImmichGatewayError(
                ImmichErrorCategory.PARSE,
                "metadata_search_invalid",
                "Risposta ricerca Immich non valida",
            )
        assets_container = data.get("assets")
        if not isinstance(assets_container, dict):
            raise ImmichGatewayError(
                ImmichErrorCategory.PARSE,
                "metadata_search_invalid",
                "Contenitore assets Immich non valido",
            )
        items = assets_container.get("items")
        if not isinstance(items, list):
            raise ImmichGatewayError(
                ImmichErrorCategory.PARSE,
                "metadata_search_invalid",
                "Elenco assets Immich non valido",
            )

        bounded = items[:bounded_limit]
        normalized: list[ImmichNormalizedAsset] = []
        invalid_count = 0
        for a in bounded:
            if not isinstance(a, dict):
                invalid_count += 1
                continue
            try:
                normalized.append(self.normalize_asset(a))
            except (ValueError, ImmichGatewayError) as exc:
                logger.warning("Scartato asset Immich non valido durante la search: %s", exc)
                invalid_count += 1
        self.last_invalid_count = invalid_count
        if parsed_version == SUPPORTED_V3_VERSION:
            raw_next_cursor = assets_container.get("nextCursor")
            if raw_next_cursor is not None and not isinstance(raw_next_cursor, str):
                raise ImmichGatewayError(
                    ImmichErrorCategory.PARSE,
                    "metadata_cursor_invalid",
                    "Cursor Immich v3 non valido",
                )
            next_cursor = raw_next_cursor or None
        else:
            # Il ramo legacy usa pagine numeriche; una pagina corta conclude l'enumerazione.
            next_cursor = str(int(payload["page"]) + 1) if len(items) >= bounded_limit else None
        return normalized, next_cursor
