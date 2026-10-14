from __future__ import annotations

import enum
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from gbrain.domain.models import ImportJob, ImportJobStatus
from gbrain.infrastructure.procrastinate_jobs import (
    ProcrastinateJobSnapshot,
    fetch_procrastinate_job_snapshot,
    fetch_procrastinate_jobs_batch,
)
from gbrain.infrastructure.task_queue import GMAIL_QUEUE_NAME, GMAIL_TASK_NAME


class ImportJobRuntimeStatus(enum.StrEnum):
    """Stato derivato e unificato tra ciclo di vita applicativo e infrastruttura di coda."""

    SCHEDULED_RETRY = "scheduled_retry"
    SCHEDULED_RUN = "scheduled_run"
    QUEUED = "queued"
    BLOCKED = "blocked"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    TERMINAL_MISMATCH = "terminal_mismatch"
    MISSING_QUEUE = "missing_queue"
    UNKNOWN = "unknown"


class ImportJobConsistency(enum.StrEnum):
    """Diagnosi di coerenza tra stato persistito su ImportJob e tabella procrastinate_jobs."""

    CONSISTENT = "consistent"
    QUEUE_NOT_EXPECTED = "queue_not_expected"
    QUEUE_MISSING = "queue_missing"
    TERMINAL_MISMATCH = "terminal_mismatch"
    EXTERNAL_ABORT_REQUESTED = "external_abort_requested"
    OBSERVATION_FAILED = "observation_failed"


class ImportJobCancelState(enum.StrEnum):
    """Stato operativo del comando di cancellazione."""

    AVAILABLE = "available"
    REQUESTED = "requested"
    COMPLETED = "completed"
    NOT_AVAILABLE = "not_available"


class ImportJobCancelStrategy(enum.StrEnum):
    """Strategia di annullamento disponibile per l'utente."""

    IMMEDIATE_IF_QUEUED_ELSE_COOPERATIVE = "immediate_if_queued_else_cooperative"
    NONE = "none"


REASON_ALLOWLIST: dict[str, str] = {
    "gmail_transient": "Errore di connessione transitorio con i servizi Google; ripresa programmata.",
    "gmail_quota": "Quota o limite di frequenza Google temporaneamente superato; ripresa programmata.",
    "transient_retry": "Errore temporaneo; nuovo tentativo programmato.",
    "gmail_rate_limit_exceeded": "Limite di velocità Google API raggiunto; ripresa programmata.",
    "gmail_history_expired": "Cronologia Gmail scaduta; necessaria riconciliazione completa.",
    "stale_config_revision": "Configurazione modificata; il lavoro precedente è stato sostituito.",
    "alias_required": "Nessun indirizzo email confermato per l'account.",
    "policy_not_found": "Policy di importazione non trovata.",
    "policy_confirmation_mismatch": "La configurazione confermata non corrisponde all'anteprima.",
    "enqueue_failed": "Impossibile accodare il lavoro alla coda di elaborazione.",
    "account_job_already_active": "Un'altra operazione è già attiva su questa fonte.",
    "retry_budget_exhausted": "Superato il numero massimo di tentativi automatici.",
}


@dataclass(frozen=True, slots=True)
class RuntimeReason:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class RuntimeQueueInfo:
    job_id: int | None
    present: bool | None
    status: str | None
    attempts: int | None
    scheduled_at: datetime | None
    database_now: datetime | None
    blocked_by_lock: bool | None
    abort_requested: bool | None


@dataclass(frozen=True, slots=True)
class RuntimeRetryInfo:
    attempts: int
    scheduled_at: datetime
    database_now: datetime
    remaining_seconds: float


@dataclass(frozen=True, slots=True)
class RuntimeCancelInfo:
    allowed: bool
    state: ImportJobCancelState
    strategy: ImportJobCancelStrategy


@dataclass(frozen=True, slots=True)
class ImportJobRuntime:
    status: ImportJobRuntimeStatus
    active: bool
    reason: RuntimeReason | None
    consistency: ImportJobConsistency
    queue: RuntimeQueueInfo
    retry: RuntimeRetryInfo | None
    cancel: RuntimeCancelInfo


