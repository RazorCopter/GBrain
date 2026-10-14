from __future__ import annotations

import logging
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import ClassVar

import psycopg
from psycopg.rows import tuple_row
from psycopg_pool import ConnectionPool

from gbrain.domain.connectors import ConnectorError, ConnectorErrorCategory

logger = logging.getLogger(__name__)


GMAIL_OAUTH_PRE_ACCOUNT_SCOPE: str = "global:oauth_pre_account"


def _normalize_scope(account_id: uuid.UUID | str) -> tuple[str, uuid.UUID | None]:
    """Normalizza la chiave di rate limiting in uno scope_key stringa e una FK opzionale.

    Per gli account persistiti, scope_key coincide con la rappresentazione canonica dell'UUID
    e connector_account_id valorizza la foreign key con vincolo ON DELETE CASCADE.
    Per lo scope pre-account globale ('global:oauth_pre_account'), connector_account_id è None,
    garantendo che tutti i processi condividano lo stesso pacing senza violazioni di vincoli
    e senza alcuna memorizzazione di PII (nessuna email, token o hash).
    """
    if isinstance(account_id, uuid.UUID):
        return str(account_id), account_id
    try:
        parsed = uuid.UUID(account_id)
        return str(parsed), parsed
    except ValueError:
        return str(account_id), None


