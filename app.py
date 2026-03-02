import logging
import sys
import argparse
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src.config import load_config
from src.router import Router
from src.health import router as health_router, init_health
from src.dashboard import router as dashboard_router


# ── Logging setup ──────────────────────────────────────────────────────────────

def setup_logging(level: str):
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


# ── Application ────────────────────────────────────────────────────────────────

# Global router (initialized at startup)
_router: Router = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    global _router

    # Load configuration
    config = load_config()
    setup_logging(config.log_level)

    logger = logging.getLogger("oc_db_kyoo")
    logger.info("=" * 60)
    logger.info("  oc_db_kyoo — Database Queue Manager")
    logger.info("=" * 60)
    logger.info(f"  Listening on port: {config.listen_port}")
    logger.info(f"  Backends: {len(config.backends)}")
    for b in config.backends:
        logger.info(f"    → {b.name}: {b.url}")
    logger.info(f"  Max concurrent per backend: {config.max_concurrent_per_backend}")
    logger.info(f"  Max queue per backend: {config.max_queue_per_backend}")
    logger.info(f"  Queue timeout: {config.queue_timeout}s")
    logger.info(f"  Backend timeout: {config.backend_timeout}s")
    logger.info("=" * 60)

    # Initialize router and health checks
    _router = Router(config)
    init_health(_router.queue_manager)

    # Start background tasks (health checker)
    await _router.start()

    yield

    # Shutdown
    logger.info("Shutting down oc_db_kyoo...")
    await _router.close()


app = FastAPI(
    title="oc_db_kyoo",
    description="OpenCitations Database Queue Manager",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)

# Health and status endpoints
app.include_router(health_router)

# Include dashboard
app.include_router(dashboard_router)


@app.api_route("/{path:path}", methods=["GET", "POST", "HEAD", "OPTIONS"])
async def catch_all(request: Request, path: str):
    """
    Catch-all route: every request is proxied through to a database backend.
    The queue manager handles routing, queuing, and concurrency control.
    """
    if _router is None:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    return await _router.proxy_request(request)


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="oc_db_kyoo — Database Queue Manager")
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to listen on (overrides conf.json and LISTEN_PORT env)",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="conf.json",
        help="Path to configuration file (default: conf.json)",
    )
    args = parser.parse_args()

    # Quick pre-load to get the port
    config = load_config(args.config)
    port = args.port or config.listen_port

    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=port,
        log_level=config.log_level,
        access_log=True,
    )