def sanitize_runtime_reason(
    error_code: str | None,
    *,
    is_scheduled_retry: bool = False,
) -> RuntimeReason | None:
    """Restituisce un motivo d'errore o di retry sanitizzato mediante allowlist rigorosa.

    Evita la divulgazione di dettagli interni, token o stack trace a client e UI.
    """
    if error_code and error_code in REASON_ALLOWLIST:
        return RuntimeReason(code=error_code, message=REASON_ALLOWLIST[error_code])

    if is_scheduled_retry:
        return RuntimeReason(
            code="transient_retry",
            message=REASON_ALLOWLIST["transient_retry"],
        )

    return None


def is_queue_identity_coherent(
    job: ImportJob,
    queue_snapshot: ProcrastinateJobSnapshot,
) -> bool:
    """Verifica rigorosa dell'identità infrastrutturale del job in coda.

    Controlla che la coda sia esattamente GMAIL_QUEUE_NAME ('gmail'), il task sia
    GMAIL_TASK_NAME e che args contenga 'job_id' esattamente coincidente con job.id.
    Args assenti o malformati (non-dict o job_id mancante/diverso) sono trattati
    come mismatch.
    """
    if queue_snapshot.queue_name != GMAIL_QUEUE_NAME:
        return False
    if queue_snapshot.task_name != GMAIL_TASK_NAME:
        return False
    if not isinstance(queue_snapshot.args, dict):
        return False
    args_job_id = queue_snapshot.args.get("job_id")
    if args_job_id is None or str(args_job_id) != str(job.id):
        return False
    return True


