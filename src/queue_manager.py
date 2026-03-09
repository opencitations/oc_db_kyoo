import asyncio
import time
import logging
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, Optional

import httpx

logger = logging.getLogger("oc_db_kyoo")


class CircuitState(str, Enum):
    CLOSED = "closed"        # Healthy — traffic flows normally
    OPEN = "open"            # Down — no traffic, waiting for probe
    HALF_OPEN = "half_open"  # Probing — health checker is confirming recovery


@dataclass
class BackendStats:
    """Real-time statistics for a single backend."""
    name: str
    active_requests: int = 0
    queued_requests: int = 0
    total_requests: int = 0
    total_completed: int = 0
    total_errors: int = 0
    total_timeouts: int = 0
    total_rejected: int = 0
    total_circuit_breaks: int = 0
    avg_response_time_ms: float = 0.0
    circuit_state: str = "closed"
    _response_times: list = field(default_factory=list, repr=False)

    def record_response_time(self, duration_ms: float):
        """Track response time with a rolling window of last 100 requests."""
        self._response_times.append(duration_ms)
        if len(self._response_times) > 100:
            self._response_times.pop(0)
        self.avg_response_time_ms = sum(self._response_times) / len(self._response_times)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "active_requests": self.active_requests,
            "queued_requests": self.queued_requests,
            "total_requests": self.total_requests,
            "total_completed": self.total_completed,
            "total_errors": self.total_errors,
            "total_timeouts": self.total_timeouts,
            "total_rejected": self.total_rejected,
            "total_circuit_breaks": self.total_circuit_breaks,
            "avg_response_time_ms": round(self.avg_response_time_ms, 2),
            "circuit_state": self.circuit_state,
        }


