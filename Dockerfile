# Base image: Python slim for a lightweight container
FROM python:3.11-slim

# Define environment variables with default values
# These can be overridden during container runtime (Docker / Kubernetes)
ENV LISTEN_PORT="8080" \
    LOG_LEVEL="info" \
    MAX_CONCURRENT_PER_BACKEND="10" \
    MAX_QUEUE_PER_BACKEND="150" \
    QUEUE_TIMEOUT="180" \
    BACKEND_TIMEOUT="60" \
    CIRCUIT_BREAKER_THRESHOLD="3" \
    CIRCUIT_BREAKER_RECOVERY_TIME="15" \
    HEALTH_CHECK_INTERVAL="10" \
    HEALTH_CHECK_TIMEOUT="5" \
    HEALTH_CHECK_QUERY="ASK WHERE { ?s ?p ?o }"
    # Backend configuration via individual env vars:
    # BACKEND_0_NAME="virtuoso-1"
    # BACKEND_0_HOST="virtuoso-1.default.svc.cluster.local"
    # BACKEND_0_PORT="8890"
    # BACKEND_0_PATH="/sparql"
    #
    # Fallback backend configuration:
    # FALLBACK_MAX_CONCURRENT_PER_BACKEND="3"
    # FALLBACK_MAX_QUEUE_PER_BACKEND="20"
    # FALLBACK_QUEUE_TIMEOUT="120"
    # FALLBACK_BACKEND_TIMEOUT="120"
    # FALLBACK_0_NAME="virtuoso-fb-1"
    # FALLBACK_0_HOST="virtuoso-fb-1.default.svc.cluster.local"
    # FALLBACK_0_PORT="8890"
    # FALLBACK_0_PATH="/sparql"

# Ensure Python output is unbuffered
ENV PYTHONUNBUFFERED=1

# Install system dependencies + uv
RUN apt-get update && \
    apt-get install curl -y && \
    curl -LsSf https://astral.sh/uv/install.sh | sh && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Make uv available in PATH
ENV PATH="/root/.local/bin:$PATH"

# Set working directory
WORKDIR /app

# Copy dependency files first for better Docker layer caching
COPY pyproject.toml uv.lock ./

# Install dependencies (frozen = use exact lockfile versions)
RUN uv sync --frozen --no-dev --no-install-project

# Copy application code
COPY . .

# Expose the service port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

# Start the application with uvicorn
CMD ["uv", "run", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "info"]