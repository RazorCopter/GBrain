from __future__ import annotations

from neo4j import AsyncDriver, AsyncGraphDatabase

from gbrain.config import Settings


class GraphProjection:
    """Probe della proiezione Neo4j; non implementa né dichiara Graphiti."""

    def __init__(self, settings: Settings) -> None:
        self._database = settings.neo4j_database
        self._driver: AsyncDriver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_secret),
            connection_timeout=3.0,
        )

    async def ping(self) -> None:
        await self._driver.verify_connectivity()

    async def close(self) -> None:
        await self._driver.close()
