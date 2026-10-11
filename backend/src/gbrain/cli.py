from __future__ import annotations

import argparse
import asyncio

import uvicorn

from gbrain.application.auth import administrative_reset_bootstrap
from gbrain.application.demo import DemoFixturesError, DemoModeDisabledError, seed_demo_fixtures
from gbrain.config import get_settings
from gbrain.infrastructure.database import Database


async def _reset_owner(confirmation: str) -> None:
    database = Database(get_settings())
    try:
        async with database.session_factory() as session:
            await administrative_reset_bootstrap(session, confirmation=confirmation)
    finally:
        await database.close()


async def _seed_demo(reset: bool) -> None:
    settings = get_settings()
    if not settings.demo_mode:
        raise DemoModeDisabledError(
            "Fixture demo rifiutate: impostare esplicitamente GBRAIN_DEMO_MODE=true"
        )
    database = Database(settings)
    try:
        async with database.session_factory() as session:
            result = await seed_demo_fixtures(session, settings, reset=reset)
        print(
            f"Fixture demo pronte: {result.item_count} elementi, "
            f"{result.revision_count} revisioni, {result.import_job_count} import job."
        )
    finally:
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="gbrain")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("web")
    reset = subparsers.add_parser("reset-owner")
    reset.add_argument("--confirm", required=True)
    demo = subparsers.add_parser(
        "demo-fixtures",
        help="Carica fixture sintetiche solo quando GBRAIN_DEMO_MODE=true",
    )
    demo.add_argument(
        "--reset",
        action="store_true",
        help="Ripristina prima esclusivamente i record deterministici del namespace demo",
    )
    args = parser.parse_args()

    if args.command == "web":
        settings = get_settings()
        uvicorn.run(
            "gbrain.main:app",
            host=settings.web_host,
            port=settings.web_port,
            proxy_headers=True,
            forwarded_allow_ips="*",
            access_log=False,
        )
    elif args.command == "reset-owner":
        asyncio.run(_reset_owner(args.confirm))
    elif args.command == "demo-fixtures":
        try:
            asyncio.run(_seed_demo(args.reset))
        except DemoFixturesError as exc:
            parser.error(str(exc))


if __name__ == "__main__":
    main()
