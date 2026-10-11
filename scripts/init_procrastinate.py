from __future__ import annotations

import asyncio

from gbrain.worker import procrastinate_app


async def main() -> None:
    await procrastinate_app.schema_manager.apply_schema_async()


if __name__ == "__main__":
    asyncio.run(main())
