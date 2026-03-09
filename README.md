# oc_db_kyoo

Database Queue Manager for OpenCitations. Sits between the caching layer (Varnish/Redis) and the database backends, managing per-backend request queuing, concurrency limiting, circuit breaking, and least-queue load balancing.

## Overview

**oc_db_kyoo** (pronounced like "queue") is an async HTTP reverse proxy that protects database backends from overload by:

- Limiting concurrent requests per backend
- Queuing excess requests with configurable depth and timeout
- Routing requests to the least-loaded backend (least-queue strategy)
- **Circuit breaker** with three states (CLOSED → OPEN → HALF_OPEN) to fast-fail when a backend is unreachable
- **Active health checking** to detect and confirm backend recovery automatically
- **Two-tier routing** with a fallback pool that activates when all primary backends are down
- **Real-time dashboard** for monitoring backend state, circuit status, and queue metrics
- Returning a friendly "backend busy" page when all backends are saturated

Each instance of oc_db_kyoo manages **one type** of database (e.g., all Virtuoso instances, or all QLever instances). Each backend within that instance gets its own independent queue and circuit breaker.

## Architecture

```
Service (oc-api, oc-sparql, oc-search, ...)
  → Varnish (HTTP cache)
    → Redis (secondary cache)
      → oc_db_kyoo (Virtuoso)  → [virtuoso-1, virtuoso-2, ...]  (primary pool)
                                → [virtuoso-fb-1, ...]           (fallback pool)
      → oc_db_kyoo (QLever)    → [qlever-1, qlever-2, ...]      (primary pool)
                                → [qlever-fb-1, ...]             (fallback pool)
```

## Configuration

### conf.json

```json
{
  "listen_port": 8080,
  "log_level": "info",
  "backends": [
    {
      "name": "virtuoso-1",
      "host": "virtuoso-1.default.svc.cluster.local",
      "port": 8890,
      "path": "/sparql"
    },
    {
      "name": "virtuoso-2",
      "host": "virtuoso-2.default.svc.cluster.local",
      "port": 8890,
      "path": "/sparql"
    },
    {
      "name": "virtuoso-3",
      "host": "virtuoso-3.default.svc.cluster.local",
      "port": 8890,
      "path": "/sparql"
    }
  ],
  "max_concurrent_per_backend": 20,
  "max_queue_per_backend": 100,
  "queue_timeout": 60,
  "backend_timeout": 60,
  "circuit_breaker_threshold": 3,
  "circuit_breaker_recovery_time": 15,
  "health_check_interval": 10,
  "health_check_timeout": 5,
  "health_check_query": "ASK WHERE { ?s ?p ?o }",
  "fallback_backends": [
    {
      "name": "virtuoso-fb-1",
      "host": "virtuoso-fb-1.default.svc.cluster.local",
      "port": 8890,
      "path": "/sparql"
    }
  ],
  "fallback_max_concurrent_per_backend": 3,
  "fallback_max_queue_per_backend": 20,
  "fallback_queue_timeout": 120,
  "fallback_backend_timeout": 120
}
```

### Parameter Reference

| Parameter | Description | Default |
|---|---|---|
| `listen_port` | Port the service listens on | `8080` |
| `log_level` | Logging level (debug, info, warning, error) | `info` |
| `backends` | List of primary database backends | — |
| `max_concurrent_per_backend` | Max simultaneous requests sent to each primary backend | `10` |
| `max_queue_per_backend` | Max requests waiting in queue per primary backend | `50` |
| `queue_timeout` | Max seconds a request can wait in queue before being dropped | `120` |
| `backend_timeout` | Max seconds to wait for a primary backend response | `900` |
| `circuit_breaker_threshold` | Consecutive connection failures before opening the circuit | `3` |
| `circuit_breaker_recovery_time` | Seconds to wait before probing an OPEN backend | `15` |
| `health_check_interval` | Seconds between health check probes on OPEN/HALF_OPEN backends | `10` |
| `health_check_timeout` | Timeout in seconds for each health check probe | `5` |
| `health_check_query` | SPARQL query used for health probes | `ASK WHERE { ?s ?p ?o }` |
| `fallback_backends` | List of fallback backends (activated when all primaries are down) | `[]` |
| `fallback_max_concurrent_per_backend` | Max simultaneous requests per fallback backend | `3` |
| `fallback_max_queue_per_backend` | Max queue depth per fallback backend | `20` |
| `fallback_queue_timeout` | Max seconds a request waits in fallback queue | `120` |
| `fallback_backend_timeout` | Max seconds to wait for a fallback backend response | `120` |

