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

# Dedicated logger for timeout requests
timeout_logger = logging.getLogger("oc_db_kyoo.timeouts")
_timeout_handler_initialized = False


def _init_timeout_logger():
    global _timeout_handler_initialized
    if _timeout_handler_initialized:
        return
    handler = logging.FileHandler("timeout_requests.log", mode="a")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [TIMEOUT] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    timeout_logger.addHandler(handler)
    timeout_logger.setLevel(logging.WARNING)
    _timeout_handler_initialized = True


def _extract_request_info(request: Request, body: bytes) -> dict:
    """Extract useful debug info from the incoming request."""
    client_ip = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent", "unknown")

    query = ""
    if request.method == "POST" and body:
        try:
            decoded = body.decode("utf-8", errors="replace")
            if "query=" in decoded:
                for part in decoded.split("&"):
                    if part.startswith("query="):
                        query = part[6:]
                        break
            else:
                query = decoded
        except Exception:
            query = "(unreadable)"
    elif request.method == "GET":
        query = request.query_params.get("query", "")

    return {
        "client": client_ip,
        "user_agent": user_agent,
        "method": request.method,
        "query": query,
    }


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
    through the QueueManager (two-tier routing: primary + fallback pool).
    """

    def __init__(self, config: AppConfig):
        self.config = config

        # Map backend name -> BackendConfig (primary + fallback)
        self._backend_configs: Dict[str, BackendConfig] = {}
        for b in config.backends:
            self._backend_configs[b.name] = b
        for b in config.fallback_backends:
            self._backend_configs[b.name] = b

        # Track which backends are fallback (for choosing the right HTTP client)
        self._fallback_names = {b.name for b in config.fallback_backends}

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

        # Fallback pool
        if config.fallback_backends:
            self.queue_manager.configure_fallback_pool(
                max_concurrent=config.fallback_max_concurrent_per_backend,
                max_queue=config.fallback_max_queue_per_backend,
                queue_timeout=config.fallback_queue_timeout,
            )
            for b in config.fallback_backends:
                self.queue_manager.add_fallback_backend(b.name)

        # Health checker — URLs for all backends (primary + fallback)
        backend_urls = {b.name: b.url for b in config.backends}
        for b in config.fallback_backends:
            backend_urls[b.name] = b.url
        self.health_checker = HealthChecker(
            queue_manager=self.queue_manager,
            backend_urls=backend_urls,
            interval=config.health_check_interval,
            timeout=config.health_check_timeout,
            query=config.health_check_query,
        )

        # Primary HTTP client
        total_primary = len(config.backends) * config.max_concurrent_per_backend
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=float(config.backend_timeout),
                write=10.0,
                pool=float(config.backend_timeout),
            ),
            limits=httpx.Limits(
                max_connections=total_primary * 2,
                max_keepalive_connections=total_primary,
            ),
            follow_redirects=False,
        )

        # Fallback HTTP client (separate timeout)
        if config.fallback_backends:
            total_fallback = len(config.fallback_backends) * config.fallback_max_concurrent_per_backend
            self._fallback_client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=10.0,
                    read=float(config.fallback_backend_timeout),
                    write=10.0,
                    pool=float(config.fallback_backend_timeout),
                ),
                limits=httpx.Limits(
                    max_connections=total_fallback * 2,
                    max_keepalive_connections=total_fallback,
                ),
                follow_redirects=False,
            )
        else:
            self._fallback_client = None

        _init_timeout_logger()

    def _get_client(self, backend_name: str) -> httpx.AsyncClient:
        """Return the appropriate HTTP client for the backend."""
        if backend_name in self._fallback_names and self._fallback_client:
            return self._fallback_client
        return self._client

    async def start(self):
        await self.health_checker.start()

    async def close(self):
        await self.health_checker.stop()
        await self._client.aclose()
        if self._fallback_client:
            await self._fallback_client.aclose()

    async def proxy_request(self, request: Request) -> Response:
        """Main entry point: route request to the best available backend."""
        backend_queue = self.queue_manager.select_backend()
        if backend_queue is None:
            logger.warning("All backends busy or down - rejecting request")
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

        # Read body early so we can log it on timeout
        body = await request.body()
        request_info = _extract_request_info(request, body)

        start_time = time.monotonic()
        try:
            response = await self._forward_request(request, backend_config, body, backend_name)
            duration_ms = (time.monotonic() - start_time) * 1000
            backend_queue.record_success(duration_ms)
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
                status_code=502, media_type="text/plain",
            )
        except httpx.TimeoutException as e:
            duration_ms = (time.monotonic() - start_time) * 1000
            logger.warning(
                f"[{backend_name}] BACKEND TIMEOUT after {duration_ms:.0f}ms"
            )
            timeout_logger.warning(
                f"[{backend_name}] {duration_ms:.0f}ms | "
                f"client={request_info['client']} | "
                f"user_agent={request_info['user_agent']}\n"
                f"{request_info['query']}\n"
                f"{'─' * 80}"
            )
            backend_queue.record_error()
            await backend_queue.record_connection_failure()
            return Response(
                content=f"Backend '{backend_name}' timeout",
                status_code=504, media_type="text/plain",
            )
        except Exception as e:
            logger.error(f"[{backend_name}] Unexpected error: {e}")
            backend_queue.record_error()
            await backend_queue.record_connection_failure()
            return Response(
                content="Internal proxy error",
                status_code=500, media_type="text/plain",
            )
        finally:
            backend_queue.release()

    async def _try_fallback_backends(self, request: Request, exclude: str) -> Response:
        """Try other available backends (primary + fallback) if the first choice was full."""
        # Collect all backend queues from both pools
        all_queues = list(self.queue_manager._backends.items()) + \
                     list(self.queue_manager._fallback_backends.items())

        for name, bq in all_queues:
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
            body = await request.body()
            request_info = _extract_request_info(request, body)
            start_time = time.monotonic()
            try:
                response = await self._forward_request(request, backend_config, body, name)
                duration_ms = (time.monotonic() - start_time) * 1000
                bq.record_success(duration_ms)
                await bq.record_connection_success()
                return response
            except httpx.ConnectError as e:
                logger.error(f"[{name}] Fallback connection error: {e}")
                bq.record_error()
                await bq.record_connection_failure()
                return Response(content="Backend error", status_code=502, media_type="text/plain")
            except httpx.TimeoutException as e:
                duration_ms = (time.monotonic() - start_time) * 1000
                logger.warning(
                    f"[{name}] FALLBACK TIMEOUT after {duration_ms:.0f}ms"
                )
                timeout_logger.warning(
                    f"[{name}] (fallback) {duration_ms:.0f}ms | "
                    f"client={request_info['client']} | "
                    f"user_agent={request_info['user_agent']}\n"
                    f"{request_info['query']}\n"
                    f"{'─' * 80}"
                )
                bq.record_error()
                await bq.record_connection_failure()
                return Response(
                    content=f"Backend '{name}' timeout",
                    status_code=504, media_type="text/plain",
                )
            except Exception as e:
                logger.error(f"[{name}] Fallback error: {e}")
                bq.record_error()
                await bq.record_connection_failure()
                return Response(content="Backend error", status_code=502, media_type="text/plain")
            finally:
                bq.release()

        logger.warning("All backends busy or down (fallback exhausted) - rejecting request")
        return HTMLResponse(content=BUSY_HTML, status_code=503)

    async def _forward_request(self, request: Request, backend: BackendConfig,
                               body: bytes, backend_name: str) -> Response:
        """Forward the HTTP request to the target backend."""
        target_url = backend.url
        if request.url.query:
            target_url += f"?{request.url.query}"

        headers = dict(request.headers)
        hop_by_hop = {"host", "connection", "keep-alive", "transfer-encoding",
                       "te", "trailer", "upgrade", "proxy-authorization",
                       "proxy-authenticate"}
        forward_headers = {
            k: v for k, v in headers.items()
            if k.lower() not in hop_by_hop
        }
        forward_headers["host"] = f"{backend.host}:{backend.port}"

        client = self._get_client(backend_name)
        resp = await client.request(
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