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


@router.get("/health")
async def health():
    """
    Liveness/readiness probe for Kubernetes.
    Returns 200 if at least one backend (primary or fallback) can accept requests.
    Returns 503 if all backends are overloaded.
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