class GmailRateLimiter(ABC):
    """Interfaccia del rate limiter Gmail per account o scope condiviso."""

    @abstractmethod
    def acquire(
        self,
        account_id: uuid.UUID | str,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> None:
        """Acquisisce uno slot per l'account o scope specificato.

        Garantisce che tra gli inizi di chiamate successive per lo stesso account o scope
        trascorra almeno l'intervallo previsto (es. 250 ms a 4 req/s), senza burst accumulato.
        Se is_cancelled() restituisce True durante l'attesa, solleva TimeoutError.
        """
        ...

    @abstractmethod
    def block_for(
        self,
        account_id: uuid.UUID | str,
        duration_seconds: float,
    ) -> None:
        """Applica un blocco/cooldown esplicito all'account o scope (es. da Retry-After)."""
        ...

    def apply_cooldown(
        self,
        account_id: uuid.UUID | str,
        duration_seconds: float,
    ) -> None:
        """Applica un cooldown all'account o scope (delega a block_for)."""
        self.block_for(account_id, duration_seconds)


class InMemoryNextSlotRateLimiter(GmailRateLimiter):
    """Implementazione in-memory con algoritmo next-slot per test e ambienti controllati."""

    def __init__(
        self,
        requests_per_second: float = 4.0,
        *,
        max_delay_seconds: float = 30.0,
        time_func: Callable[[], float] = time.monotonic,
        sleep_func: Callable[[float], None] = time.sleep,
    ) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second deve essere > 0")
        if max_delay_seconds <= 0:
            raise ValueError("max_delay_seconds deve essere > 0")
        self._interval_seconds = 1.0 / float(requests_per_second)
        self._max_delay_seconds = float(max_delay_seconds)
        self._time_func = time_func
        self._sleep_func = sleep_func
        self._lock = threading.Lock()
        self._next_slot_at: dict[str, float] = {}

    def acquire(
        self,
        account_id: uuid.UUID | str,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> None:
        if is_cancelled is not None and is_cancelled():
            raise TimeoutError("Operazione Gmail interrotta per cancellazione richiesta")

        key = str(account_id)
        with self._lock:
            now = self._time_func()
            current_next = self._next_slot_at.get(key, now)
            slot_at = max(now, current_next)
            self._next_slot_at[key] = slot_at + self._interval_seconds
            delay = slot_at - now

        if delay > self._max_delay_seconds:
            raise ConnectorError(
                ConnectorErrorCategory.QUOTA,
                f"Ritardo di rate limiting ({delay:.2f}s) supera il massimo consentito ({self._max_delay_seconds:.2f}s)",
                retryable=True,
                retry_after_seconds=delay,
            )

        if delay <= 0:
            return

        if is_cancelled is None:
            self._sleep_func(delay)
            return

        remaining = delay
        while remaining > 0:
            if is_cancelled():
                raise TimeoutError("Operazione Gmail interrotta per cancellazione richiesta")
            step = min(0.05, remaining)
            self._sleep_func(step)
            remaining -= step

    def block_for(
        self,
        account_id: uuid.UUID | str,
        duration_seconds: float,
    ) -> None:
        if duration_seconds <= 0:
            return
        key = str(account_id)
        with self._lock:
            target = self._time_func() + duration_seconds
            current = self._next_slot_at.get(key, 0.0)
            self._next_slot_at[key] = max(current, target)

    def apply_cooldown(
        self,
        account_id: uuid.UUID | str,
        duration_seconds: float,
    ) -> None:
        self.block_for(account_id, duration_seconds)


class PostgresGmailRateLimiter(GmailRateLimiter):
    """Rate limiter PostgreSQL condiviso cross-process con allocazione atomica next-slot.

    Caratteristiche:
    - Autorità temporale: clock_timestamp() di PostgreSQL.
    - Nessun burst: GREATEST(clock_timestamp(), next_slot_at, blocked_until).
    - Nessun lock trattenuto durante wait o HTTP: transazione brevissima rilasciata prima dello sleep.
    - Attesa cooperativa cancellabile: loop su intervalli ridotti con controllo is_cancelled().
    - Connessioni gestite via pool thread-safe per non saturare PostgreSQL.
    - Failure mode esplicito e retryable se il DB fallisce o il delay supera la soglia massima.
    """

    _pools: ClassVar[dict[str, ConnectionPool]] = {}
    _pool_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(
        self,
        connection_url: str,
        requests_per_second: float = 4.0,
        *,
        max_delay_seconds: float = 30.0,
        sleep_func: Callable[[float], None] = time.sleep,
    ) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second deve essere > 0")
        if max_delay_seconds <= 0:
            raise ValueError("max_delay_seconds deve essere > 0")
        self._conn_url = connection_url.replace("postgresql+psycopg://", "postgresql://")
        self._interval_seconds = 1.0 / float(requests_per_second)
        self._max_delay_seconds = float(max_delay_seconds)
        self._sleep_func = sleep_func

    def _get_pool(self) -> ConnectionPool:
        with self._pool_lock:
            pool = self._pools.get(self._conn_url)
            if pool is None or pool.closed:
                pool = ConnectionPool(
                    self._conn_url,
                    min_size=1,
                    max_size=10,
                    timeout=5.0,
                    kwargs={"autocommit": True},
                )
                self._pools[self._conn_url] = pool
            return pool

    def acquire(
        self,
        account_id: uuid.UUID | str,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> None:
        if is_cancelled is not None and is_cancelled():
            raise TimeoutError("Operazione Gmail interrotta per cancellazione richiesta")

        # Prenotazione atomica next-slot sul database
        delay_seconds = self._reserve_slot(account_id)

        if delay_seconds > self._max_delay_seconds:
            raise ConnectorError(
                ConnectorErrorCategory.QUOTA,
                f"Ritardo di rate limiting ({delay_seconds:.2f}s) supera il massimo consentito ({self._max_delay_seconds:.2f}s)",
                retryable=True,
                retry_after_seconds=delay_seconds,
            )

        if delay_seconds <= 0.0:
            return

        # Attesa cooperativa fuori da qualsiasi transazione o connessione DB
        remaining = delay_seconds
        while remaining > 0.0:
            if is_cancelled is not None and is_cancelled():
                raise TimeoutError("Operazione Gmail interrotta per cancellazione richiesta")
            step = min(0.05, remaining)
            self._sleep_func(step)
            remaining -= step

    def _reserve_slot(self, account_id: uuid.UUID | str) -> float:
        scope_key, connector_account_id = _normalize_scope(account_id)
        query = """
            INSERT INTO gmail_rate_limits (scope_key, connector_account_id, next_slot_at, updated_at)
            VALUES (%s, %s, clock_timestamp() + (%s * interval '1 second'), clock_timestamp())
            ON CONFLICT (scope_key) DO UPDATE
            SET connector_account_id = COALESCE(gmail_rate_limits.connector_account_id, EXCLUDED.connector_account_id),
                next_slot_at = GREATEST(
                    clock_timestamp(),
                    gmail_rate_limits.next_slot_at,
                    COALESCE(gmail_rate_limits.blocked_until, clock_timestamp())
                ) + (%s * interval '1 second'),
                updated_at = clock_timestamp()
            RETURNING
                GREATEST(
                    clock_timestamp(),
                    gmail_rate_limits.next_slot_at - (%s * interval '1 second'),
                    COALESCE(gmail_rate_limits.blocked_until, clock_timestamp())
                ) as granted_slot_at,
                clock_timestamp() as db_now;
        """
        try:
            pool = self._get_pool()
            with pool.connection() as conn:
                with conn.cursor(row_factory=tuple_row) as cur:
                    cur.execute(
                        query,
                        (
                            scope_key,
                            connector_account_id,
                            self._interval_seconds,
                            self._interval_seconds,
                            self._interval_seconds,
                        ),
                    )
                    row = cur.fetchone()
                    if not row:
                        return 0.0
                    granted_slot_at, db_now = row
                    delay = (granted_slot_at - db_now).total_seconds()
                    return max(0.0, float(delay))
        except (psycopg.Error, TimeoutError) as exc:
            logger.warning("Errore prenotazione rate limiter PostgreSQL: %s", exc)
            raise ConnectorError(
                ConnectorErrorCategory.TRANSIENT,
                f"Errore rate limiter PostgreSQL: {exc.__class__.__name__}",
                retryable=True,
            ) from exc

    def block_for(
        self,
        account_id: uuid.UUID | str,
        duration_seconds: float,
    ) -> None:
        if duration_seconds <= 0:
            return
        scope_key, connector_account_id = _normalize_scope(account_id)
        query = """
            INSERT INTO gmail_rate_limits (scope_key, connector_account_id, next_slot_at, blocked_until, updated_at)
            VALUES (
                %s,
                %s,
                clock_timestamp() + (%s * interval '1 second'),
                clock_timestamp() + (%s * interval '1 second'),
                clock_timestamp()
            )
            ON CONFLICT (scope_key) DO UPDATE
            SET connector_account_id = COALESCE(gmail_rate_limits.connector_account_id, EXCLUDED.connector_account_id),
                blocked_until = GREATEST(
                    COALESCE(gmail_rate_limits.blocked_until, clock_timestamp()),
                    clock_timestamp() + (%s * interval '1 second')
                ),
                next_slot_at = GREATEST(
                    gmail_rate_limits.next_slot_at,
                    clock_timestamp() + (%s * interval '1 second')
                ),
                updated_at = clock_timestamp();
        """
        try:
            pool = self._get_pool()
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        query,
                        (
                            scope_key,
                            connector_account_id,
                            duration_seconds,
                            duration_seconds,
                            duration_seconds,
                            duration_seconds,
                        ),
                    )
        except (psycopg.Error, TimeoutError) as exc:
            logger.warning("Errore block_for rate limiter PostgreSQL: %s", exc)
            raise ConnectorError(
                ConnectorErrorCategory.TRANSIENT,
                f"Errore rate limiter PostgreSQL: {exc.__class__.__name__}",
                retryable=True,
            ) from exc

    def apply_cooldown(
        self,
        account_id: uuid.UUID | str,
        duration_seconds: float,
    ) -> None:
        self.block_for(account_id, duration_seconds)