class BackendQueue:
    """
    Manages concurrency, queuing, and circuit breaker for a single database backend.

    Circuit breaker states:
      CLOSED    → healthy, traffic flows normally
      OPEN      → backend is down, all requests skip this backend
      HALF_OPEN → first health probe succeeded, waiting for second confirmation

    Recovery is driven entirely by the health checker:
      OPEN → (probe OK) → HALF_OPEN → (probe OK) → CLOSED
      OPEN → (probe OK) → HALF_OPEN → (probe FAIL) → OPEN

    No real user traffic is sent to HALF_OPEN backends.

    Queue drain:
      When the circuit transitions to OPEN, a drain event is fired so that
      all requests already waiting in queue bail out immediately (return False)
      instead of sitting there for queue_timeout seconds.
    """

    def __init__(self, name: str, max_concurrent: int, max_queue: int,
                 queue_timeout: int, cb_threshold: int, cb_recovery_time: int):
        self.name = name
        self.max_concurrent = max_concurrent
        self.max_queue = max_queue
        self.queue_timeout = queue_timeout

        # Circuit breaker config
        self.cb_threshold = cb_threshold
        self.cb_recovery_time = cb_recovery_time

        # Circuit breaker state
        self._circuit_state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._last_failure_time: float = 0
        self._circuit_lock = asyncio.Lock()

        # Drain event — set when circuit opens to wake up queued requests
        self._drain_event = asyncio.Event()

        # Semaphore controls how many requests hit the backend concurrently
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._queue_count = 0
        self._lock = asyncio.Lock()
        self.stats = BackendStats(name=name)

    @property
    def circuit_state(self) -> CircuitState:
        return self._circuit_state

    @property
    def is_available(self) -> bool:
        """Backend is available for routing only if circuit is CLOSED."""
        return self._circuit_state == CircuitState.CLOSED

    @property
    def active_requests(self) -> int:
        return self.max_concurrent - self._semaphore._value

    @property
    def queued_requests(self) -> int:
        return self._queue_count

    @property
    def total_load(self) -> int:
        """Total load = active + queued. Used for least-queue routing."""
        return self.active_requests + self._queue_count

    def is_queue_full(self) -> bool:
        return self._queue_count >= self.max_queue

    async def record_connection_success(self):
        """
        A request (or probe) succeeded. Reset circuit breaker.
        Called for both real requests and health check probes.
        """
        async with self._circuit_lock:
            old_state = self._circuit_state
            self._consecutive_failures = 0
            self._circuit_state = CircuitState.CLOSED
            self.stats.circuit_state = self._circuit_state.value
            # Clear drain event so new requests can queue normally
            self._drain_event.clear()
            if old_state != CircuitState.CLOSED:
                logger.info(
                    f"[{self.name}] Circuit CLOSED — backend recovered "
                    f"(was {old_state.value})"
                )

    async def record_connection_failure(self):
        """
        A connection-level failure occurred.
        This includes: ConnectError, ConnectTimeout, ReadTimeout, WriteTimeout,
        or any other transport-level failure.
        NOT called for HTTP 4xx/5xx from the database — those mean the db is alive.
        """
        async with self._circuit_lock:
            self._consecutive_failures += 1
            self._last_failure_time = time.monotonic()

            if (self._circuit_state == CircuitState.CLOSED and
                    self._consecutive_failures >= self.cb_threshold):
                self._circuit_state = CircuitState.OPEN
                self.stats.circuit_state = self._circuit_state.value
                self.stats.total_circuit_breaks += 1
                # Fire drain event — wake up all queued requests immediately
                self._drain_event.set()
                logger.warning(
                    f"[{self.name}] Circuit OPEN — {self._consecutive_failures} "
                    f"consecutive failures — draining {self._queue_count} queued requests"
                )
            elif self._circuit_state == CircuitState.HALF_OPEN:
                # The health check confirmation failed — back to open
                self._circuit_state = CircuitState.OPEN
                self.stats.circuit_state = self._circuit_state.value
                self._drain_event.set()
                logger.warning(
                    f"[{self.name}] Circuit back to OPEN — half-open confirmation failed"
                )

    async def try_transition_to_half_open(self) -> bool:
        """
        Called by the health checker when a probe succeeds on an OPEN backend.
        Transitions to HALF_OPEN so the next health check can confirm recovery.
        Returns True if transition happened.
        """
        async with self._circuit_lock:
            if self._circuit_state != CircuitState.OPEN:
                return False

            elapsed = time.monotonic() - self._last_failure_time
            if elapsed < self.cb_recovery_time:
                return False

            self._circuit_state = CircuitState.HALF_OPEN
            self.stats.circuit_state = self._circuit_state.value
            # Clear drain event (no traffic goes here anyway, but keep state clean)
            self._drain_event.clear()
            logger.info(
                f"[{self.name}] Circuit HALF_OPEN — first probe succeeded, "
                f"waiting for confirmation"
            )
            return True

    async def acquire(self) -> bool:
        """
        Try to acquire a slot to send a request to this backend.

        Returns True if acquired (immediately or after queuing).
        Returns False if queue is full, circuit is not CLOSED, or circuit
        opens while waiting (drain event).
        Raises asyncio.TimeoutError if queue_timeout is exceeded.

        The method checks the drain event every 0.5s so that when the
        circuit opens, queued requests bail out within ~500ms instead
        of waiting the full queue_timeout (180s).
        """
        # Fast-fail if circuit is not CLOSED
        if self._circuit_state != CircuitState.CLOSED:
            self.stats.total_rejected += 1
            return False

        async with self._lock:
            if self._queue_count >= self.max_queue and self._semaphore.locked():
                self.stats.total_rejected += 1
                return False
            self._queue_count += 1
            self.stats.queued_requests = self._queue_count
            self.stats.total_requests += 1

        try:
            deadline = time.monotonic() + self.queue_timeout

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # Queue timeout expired
                    async with self._lock:
                        self._queue_count -= 1
                        self.stats.queued_requests = self._queue_count
                        self.stats.total_timeouts += 1
                    raise asyncio.TimeoutError()

                # Check drain event — circuit opened while we were waiting?
                if self._drain_event.is_set():
                    async with self._lock:
                        self._queue_count -= 1
                        self.stats.queued_requests = self._queue_count
                        self.stats.total_rejected += 1
                    logger.debug(
                        f"[{self.name}] Request drained from queue (circuit open)"
                    )
                    return False

                # Try to acquire semaphore with a short timeout
                # so we can re-check the drain event periodically
                check_interval = min(remaining, 0.5)
                try:
                    await asyncio.wait_for(
                        self._semaphore.acquire(),
                        timeout=check_interval,
                    )
                    # Got the semaphore — we're through
                    async with self._lock:
                        self._queue_count -= 1
                        self.stats.queued_requests = self._queue_count
                        self.stats.active_requests = self.active_requests
                    return True
                except asyncio.TimeoutError:
                    # Semaphore not available yet, loop back
                    continue

        except asyncio.TimeoutError:
            raise

    def release(self):
        self._semaphore.release()
        self.stats.active_requests = self.active_requests

    def record_success(self, duration_ms: float):
        self.stats.total_completed += 1
        self.stats.record_response_time(duration_ms)

    def record_error(self):
        self.stats.total_errors += 1


