from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from gbrain.application.import_job_cancel import cancel_import_job
from gbrain.application.import_job_runtime import (
    ImportJobConsistency,
    ImportJobRuntimeStatus,
    derive_import_job_runtime,
    is_queue_identity_coherent,
)
from gbrain.domain.models import ImportJob, ImportJobStatus
from gbrain.infrastructure.procrastinate_jobs import ProcrastinateJobSnapshot


def _job(source_kind: str, *, queue_job_id: int | None = 42) -> ImportJob:
    return ImportJob(
        id=uuid.uuid4(),
        source_kind=source_kind,
        connector_account_id=uuid.uuid4(),
        mode="full",
        status=ImportJobStatus.PENDING,
        queue_job_id=queue_job_id,
        checkpoint={"phase": "queued"},
        policy_snapshot={},
    )


def _snapshot(job: ImportJob, *, source_kind: str | None = None) -> ProcrastinateJobSnapshot:
    kind = source_kind or job.source_kind
    queue_name, task_name = {
        "gmail": ("gmail", "gbrain.gmail_sync"),
        "immich": ("immich", "gbrain.immich_sync"),
    }[kind]
    return ProcrastinateJobSnapshot(
        job_id=job.queue_job_id or 42,
        queue_name=queue_name,
        task_name=task_name,
        lock=None,
        status="todo",
        scheduled_at=None,
        attempts=0,
        abort_requested=False,
        worker_id=None,
        database_now=datetime.now(UTC),
        blocked_by_lock=False,
        args={"job_id": str(job.id)},
    )


@pytest.mark.parametrize("source_kind", ["gmail", "immich"])
def test_queue_identity_accepts_each_connector_contract(source_kind: str) -> None:
    job = _job(source_kind)
    assert is_queue_identity_coherent(job, _snapshot(job)) is True


def test_queue_identity_rejects_crossed_unknown_missing_and_mismatched_args() -> None:
    immich_job = _job("immich")
    assert (
        is_queue_identity_coherent(immich_job, _snapshot(immich_job, source_kind="gmail")) is False
    )

    unknown_job = _job("unknown")
    gmail_snapshot = _snapshot(unknown_job, source_kind="gmail")
    assert is_queue_identity_coherent(unknown_job, gmail_snapshot) is False

    missing_args = replace(_snapshot(immich_job), args={})
    assert is_queue_identity_coherent(immich_job, missing_args) is False

    wrong_id = replace(_snapshot(immich_job), args={"job_id": str(uuid.uuid4())})
    assert is_queue_identity_coherent(immich_job, wrong_id) is False


def test_immich_runtime_with_valid_queue_is_not_terminal_mismatch() -> None:
    job = _job("immich")
    runtime = derive_import_job_runtime(job, _snapshot(job))
    assert runtime.status is ImportJobRuntimeStatus.QUEUED
    assert runtime.consistency is ImportJobConsistency.CONSISTENT


@pytest.mark.asyncio
async def test_cancel_immich_valid_and_idempotent() -> None:
    job = _job("immich")
    session = AsyncMock()
    queue = MagicMock()
    queue.job_manager.cancel_job_by_id_async = AsyncMock(return_value=True)

    with patch(
        "gbrain.application.import_job_cancel.fetch_procrastinate_job_snapshot",
        new=AsyncMock(return_value=_snapshot(job)),
    ):
        result = await cancel_import_job(session, queue, job, expected_source_kind="immich")

    assert result.status is ImportJobStatus.CANCELLED
    assert result.cancel_requested_at is not None
    queue.job_manager.cancel_job_by_id_async.assert_awaited_once_with(
        job_id=42, abort=False, delete_job=False
    )

    await cancel_import_job(session, queue, job, expected_source_kind="immich")
    queue.job_manager.cancel_job_by_id_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_immich_missing_queue_never_mutates_queue() -> None:
    job = _job("immich")
    session = AsyncMock()
    queue = MagicMock()
    queue.job_manager.cancel_job_by_id_async = AsyncMock()

    with patch(
        "gbrain.application.import_job_cancel.fetch_procrastinate_job_snapshot",
        new=AsyncMock(return_value=None),
    ):
        await cancel_import_job(session, queue, job, expected_source_kind="immich")

    assert job.status is ImportJobStatus.CANCELLED
    queue.job_manager.cancel_job_by_id_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_immich_rejects_alien_queue_without_mutation() -> None:
    job = _job("immich")
    session = AsyncMock()
    queue = MagicMock()
    queue.job_manager.cancel_job_by_id_async = AsyncMock()
    alien = _snapshot(job, source_kind="gmail")

    with (
        patch(
            "gbrain.application.import_job_cancel.fetch_procrastinate_job_snapshot",
            new=AsyncMock(return_value=alien),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await cancel_import_job(session, queue, job, expected_source_kind="immich")

    assert exc_info.value.status_code == 409
    assert job.status is ImportJobStatus.PENDING
    assert job.cancel_requested_at is None
    session.commit.assert_not_awaited()
    queue.job_manager.cancel_job_by_id_async.assert_not_awaited()