### Environment Variables

Environment variables override `conf.json` values (Docker/Kubernetes pattern):

```env
LISTEN_PORT=8080
LOG_LEVEL=info

# Queue settings
MAX_CONCURRENT_PER_BACKEND=10
MAX_QUEUE_PER_BACKEND=250
QUEUE_TIMEOUT=180
BACKEND_TIMEOUT=60

# Circuit breaker
CIRCUIT_BREAKER_THRESHOLD=3
CIRCUIT_BREAKER_RECOVERY_TIME=15

# Health check
HEALTH_CHECK_INTERVAL=10
HEALTH_CHECK_TIMEOUT=5
HEALTH_CHECK_QUERY=ASK WHERE { ?s ?p ?o }

# Primary backends (BACKEND_N_*)
BACKEND_0_NAME=virtuoso-1
BACKEND_0_HOST=virtuoso-1.default.svc.cluster.local
BACKEND_0_PORT=8890
BACKEND_0_PATH=/sparql
BACKEND_1_NAME=virtuoso-2
BACKEND_1_HOST=virtuoso-2.default.svc.cluster.local
BACKEND_1_PORT=8890
BACKEND_1_PATH=/sparql

# Fallback backends (FALLBACK_N_*)
FALLBACK_MAX_CONCURRENT_PER_BACKEND=3
FALLBACK_MAX_QUEUE_PER_BACKEND=20
FALLBACK_QUEUE_TIMEOUT=120
FALLBACK_BACKEND_TIMEOUT=120
FALLBACK_0_NAME=virtuoso-fb-1
FALLBACK_0_HOST=virtuoso-fb-1.default.svc.cluster.local
FALLBACK_0_PORT=8890
FALLBACK_0_PATH=/sparql
```

The service discovers backends by scanning `BACKEND_N_HOST` and `FALLBACK_N_HOST` env vars (N=0,1,2,...). If env vars exist for a pool, they replace the corresponding `conf.json` entries entirely.

## Circuit Breaker

Each backend has an independent circuit breaker with three states:

```
        ┌─ 2nd probe ok ────────────────────────┐
        │                                        │
    CLOSED ──N failures──▶ OPEN ──probe ok──▶ HALF_OPEN
        ▲                   ▲                    │
        │                   └── probe failed ────┘
        │                                        │
        └──── 2nd probe ok ─────────────────────┘
```

- **CLOSED** — healthy, traffic flows normally.
- **OPEN** — backend is unreachable. All queued requests are drained immediately (no waiting for `queue_timeout`). New requests skip this backend. A background health checker probes at regular intervals.
- **HALF_OPEN** — first health probe succeeded, waiting for confirmation. No user traffic is routed to this backend. The health checker sends a second probe after one interval. If it succeeds → CLOSED. If it fails → back to OPEN.

Recovery is driven entirely by the health checker — no real user requests are involved in the recovery process.

Connection-level failures that trigger the circuit breaker include `ConnectError`, `ConnectTimeout`, `ReadTimeout`, `WriteTimeout`, and any other transport-level `httpx.TimeoutException`. HTTP 4xx/5xx responses do **not** trip the breaker (the database process is alive).

## Fallback Pool

When **all** primary backends have their circuit OPEN, the fallback pool activates:

- Fallback backends have their own independent concurrency limits, queue depth, and timeouts.
- Each fallback backend has its own circuit breaker.
- As soon as any primary backend recovers (circuit returns to CLOSED), traffic routes back to the primary pool.

This two-tier design ensures that reserve capacity is available during full primary outages without affecting normal-operation routing.

## Health Checker

A background task runs every `health_check_interval` seconds and probes all OPEN and HALF_OPEN backends (both primary and fallback) by sending a real SPARQL query:

```
GET <backend_url>?query=ASK WHERE { ?s ?p ?o }
Accept: application/sparql-results+json
```

The health checker drives the full recovery process:

1. **OPEN backend**: If the probe gets a response with status < 500, the backend transitions to HALF_OPEN.
2. **HALF_OPEN backend**: If the probe gets a response with status < 500, the backend transitions to CLOSED (confirmed recovery). If the probe fails, the backend goes back to OPEN.

This two-step confirmation ensures a backend is reliably responding before it receives user traffic again.

> **Note**: Virtuoso requires `ASK WHERE { ?s ?p ?o }` without `LIMIT`. QLever backends may use a different query via `HEALTH_CHECK_QUERY`.

