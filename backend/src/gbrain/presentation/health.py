from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request, Response, status

health_router = APIRouter()


@health_router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@health_router.get("/health/ready")
async def ready(request: Request, response: Response) -> dict[str, object]:
    postgres_status = "ok"
    neo4j_status = "ok"
    try:
        await asyncio.wait_for(request.app.state.database.ping(), timeout=3.0)
    except Exception:
        postgres_status = "unavailable"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    try:
        await asyncio.wait_for(request.app.state.graph.ping(), timeout=3.0)
    except Exception:
        neo4j_status = "unavailable"

    return {
        "status": "ok" if postgres_status == "ok" else "unavailable",
        "postgres": postgres_status,
        "neo4j_projection": neo4j_status,
        "graphiti": "not_implemented",
    }
