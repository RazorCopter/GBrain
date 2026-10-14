from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class ProcrastinateJobSnapshot:
    """Istantanea coerente dello stato di un job Procrastinate e del clock DB."""

    job_id: int
    queue_name: str
    task_name: str
    lock: str | None
    status: str
    scheduled_at: datetime | None
    attempts: int
    abort_requested: bool
    worker_id: int | None
    database_now: datetime
    blocked_by_lock: bool
    has_retry_event: bool = False
    args: dict[str, Any] = field(default_factory=dict)


_BASE_SNAPSHOT_SELECT = """
    SELECT
        jobs.id,
        jobs.queue_name,
        jobs.task_name,
        jobs.lock,
        jobs.status::text AS status,
        jobs.scheduled_at,
        jobs.attempts,
        jobs.abort_requested,
        jobs.worker_id,
        jobs.args,
        clock_timestamp() AS database_now,
        EXISTS (
            SELECT 1
            FROM procrastinate_events AS pe
            WHERE pe.job_id = jobs.id AND pe.type = 'deferred_for_retry'
        ) AS has_retry_event,
        CASE
            WHEN jobs.lock IS NULL THEN FALSE
            ELSE EXISTS (
                SELECT 1
                FROM procrastinate_jobs AS other_jobs
                WHERE
                    other_jobs.lock = jobs.lock
                    AND other_jobs.id <> jobs.id
                    AND (
                        other_jobs.status = 'doing'
                        OR (
                            other_jobs.status = 'todo'
                            AND (
                                other_jobs.priority > jobs.priority
                                OR (
                                    other_jobs.priority = jobs.priority
                                    AND other_jobs.id < jobs.id
                                )
                            )
                        )
                    )
            )
        END AS blocked_by_lock
    FROM procrastinate_jobs AS jobs
"""

_JOB_SNAPSHOT_QUERY = text(
    f"""
    {_BASE_SNAPSHOT_SELECT}
    WHERE jobs.id = :job_id
    """
)

_JOBS_BATCH_SNAPSHOT_QUERY = text(
    f"""
    {_BASE_SNAPSHOT_SELECT}
    WHERE jobs.id = ANY(:job_ids)
    """
)


def _parse_job_args(raw_args: Any) -> dict[str, Any]:
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _row_to_snapshot(row: Any) -> ProcrastinateJobSnapshot:
    has_retry = bool(row["has_retry_event"]) if "has_retry_event" in row else False
    raw_args = row["args"] if "args" in row else {}
    return ProcrastinateJobSnapshot(
        job_id=int(row["id"]),
        queue_name=str(row["queue_name"]),
        task_name=str(row["task_name"]),
        lock=str(row["lock"]) if row["lock"] is not None else None,
        status=str(row["status"]),
        scheduled_at=row["scheduled_at"],
        attempts=int(row["attempts"]),
        abort_requested=bool(row["abort_requested"]),
        worker_id=int(row["worker_id"]) if row["worker_id"] is not None else None,
        database_now=row["database_now"],
        blocked_by_lock=bool(row["blocked_by_lock"]),
        has_retry_event=has_retry,
        args=_parse_job_args(raw_args),
    )


async def fetch_procrastinate_job_snapshot(
    session: AsyncSession,
    queue_job_id: int,
) -> ProcrastinateJobSnapshot | None:
    """Legge lo stato infrastrutturale di un job Procrastinate con lock status e clock DB.

    Esegue una singola query parametrizzata che acquisisce lo stato del job, il clock
    reale di PostgreSQL (evitando clock drift applicativo), l'eventuale evento di retry
    e valuta la reale contesa sul lock secondo la semantica di `procrastinate_fetch_job_v2`.
    """
    result = await session.execute(_JOB_SNAPSHOT_QUERY, {"job_id": queue_job_id})
    row: Any = result.mappings().one_or_none()
    if row is None:
        return None
    return _row_to_snapshot(row)


async def fetch_procrastinate_jobs_batch(
    session: AsyncSession,
    queue_job_ids: Sequence[int],
) -> dict[int, ProcrastinateJobSnapshot]:
    """Recupera in batch gli snapshot per evitare problemi N+1 in visualizzazioni aggregate."""
    if not queue_job_ids:
        return {}
    unique_ids = list(dict.fromkeys(queue_job_ids))
    result = await session.execute(_JOBS_BATCH_SNAPSHOT_QUERY, {"job_ids": unique_ids})
    snapshots: dict[int, ProcrastinateJobSnapshot] = {}
    for row in result.mappings().all():
        snapshot = _row_to_snapshot(row)
        snapshots[snapshot.job_id] = snapshot
    return snapshots