def derive_import_job_runtime(
    job: ImportJob,
    queue_snapshot: ProcrastinateJobSnapshot | None,
    *,
    queue_error: Exception | None = None,
) -> ImportJobRuntime:
    """Deriva in modo conservativo lo stato runtime unificato per un ImportJob."""
    is_app_active = job.status in {ImportJobStatus.PENDING, ImportJobStatus.RUNNING}

    # 1. Errore nell'osservazione infrastrutturale della coda
    if queue_error is not None:
        queue_info = RuntimeQueueInfo(
            job_id=job.queue_job_id,
            present=None,
            status=None,
            attempts=None,
            scheduled_at=None,
            database_now=None,
            blocked_by_lock=None,
            abort_requested=None,
        )
        cancel_info = RuntimeCancelInfo(
            allowed=False,
            state=(
                ImportJobCancelState.REQUESTED
                if job.cancel_requested_at is not None
                else ImportJobCancelState.NOT_AVAILABLE
            ),
            strategy=ImportJobCancelStrategy.NONE,
        )
        return ImportJobRuntime(
            status=ImportJobRuntimeStatus.UNKNOWN,
            active=is_app_active,
            reason=sanitize_runtime_reason(job.error_code),
            consistency=ImportJobConsistency.OBSERVATION_FAILED,
            queue=queue_info,
            retry=None,
            cancel=cancel_info,
        )

    # 2. Nessun queue_job_id associato o riga assente nella tabella procrastinate_jobs
    if queue_snapshot is None:
        queue_info = RuntimeQueueInfo(
            job_id=job.queue_job_id,
            present=False if job.queue_job_id is not None else None,
            status=None,
            attempts=None,
            scheduled_at=None,
            database_now=None,
            blocked_by_lock=None,
            abort_requested=None,
        )
        if is_app_active:
            derived_status = ImportJobRuntimeStatus.MISSING_QUEUE
            consistency = ImportJobConsistency.QUEUE_MISSING
        else:
            try:
                derived_status = ImportJobRuntimeStatus(job.status.value)
            except ValueError:
                derived_status = ImportJobRuntimeStatus.UNKNOWN
            consistency = ImportJobConsistency.QUEUE_NOT_EXPECTED

        cancel_allowed = is_app_active and job.cancel_requested_at is None
        cancel_state = (
            ImportJobCancelState.COMPLETED
            if job.status == ImportJobStatus.CANCELLED
            else ImportJobCancelState.REQUESTED
            if job.cancel_requested_at is not None
            else ImportJobCancelState.AVAILABLE
            if cancel_allowed
            else ImportJobCancelState.NOT_AVAILABLE
        )

        return ImportJobRuntime(
            status=derived_status,
            active=is_app_active,
            reason=sanitize_runtime_reason(job.error_code),
            consistency=consistency,
            queue=queue_info,
            retry=None,
            cancel=RuntimeCancelInfo(
                allowed=cancel_allowed,
                state=cancel_state,
                strategy=(
                    ImportJobCancelStrategy.IMMEDIATE_IF_QUEUED_ELSE_COOPERATIVE
                    if is_app_active
                    else ImportJobCancelStrategy.NONE
                ),
            ),
        )

    # 3. Riga di coda presente: verifica coerenza task_name e job_id negli args
    queue_info = RuntimeQueueInfo(
        job_id=queue_snapshot.job_id,
        present=True,
        status=queue_snapshot.status,
        attempts=queue_snapshot.attempts,
        scheduled_at=queue_snapshot.scheduled_at,
        database_now=queue_snapshot.database_now,
        blocked_by_lock=queue_snapshot.blocked_by_lock,
        abort_requested=queue_snapshot.abort_requested,
    )

    if not is_queue_identity_coherent(job, queue_snapshot):
        return ImportJobRuntime(
            status=ImportJobRuntimeStatus.TERMINAL_MISMATCH,
            active=is_app_active,
            reason=sanitize_runtime_reason(job.error_code),
            consistency=ImportJobConsistency.TERMINAL_MISMATCH,
            queue=queue_info,
            retry=None,
            cancel=RuntimeCancelInfo(
                allowed=False,
                state=ImportJobCancelState.NOT_AVAILABLE,
                strategy=ImportJobCancelStrategy.NONE,
            ),
        )

    # Controllo abort esterno requested sulla coda Procrastinate
    if queue_snapshot.abort_requested and is_app_active:
        derived_status = (
            ImportJobRuntimeStatus.CANCELLING
            if job.cancel_requested_at is not None
            else ImportJobRuntimeStatus.UNKNOWN
        )
        return ImportJobRuntime(
            status=derived_status,
            active=is_app_active,
            reason=sanitize_runtime_reason(job.error_code),
            consistency=ImportJobConsistency.EXTERNAL_ABORT_REQUESTED,
            queue=queue_info,
            retry=None,
            cancel=RuntimeCancelInfo(
                allowed=False,
                state=ImportJobCancelState.REQUESTED,
                strategy=ImportJobCancelStrategy.NONE,
            ),
        )

    retry_info: RuntimeRetryInfo | None = None

    if is_app_active:
        if queue_snapshot.status == "todo":
            if (
                queue_snapshot.scheduled_at is not None
                and queue_snapshot.scheduled_at > queue_snapshot.database_now
            ):
                remaining = max(
                    0.0,
                    (queue_snapshot.scheduled_at - queue_snapshot.database_now).total_seconds(),
                )
                retry_info = RuntimeRetryInfo(
                    attempts=queue_snapshot.attempts,
                    scheduled_at=queue_snapshot.scheduled_at,
                    database_now=queue_snapshot.database_now,
                    remaining_seconds=remaining,
                )
                # Retry programmato SOLO se oggettivamente dimostrato; altrimenti prima esecuzione programmata
                is_demonstrated_retry = bool(
                    queue_snapshot.attempts > 0
                    or queue_snapshot.has_retry_event
                    or (job.retry_count > 0)
                )
                if is_demonstrated_retry:
                    derived_status = ImportJobRuntimeStatus.SCHEDULED_RETRY
                else:
                    derived_status = ImportJobRuntimeStatus.SCHEDULED_RUN
                consistency = ImportJobConsistency.CONSISTENT
            elif queue_snapshot.blocked_by_lock:
                derived_status = ImportJobRuntimeStatus.BLOCKED
                consistency = ImportJobConsistency.CONSISTENT
            else:
                derived_status = ImportJobRuntimeStatus.QUEUED
                consistency = ImportJobConsistency.CONSISTENT
        elif queue_snapshot.status == "doing":
            if job.cancel_requested_at is not None:
                derived_status = ImportJobRuntimeStatus.CANCELLING
            else:
                derived_status = ImportJobRuntimeStatus.RUNNING
            consistency = ImportJobConsistency.CONSISTENT
        else:
            # Job applicativo attivo, ma coda terminata (succeeded, failed, cancelled, aborted)
            derived_status = ImportJobRuntimeStatus.TERMINAL_MISMATCH
            consistency = ImportJobConsistency.TERMINAL_MISMATCH
    else:
        # Job applicativo terminale
        if queue_snapshot.status in {"todo", "doing"}:
            derived_status = ImportJobRuntimeStatus.TERMINAL_MISMATCH
            consistency = ImportJobConsistency.TERMINAL_MISMATCH
        else:
            # Entrambi terminali: verifica compatibilità di transizione
            app_st = job.status
            q_st = queue_snapshot.status

            if (
                app_st
                in {
                    ImportJobStatus.COMPLETED,
                    ImportJobStatus.COMPLETED_WITH_WARNINGS,
                    ImportJobStatus.SUPERSEDED,
                }
                and q_st == "succeeded"
            ):
                derived_status = ImportJobRuntimeStatus(app_st.value)
                consistency = ImportJobConsistency.CONSISTENT
            elif app_st == ImportJobStatus.FAILED and q_st == "failed":
                derived_status = ImportJobRuntimeStatus.FAILED
                consistency = ImportJobConsistency.CONSISTENT
            elif app_st == ImportJobStatus.CANCELLED and q_st in {
                "cancelled",
                "succeeded",
                "aborted",
            }:
                derived_status = ImportJobRuntimeStatus.CANCELLED
                consistency = ImportJobConsistency.CONSISTENT
            else:
                derived_status = ImportJobRuntimeStatus.TERMINAL_MISMATCH
                consistency = ImportJobConsistency.TERMINAL_MISMATCH

    is_retry = derived_status == ImportJobRuntimeStatus.SCHEDULED_RETRY
    reason = sanitize_runtime_reason(job.error_code, is_scheduled_retry=is_retry)

    cancel_allowed = (
        is_app_active
        and job.cancel_requested_at is None
        and derived_status
        not in {
            ImportJobRuntimeStatus.TERMINAL_MISMATCH,
            ImportJobRuntimeStatus.UNKNOWN,
            ImportJobRuntimeStatus.MISSING_QUEUE,
        }
    )
    cancel_state = (
        ImportJobCancelState.COMPLETED
        if job.status == ImportJobStatus.CANCELLED
        else ImportJobCancelState.REQUESTED
        if job.cancel_requested_at is not None
        else ImportJobCancelState.AVAILABLE
        if cancel_allowed
        else ImportJobCancelState.NOT_AVAILABLE
    )

    cancel_info = RuntimeCancelInfo(
        allowed=cancel_allowed,
        state=cancel_state,
        strategy=(
            ImportJobCancelStrategy.IMMEDIATE_IF_QUEUED_ELSE_COOPERATIVE
            if is_app_active
            else ImportJobCancelStrategy.NONE
        ),
    )

    return ImportJobRuntime(
        status=derived_status,
        active=is_app_active,
        reason=reason,
        consistency=consistency,
        queue=queue_info,
        retry=retry_info,
        cancel=cancel_info,
    )


