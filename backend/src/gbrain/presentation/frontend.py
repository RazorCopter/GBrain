from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

from gbrain.application.auth import authenticate_session

frontend_router = APIRouter()
_PUBLIC_FRONTEND_PATHS = frozenset({"/", "/login", "/bootstrap", "/sources"})


def _frontend_file(frontend_dir: Path, requested_path: str) -> Path | None:
    normalized = requested_path.strip("/")
    if normalized.startswith("assets/"):
        candidate = (frontend_dir / normalized).resolve()
        assets_root = (frontend_dir / "assets").resolve()
        if candidate.is_relative_to(assets_root) and candidate.is_file():
            return candidate
    return None


@frontend_router.get("/{full_path:path}", include_in_schema=False)
async def frontend(full_path: str, request: Request) -> Response:
    settings = request.app.state.settings
    frontend_dir: Path = settings.frontend_dir.resolve()
    path = "/" + full_path

    if path.startswith("/api/") or path.startswith("/health/"):
        raise HTTPException(status_code=404, detail="Risorsa non trovata")

    asset = _frontend_file(frontend_dir, path)
    if asset is not None:
        return FileResponse(asset, headers={"Cache-Control": "public, max-age=31536000, immutable"})

    if path not in _PUBLIC_FRONTEND_PATHS:
        async with request.app.state.database.session_factory() as session:
            authenticated = await authenticate_session(
                session,
                settings,
                request.cookies.get(settings.session_cookie_name),
            )
        if authenticated is None:
            raise HTTPException(status_code=401, detail="Autenticazione richiesta")

    index = frontend_dir / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=503, detail="Frontend non compilato")
    return FileResponse(index, headers={"Cache-Control": "no-store"})
