from __future__ import annotations

import math
import random
from collections.abc import Callable

import procrastinate
from procrastinate.jobs import Job

from gbrain.application.gmail_jobs import GmailJobTransientError
from gbrain.config import Settings
from gbrain.domain.job_queue import GMAIL_QUEUE_IDENTITY, IMMICH_QUEUE_IDENTITY

GMAIL_QUEUE_NAME = GMAIL_QUEUE_IDENTITY.queue_name
GMAIL_TASK_NAME = GMAIL_QUEUE_IDENTITY.task_name

IMMICH_QUEUE_NAME = IMMICH_QUEUE_IDENTITY.queue_name
IMMICH_TASK_NAME = IMMICH_QUEUE_IDENTITY.task_name


class ImmichJobRetryStrategy(procrastinate.RetryStrategy):
    """Strategia di retry Procrastinate per Immich con backoff esponenziale bounded e jitter."""

    def __init__(
        self,
        *,
        max_attempts: int = 4,
        exponential_wait: int = 2,
        initial_wait_seconds: float = 2.0,
        max_wait_seconds: float = 60.0,
        jitter_ratio: float = 0.2,
        random_func: Callable[[], float] = random.random,
    ) -> None:
        from gbrain.domain.immich import ImmichGatewayError

        super().__init__(
            max_attempts=max_attempts,
            exponential_wait=exponential_wait,
            retry_exceptions=(ImmichGatewayError,),
        )
        self.initial_wait_seconds = initial_wait_seconds
        self.max_wait_seconds = max_wait_seconds
        self.jitter_ratio = jitter_ratio
        self.random_func = random_func

    def get_retry_decision(
        self,
        *,
        exception: BaseException,
        job: Job,
    ) -> procrastinate.retry.RetryDecision | None:
        from gbrain.domain.immich import ImmichGatewayError

        if not isinstance(exception, ImmichGatewayError) or not exception.retryable:
            return None
        if self.max_attempts is not None and job.attempts >= self.max_attempts:
            return None

        attempt_idx = max(0, job.attempts - 1)
        delay = min(
            self.max_wait_seconds,
            self.initial_wait_seconds * math.pow(self.exponential_wait, attempt_idx),
        )
        jitter = (self.random_func() * 2.0 - 1.0) * self.jitter_ratio * delay
        final_delay = max(0.0, delay + jitter)
        return procrastinate.retry.RetryDecision(retry_in={"seconds": math.ceil(final_delay)})


class GmailJobRetryStrategy(procrastinate.RetryStrategy):
    """Strategia di retry Procrastinate con backoff esponenziale bounded, jitter e rispetto di Retry-After."""

    def __init__(
        self,
        *,
        max_attempts: int,
        exponential_wait: int = 2,
        initial_wait_seconds: float | None = None,
        max_wait_seconds: float = 300.0,
        jitter_ratio: float = 0.2,
        retry_exceptions: tuple[type[Exception], ...] = (GmailJobTransientError,),
        random_func: Callable[[], float] = random.random,
    ) -> None:
        super().__init__(
            max_attempts=max_attempts,
            exponential_wait=exponential_wait,
            retry_exceptions=retry_exceptions,
        )
        self.initial_wait_seconds = float(initial_wait_seconds or exponential_wait)
        self.max_wait_seconds = max_wait_seconds
        self.jitter_ratio = jitter_ratio
        self.random_func = random_func

    def get_retry_decision(
        self,
        *,
        exception: BaseException,
        job: Job,
    ) -> procrastinate.retry.RetryDecision | None:
        if not isinstance(exception, GmailJobTransientError):
            return None
        if self.max_attempts is not None and job.attempts >= self.max_attempts:
            return None

        attempt_idx = max(0, job.attempts)
        base = min(self.max_wait_seconds, self.initial_wait_seconds * (2**attempt_idx))
        base = max(1.0, base)
        jitter = 1.0 - self.jitter_ratio + (2.0 * self.jitter_ratio * self.random_func())
        wait = base * jitter

        retry_after = getattr(exception, "retry_after_seconds", None)
        if retry_after is not None and retry_after > 0:
            wait = max(wait, float(retry_after))

        bounded_wait = min(self.max_wait_seconds, max(1.0, wait))
        return procrastinate.retry.RetryDecision(retry_in={"seconds": math.ceil(bounded_wait)})


def create_procrastinate_app(settings: Settings) -> procrastinate.App:
    connector = procrastinate.PsycopgConnector(conninfo=settings.procrastinate_dsn)
    return procrastinate.App(connector=connector)
