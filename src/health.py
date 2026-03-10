import logging
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from src.queue_manager import QueueManager

logger = logging.getLogger("oc_db_kyoo")

router = APIRouter()

# Will be set by app.py at startup
_queue_manager: QueueManager = None


def init_health(queue_manager: QueueManager):
    global _queue_manager
    _queue_manager = queue_manager


@router.get("/ready")
async def ready():
    """
    Kubernetes liveness and readiness probe.
    Returns 200 if the oc_db_kyoo process is running and initialized.
    Returns 503 only during startup before initialization completes.

    This does NOT check backend health — that way the pod stays in the
    Service even when all backends are down, allowing oc_db_kyoo to
    respond with a proper "503 Backend Busy" page instead of
    "Connection refused".

    If the process is frozen/dead, this endpoint won't respond at all,
    and Kubernetes will kill the pod via liveness timeout.
    """
    if _queue_manager is None:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "message": "Service not initialized"}
        )

    return JSONResponse(
        status_code=200,
        content={"status": "ready"}
    )


@router.get("/health")
async def health():
    """
    Backend health check for monitoring and dashboards.
    Returns 200 if at least one backend (primary or fallback) can accept requests.
    Returns 503 if all backends are overloaded or have circuit OPEN.

    NOT used by Kubernetes probes — use /ready for that.
    """
    if _queue_manager is None:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "message": "Service not initialized"}
        )

    if _queue_manager.is_healthy():
        return JSONResponse(
            status_code=200,
            content={"status": "ok", "backends": len(_queue_manager.backend_names)}
        )
    else:
        return JSONResponse(
            status_code=503,
            content={"status": "overloaded", "message": "All backends are busy"}
        )


@router.get("/status")
async def status():
    """
    Detailed status endpoint showing per-backend queue statistics.
    Includes both primary and fallback pools.
    """
    if _queue_manager is None:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "message": "Service not initialized"}
        )

    response = {
        "status": "ok" if _queue_manager.is_healthy() else "overloaded",
        "backends": _queue_manager.all_stats(),
    }

    if _queue_manager.has_fallback:
        response["all_primaries_down"] = _queue_manager._all_primaries_down()
        response["fallback_backends"] = _queue_manager.all_fallback_stats()

    return JSONResponse(status_code=200, content=response)