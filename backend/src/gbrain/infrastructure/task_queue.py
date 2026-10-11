from __future__ import annotations

import procrastinate

from gbrain.config import Settings

GMAIL_TASK_NAME = "gbrain.gmail_sync"


def create_procrastinate_app(settings: Settings) -> procrastinate.App:
    connector = procrastinate.PsycopgConnector(conninfo=settings.procrastinate_dsn)
    return procrastinate.App(connector=connector)