class HealthChecker:
    """
    Background task that probes OPEN and HALF_OPEN backends with a real
    SPARQL query to detect and confirm recovery.

    Recovery flow (driven entirely by health checker, no user traffic involved):
      OPEN → (probe OK) → HALF_OPEN → (probe OK) → CLOSED
      OPEN → (probe OK) → HALF_OPEN → (probe FAIL) → OPEN
    """

    def __init__(self, queue_manager: 'QueueManager', backend_urls: Dict[str, str],
                 interval: int, timeout: int, query: str):
        self._qm = queue_manager
        self._backend_urls = backend_urls  # name -> base URL
        self._interval = interval
        self._timeout = timeout
        self._query = query
        self._task: Optional[asyncio.Task] = None
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=float(timeout), write=5.0, pool=10.0),
            follow_redirects=False,
        )

    async def start(self):
        self._task = asyncio.create_task(self._loop())
        logger.info(
            f"Health checker started: interval={self._interval}s, "
            f"timeout={self._timeout}s, query='{self._query}'"
        )

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._client.aclose()
        logger.info("Health checker stopped")

    async def _loop(self):
        """Main loop: periodically probe backends that need checking."""
        while True:
            try:
                await asyncio.sleep(self._interval)
                await self._check_backends()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health checker error: {e}")

    async def _check_backends(self):
        """Probe all OPEN and HALF_OPEN backends (primary + fallback) concurrently."""
        check_backends = [
            bq for bq in self._qm._backends.values()
            if bq.circuit_state in (CircuitState.OPEN, CircuitState.HALF_OPEN)
        ]
        check_backends += [
            bq for bq in self._qm._fallback_backends.values()
            if bq.circuit_state in (CircuitState.OPEN, CircuitState.HALF_OPEN)
        ]
        if not check_backends:
            return

        tasks = [self._probe_backend(bq) for bq in check_backends]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _probe_backend(self, bq: BackendQueue):
        """Send a lightweight query to check if a backend has recovered."""
        url = self._backend_urls.get(bq.name)
        if not url:
            return

        try:
            resp = await self._client.get(
                url,
                params={"query": self._query},
                headers={"Accept": "application/sparql-results+json"},
            )
            if resp.status_code < 500:
                # Any non-5xx means the db process is alive and responding.
                if bq.circuit_state == CircuitState.OPEN:
                    # First probe OK → move to HALF_OPEN
                    logger.info(
                        f"[{bq.name}] Health probe OK (status {resp.status_code}) "
                        f"— moving to HALF_OPEN"
                    )
                    await bq.try_transition_to_half_open()
                elif bq.circuit_state == CircuitState.HALF_OPEN:
                    # Second probe OK → confirmed, move to CLOSED
                    logger.info(
                        f"[{bq.name}] Health probe OK (status {resp.status_code}) "
                        f"— confirmed recovery, moving to CLOSED"
                    )
                    await bq.record_connection_success()
            else:
                # 5xx response
                if bq.circuit_state == CircuitState.HALF_OPEN:
                    logger.warning(
                        f"[{bq.name}] Health probe failed (status {resp.status_code}) "
                        f"— back to OPEN"
                    )
                    await bq.record_connection_failure()
                else:
                    logger.debug(
                        f"[{bq.name}] Health probe got {resp.status_code} — still down"
                    )

        except httpx.ConnectError:
            logger.debug(f"[{bq.name}] Health probe — connection refused")
            if bq.circuit_state == CircuitState.HALF_OPEN:
                await bq.record_connection_failure()
        except httpx.TimeoutException:
            logger.debug(f"[{bq.name}] Health probe — timeout")
            if bq.circuit_state == CircuitState.HALF_OPEN:
                await bq.record_connection_failure()
        except Exception as e:
            logger.debug(f"[{bq.name}] Health probe — error: {e}")
            if bq.circuit_state == CircuitState.HALF_OPEN:
                await bq.record_connection_failure()