## Timeout Logging

Backend timeouts are logged to a dedicated file `timeout_requests.log` with full detail including the SPARQL query text, client IP, and user agent, separated clearly for analysis.

## Endpoints

| Endpoint | Description |
|---|---|
| `/{path}` | Proxy — all requests are forwarded to the least-loaded backend |
| `/health` | Liveness/readiness probe: 200 if at least one CLOSED backend (primary or fallback) can accept requests, 503 otherwise |
| `/status` | Detailed per-backend queue and circuit breaker statistics (JSON) |
| `/dashboard` | Real-time monitoring dashboard with auto-refresh (HTML) |

### /status response example

```json
{
  "status": "ok",
  "all_primaries_down": false,
  "backends": [
    {
      "name": "virtuoso-1",
      "active_requests": 3,
      "queued_requests": 0,
      "total_requests": 1250,
      "total_completed": 1245,
      "total_errors": 2,
      "total_timeouts": 3,
      "total_rejected": 0,
      "total_circuit_breaks": 0,
      "avg_response_time_ms": 245.67,
      "circuit_state": "closed"
    },
    {
      "name": "virtuoso-2",
      "active_requests": 5,
      "queued_requests": 2,
      "total_requests": 1180,
      "total_completed": 1175,
      "total_errors": 1,
      "total_timeouts": 4,
      "total_rejected": 0,
      "total_circuit_breaks": 1,
      "avg_response_time_ms": 312.45,
      "circuit_state": "closed"
    }
  ],
  "fallback_backends": [
    {
      "name": "virtuoso-fb-1",
      "active_requests": 0,
      "queued_requests": 0,
      "total_requests": 0,
      "total_completed": 0,
      "total_errors": 0,
      "total_timeouts": 0,
      "total_rejected": 0,
      "total_circuit_breaks": 0,
      "avg_response_time_ms": 0.0,
      "circuit_state": "closed"
    }
  ]
}
```

### /dashboard

The dashboard shows color-coded cards for each backend with real-time circuit state, queue meters, and request counters. Fallback backends are displayed in a separate section with an activation banner indicating whether the fallback pool is active or on standby. Auto-refreshes every 2 seconds.

## How it works

1. A request arrives at oc_db_kyoo
2. The **least-queue router** selects the backend with the lowest total load (active + queued) from the primary pool
3. Only backends with a **CLOSED** circuit receive user traffic
4. If the selected backend has capacity, the request is forwarded immediately
5. If the backend is at max concurrency, the request enters the backend's queue
6. If the queue is full, other available CLOSED backends (including fallback if all primaries are down) are tried
7. If the circuit **opens** while requests are queued, they are **drained immediately** instead of waiting for the full queue timeout
8. If all backends are saturated or down, a **503 "Backend Busy"** page is returned
9. A **health checker** probes OPEN and HALF_OPEN backends periodically using a real SPARQL query
10. Recovery requires two consecutive successful probes: OPEN → HALF_OPEN (first probe OK) → CLOSED (second probe OK). No user traffic is involved in recovery

## Running

### Local development

```bash
# Install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install dependencies and create virtual environment
uv sync

# Run the application
uv run python app.py

# Or with a custom port
uv run python app.py --port 9090
```

### Docker

The image version is read from `pyproject.toml` (single source of truth). To publish a new image on DockerHub, update the `version` field in `pyproject.toml` and push to `main` — the GitHub Actions workflow will build and push the new tag automatically, skipping the build if that version already exists.

```bash
VERSION=$(grep -m1 '^version' pyproject.toml | cut -d'"' -f2)

docker build -t opencitations/oc_db_kyoo:$VERSION .
docker run -p 8080:8080 \
  -e MAX_CONCURRENT_PER_BACKEND=5 \
  -e BACKEND_0_NAME=db1 \
  -e BACKEND_0_HOST=localhost \
  -e BACKEND_0_PORT=8890 \
  -e BACKEND_0_PATH=/sparql \
  opencitations/oc_db_kyoo:$VERSION
```

### Kubernetes

See `manifest-example.yaml` and `.env.example` for deployment templates.

## Tech Stack

- **Python 3.11+**
- **[uv](https://docs.astral.sh/uv/)** — fast Python package manager
- **FastAPI** + **uvicorn** — async HTTP framework
- **httpx** — async HTTP client for backend forwarding
- **asyncio.Semaphore** — concurrency control per backend
- **Pydantic** — configuration validation