from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from gbrain.application.import_job_runtime import is_queue_identity_coherent
from gbrain.domain.models import ImportJob, ImportJobStatus
from gbrain.infrastructure.procrastinate_jobs import fetch_procrastinate_job_snapshot

_QUEUE_TERMINAL_STATUSES = {"cancelled", "succeeded", "failed", "aborted"}
_APP_ACTIVE_STATUSES = {ImportJobStatus.PENDING, ImportJobStatus.RUNNING}


async def cancel_import_job(
    session: AsyncSession,
    queue: Any,
    job: ImportJob,
    *,
    expected_source_kind: str,
) -> ImportJob:
    """Richiede la cancellazione mirata, cooperativa e idempotente di un import job.

    L'identità applicativa e quella della coda vengono validate prima di qualunque
    mutazione. Non viene mai richiesto l'abort globale di un worker.
    """
    if job.source_kind != expected_source_kind:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lavoro non trovato")

    if job.status not in _APP_ACTIVE_STATUSES:
        return job

    if job.queue_job_id is None:
        now = datetime.now(UTC)
        job.cancel_requested_at = job.cancel_requested_at or now
        job.status = ImportJobStatus.CANCELLED
        job.finished_at = now
        job.checkpoint = {**(job.checkpoint or {}), "phase": "cancelled_no_queue"}
        await session.commit()
        await session.refresh(job)
        return job

    snapshot = await fetch_procrastinate_job_snapshot(session, job.queue_job_id)
    if snapshot is None:
        now = datetime.now(UTC)
        job.cancel_requested_at = job.cancel_requested_at or now
        job.status = ImportJobStatus.CANCELLED
        job.finished_at = now
        job.checkpoint = {**(job.checkpoint or {}), "phase": "cancelled_missing_queue"}
        await session.commit()
        await session.refresh(job)
        return job

    if not is_queue_identity_coherent(job, snapshot):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Incongruenza tra il lavoro applicativo e il record in coda di elaborazione",
        )

    if snapshot.status in _QUEUE_TERMINAL_STATUSES:
        if snapshot.status == "cancelled":
            now = datetime.now(UTC)
            job.status = ImportJobStatus.CANCELLED
            job.cancel_requested_at = job.cancel_requested_at or now
            job.finished_at = now
            job.checkpoint = {**(job.checkpoint or {}), "phase": "cancelled_reconciled"}
            await session.commit()
            await session.refresh(job)
        return job

    if job.cancel_requested_at is None:
        job.cancel_requested_at = datetime.now(UTC)
        job.checkpoint = {**(job.checkpoint or {}), "phase": "cancellation_requested"}
        await session.commit()
        await session.refresh(job)

    if snapshot.status == "todo":
        try:
            cancelled = await queue.job_manager.cancel_job_by_id_async(
                job_id=job.queue_job_id,
                abort=False,
                delete_job=False,
            )
        except Exception:
            cancelled = False

        if cancelled:
            job.status = ImportJobStatus.CANCELLED
            job.finished_at = datetime.now(UTC)
            job.checkpoint = {**(job.checkpoint or {}), "phase": "cancelled_in_queue"}
            await session.commit()
            await session.refresh(job)
        else:
            try:
                post_snapshot = await fetch_procrastinate_job_snapshot(session, job.queue_job_id)
            except Exception:
                post_snapshot = None
            if (
                post_snapshot is not None
                and is_queue_identity_coherent(job, post_snapshot)
                and post_snapshot.status == "cancelled"
            ):
                job.status = ImportJobStatus.CANCELLED
                job.finished_at = datetime.now(UTC)
                job.checkpoint = {**(job.checkpoint or {}), "phase": "cancelled_reconciled"}
                await session.commit()
                await session.refresh(job)

    return job