class QueueManager:
    """
    Manages two pools of backend queues:
      - Primary pool: normal backends, used first
      - Fallback pool: reserve backends, used only when ALL primaries are down (circuit OPEN)

    Each pool has independent concurrency/queue/timeout settings.
    Both pools have circuit breaker and health checking.
    """

    def __init__(self, max_concurrent: int, max_queue: int, queue_timeout: int,
                 cb_threshold: int, cb_recovery_time: int):
        self.max_concurrent = max_concurrent
        self.max_queue = max_queue
        self.queue_timeout = queue_timeout
        self.cb_threshold = cb_threshold
        self.cb_recovery_time = cb_recovery_time
        self._backends: Dict[str, BackendQueue] = {}

        # Fallback pool — separate limits
        self._fallback_backends: Dict[str, BackendQueue] = {}
        self._fallback_max_concurrent = 3
        self._fallback_max_queue = 20
        self._fallback_queue_timeout = 120

    def configure_fallback_pool(self, max_concurrent: int, max_queue: int,
                                queue_timeout: int):
        """Set limits for the fallback pool (called before adding fallback backends)."""
        self._fallback_max_concurrent = max_concurrent
        self._fallback_max_queue = max_queue
        self._fallback_queue_timeout = queue_timeout

    def add_backend(self, name: str) -> BackendQueue:
        bq = BackendQueue(
            name=name,
            max_concurrent=self.max_concurrent,
            max_queue=self.max_queue,
            queue_timeout=self.queue_timeout,
            cb_threshold=self.cb_threshold,
            cb_recovery_time=self.cb_recovery_time,
        )
        self._backends[name] = bq
        logger.info(
            f"Primary backend '{name}' registered: "
            f"max_concurrent={self.max_concurrent}, "
            f"max_queue={self.max_queue}, "
            f"queue_timeout={self.queue_timeout}s, "
            f"cb_threshold={self.cb_threshold}, "
            f"cb_recovery={self.cb_recovery_time}s"
        )
        return bq

    def add_fallback_backend(self, name: str) -> BackendQueue:
        bq = BackendQueue(
            name=name,
            max_concurrent=self._fallback_max_concurrent,
            max_queue=self._fallback_max_queue,
            queue_timeout=self._fallback_queue_timeout,
            cb_threshold=self.cb_threshold,
            cb_recovery_time=self.cb_recovery_time,
        )
        self._fallback_backends[name] = bq
        logger.info(
            f"Fallback backend '{name}' registered: "
            f"max_concurrent={self._fallback_max_concurrent}, "
            f"max_queue={self._fallback_max_queue}, "
            f"queue_timeout={self._fallback_queue_timeout}s, "
            f"cb_threshold={self.cb_threshold}, "
            f"cb_recovery={self.cb_recovery_time}s"
        )
        return bq

    def get_backend(self, name: str) -> Optional[BackendQueue]:
        return self._backends.get(name) or self._fallback_backends.get(name)

    def _all_primaries_down(self) -> bool:
        """True if every primary backend has its circuit OPEN."""
        return all(
            bq.circuit_state == CircuitState.OPEN
            for bq in self._backends.values()
        )

    def _select_from_pool(self, pool: Dict[str, BackendQueue]) -> Optional[BackendQueue]:
        """Pick the least-loaded available backend from a pool.
        Only CLOSED backends receive user traffic.
        HALF_OPEN backends are tested exclusively by the health checker."""
        available = [
            bq for bq in pool.values()
            if bq.circuit_state == CircuitState.CLOSED
            and (not bq.is_queue_full() or not bq._semaphore.locked())
        ]
        if not available:
            return None
        return min(available, key=lambda bq: bq.total_load)

    def select_backend(self) -> Optional[BackendQueue]:
        """
        Two-tier routing:
          1. Try primary pool first (CLOSED backends only)
          2. If ALL primaries are down (circuit OPEN), try fallback pool
        """
        primary = self._select_from_pool(self._backends)
        if primary is not None:
            return primary

        # Only use fallback if ALL primaries have open circuits
        if self._fallback_backends and self._all_primaries_down():
            fallback = self._select_from_pool(self._fallback_backends)
            if fallback is not None:
                logger.info(
                    f"All primaries down — routing to fallback '{fallback.name}'"
                )
                return fallback

        return None

    def all_stats(self) -> list:
        """Stats for primary backends."""
        return [bq.stats.to_dict() for bq in self._backends.values()]

    def all_fallback_stats(self) -> list:
        """Stats for fallback backends."""
        return [bq.stats.to_dict() for bq in self._fallback_backends.values()]

    def is_healthy(self) -> bool:
        """Service is healthy if at least one backend (primary or fallback) can accept requests."""
        primary_ok = any(
            bq.circuit_state == CircuitState.CLOSED
            and (not bq.is_queue_full() or not bq._semaphore.locked())
            for bq in self._backends.values()
        )
        if primary_ok:
            return True

        # Check fallback pool
        if self._fallback_backends and self._all_primaries_down():
            return any(
                bq.circuit_state == CircuitState.CLOSED
                and (not bq.is_queue_full() or not bq._semaphore.locked())
                for bq in self._fallback_backends.values()
            )
        return False

    @property
    def backend_names(self) -> list:
        return list(self._backends.keys())

    @property
    def fallback_backend_names(self) -> list:
        return list(self._fallback_backends.keys())

    @property
    def has_fallback(self) -> bool:
        return len(self._fallback_backends) > 0