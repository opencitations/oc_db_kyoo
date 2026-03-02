import asyncio
import time
import logging
from typing import Dict

import httpx
from fastapi import Request, Response
from fastapi.responses import HTMLResponse

from src.config import AppConfig, BackendConfig
from src.queue_manager import QueueManager, BackendQueue, HealthChecker

logger = logging.getLogger("oc_db_kyoo")

BUSY_HTML = """<!DOCTYPE html>
<html>
<head><title>Service Busy</title></head>
<body style="font-family:sans-serif;text-align:center;padding:60px;">
<h1>503 &mdash; Backend Busy</h1>
<p>All database backends are currently overloaded or unavailable. Please try again later.</p>
</body>
</html>"""


class Router:
    """
    Receives incoming HTTP requests and proxies them to database backends
    through the QueueManager (least-queue routing with circuit breaker).
    """

    def __init__(self, config: AppConfig):
        self.config = config

        # Map backend name -> BackendConfig
        self._backend_configs: Dict[str, BackendConfig] = {}
        for b in config.backends:
            self._backend_configs[b.name] = b

        # Queue manager with circuit breaker
        self.queue_manager = QueueManager(
            max_concurrent=config.max_concurrent_per_backend,
            max_queue=config.max_queue_per_backend,
            queue_timeout=config.queue_timeout,
            cb_threshold=config.circuit_breaker_threshold,
            cb_recovery_time=config.circuit_breaker_recovery_time,
        )
        for b in config.backends:
            self.queue_manager.add_backend(b.name)

        # Health checker for probing OPEN backends
        backend_urls = {b.name: b.url for b in config.backends}
        self.health_checker = HealthChecker(
            queue_manager=self.queue_manager,
            backend_urls=backend_urls,
            interval=config.health_check_interval,
            timeout=config.health_check_timeout,
            query=config.health_check_query,
        )

        # Async HTTP client
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=float(config.backend_timeout),
                write=10.0,
                pool=float(config.backend_timeout),
            ),
            limits=httpx.Limits(
                max_connections=len(config.backends) * config.max_concurrent_per_backend * 2,
                max_keepalive_connections=len(config.backends) * config.max_concurrent_per_backend,
            ),
            follow_redirects=False,
        )

    async def start(self):
        """Start background tasks (health checker)."""
        await self.health_checker.start()

    async def close(self):
        """Shutdown: stop health checker, close HTTP client."""
        await self.health_checker.stop()
        await self._client.aclose()

    async def proxy_request(self, request: Request) -> Response:
        """Main entry point: route request to the least-loaded available backend."""
        backend_queue = self.queue_manager.select_backend()
        if backend_queue is None:
            logger.warning("All backends busy or down — rejecting request")
            return HTMLResponse(content=BUSY_HTML, status_code=503)

        backend_name = backend_queue.name
        backend_config = self._backend_configs[backend_name]

        # Try to acquire a queue slot
        try:
            acquired = await backend_queue.acquire()
            if not acquired:
                return await self._try_fallback_backends(request, exclude=backend_name)
        except asyncio.TimeoutError:
            logger.warning(f"Queue timeout for backend '{backend_name}'")
            return HTMLResponse(content=BUSY_HTML, status_code=503)

        # Forward the request
        start_time = time.monotonic()
        try:
            response = await self._forward_request(request, backend_config)
            duration_ms = (time.monotonic() - start_time) * 1000
            backend_queue.record_success(duration_ms)

            # Connection succeeded — reset circuit breaker
            await backend_queue.record_connection_success()

            logger.debug(
                f"[{backend_name}] {request.method} completed in {duration_ms:.0f}ms "
                f"(status {response.status_code})"
            )
            return response

        except httpx.ConnectError as e:
            logger.error(f"[{backend_name}] Connection error: {e}")
            backend_queue.record_error()
            await backend_queue.record_connection_failure()
            return Response(
                content=f"Backend '{backend_name}' connection error",
                status_code=502,
                media_type="text/plain",
            )
        except httpx.TimeoutException as e:
            logger.error(f"[{backend_name}] Backend timeout after {self.config.backend_timeout}s")
            backend_queue.record_error()
            # Only connect timeouts trigger the circuit breaker.
            # Read timeouts mean the db is alive but slow.
            if isinstance(e, httpx.ConnectTimeout):
                await backend_queue.record_connection_failure()
            return Response(
                content=f"Backend '{backend_name}' timeout",
                status_code=504,
                media_type="text/plain",
            )
        except Exception as e:
            logger.error(f"[{backend_name}] Unexpected error: {e}")
            backend_queue.record_error()
            await backend_queue.record_connection_failure()
            return Response(
                content="Internal proxy error",
                status_code=500,
                media_type="text/plain",
            )
        finally:
            backend_queue.release()

    async def _try_fallback_backends(self, request: Request, exclude: str) -> Response:
        """Try other available backends if the first choice was full."""
        for name, bq in self.queue_manager._backends.items():
            if name == exclude:
                continue
            if not bq.is_available:
                continue
            if bq.is_queue_full() and bq._semaphore.locked():
                continue

            try:
                acquired = await bq.acquire()
                if not acquired:
                    continue
            except asyncio.TimeoutError:
                continue

            backend_config = self._backend_configs[name]
            start_time = time.monotonic()
            try:
                response = await self._forward_request(request, backend_config)
                duration_ms = (time.monotonic() - start_time) * 1000
                bq.record_success(duration_ms)
                await bq.record_connection_success()
                return response
            except httpx.ConnectError as e:
                logger.error(f"[{name}] Fallback connection error: {e}")
                bq.record_error()
                await bq.record_connection_failure()
                return Response(content="Backend error", status_code=502, media_type="text/plain")
            except Exception as e:
                logger.error(f"[{name}] Fallback error: {e}")
                bq.record_error()
                await bq.record_connection_failure()
                return Response(content="Backend error", status_code=502, media_type="text/plain")
            finally:
                bq.release()

        logger.warning("All backends busy or down (fallback exhausted) — rejecting request")
        return HTMLResponse(content=BUSY_HTML, status_code=503)

    async def _forward_request(self, request: Request, backend: BackendConfig) -> Response:
        """Forward the HTTP request to the target backend."""
        target_url = backend.url
        if request.url.query:
            target_url += f"?{request.url.query}"

        body = await request.body()

        headers = dict(request.headers)
        hop_by_hop = {"host", "connection", "keep-alive", "transfer-encoding",
                       "te", "trailer", "upgrade", "proxy-authorization",
                       "proxy-authenticate"}
        forward_headers = {
            k: v for k, v in headers.items()
            if k.lower() not in hop_by_hop
        }
        forward_headers["host"] = f"{backend.host}:{backend.port}"

        resp = await self._client.request(
            method=request.method,
            url=target_url,
            headers=forward_headers,
            content=body,
        )

        excluded_response_headers = {"transfer-encoding", "connection", "keep-alive",
                                      "content-encoding", "content-length"}
        response_headers = {
            k: v for k, v in resp.headers.items()
            if k.lower() not in excluded_response_headers
        }
        response_headers["X-Served-By"] = backend.name

        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=response_headers,
            media_type=resp.headers.get("content-type"),
        )