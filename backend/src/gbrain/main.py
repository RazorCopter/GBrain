from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware

from gbrain.config import get_settings
from gbrain.infrastructure.database import Database
from gbrain.infrastructure.graph import GraphProjection
from gbrain.infrastructure.task_queue import create_procrastinate_app
from gbrain.logging import configure_logging
from gbrain.presentation.api import api_router
from gbrain.presentation.frontend import frontend_router
from gbrain.presentation.gmail_api import gmail_router
from gbrain.presentation.health import health_router


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    settings = get_settings()
    for directory in (
        settings.data_dir,
        settings.cache_dir,
        settings.import_dir,
        settings.backup_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    app.state.settings = settings
    app.state.database = Database(settings)
    app.state.graph = GraphProjection(settings)
    app.state.procrastinate = create_procrastinate_app(settings)
    try:
        async with app.state.procrastinate.open_async():
            yield
    finally:
        await app.state.graph.close()
        await app.state.database.close()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    app = FastAPI(
        title="GBrain API",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.include_router(health_router)
    app.include_router(api_router)
    app.include_router(gmail_router)
    app.include_router(frontend_router)
    return app


app = create_app()