async def build_import_job_runtime(
    session: AsyncSession,
    job: ImportJob,
) -> ImportJobRuntime:
    """Interroga lo snapshot Procrastinate con fallback conservativo e ne deriva il runtime."""
    if job.queue_job_id is None:
        return derive_import_job_runtime(job, None)
    try:
        snapshot = await fetch_procrastinate_job_snapshot(session, job.queue_job_id)
        return derive_import_job_runtime(job, snapshot)
    except Exception as exc:
        return derive_import_job_runtime(job, None, queue_error=exc)


async def build_import_jobs_runtime_batch(
    session: AsyncSession,
    jobs: Sequence[ImportJob],
) -> dict[uuid.UUID, ImportJobRuntime]:
    """Costruisce i runtime per una sequenza di ImportJob eseguendo una singola query batch."""
    queue_ids = [j.queue_job_id for j in jobs if j.queue_job_id is not None]
    snapshots: dict[int, ProcrastinateJobSnapshot] = {}
    batch_error: Exception | None = None
    if queue_ids:
        try:
            snapshots = await fetch_procrastinate_jobs_batch(session, queue_ids)
        except Exception as exc:
            batch_error = exc

    results: dict[uuid.UUID, ImportJobRuntime] = {}
    for j in jobs:
        if batch_error is not None:
            results[j.id] = derive_import_job_runtime(j, None, queue_error=batch_error)
        elif j.queue_job_id is not None and j.queue_job_id in snapshots:
            results[j.id] = derive_import_job_runtime(j, snapshots[j.queue_job_id])
        else:
            results[j.id] = derive_import_job_runtime(j, None)
    return results
