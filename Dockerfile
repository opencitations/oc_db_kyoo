# Base image: Python slim for a lightweight container
FROM python:3.11-slim

# Define environment variables with default values
# These can be overridden during container runtime (Docker / Kubernetes)
ENV LISTEN_PORT="8080" \
    LOG_LEVEL="info" \
    MAX_CONCURRENT_PER_BACKEND="10" \
    MAX_QUEUE_PER_BACKEND="250" \
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
    # BACKEND_1_NAME="virtuoso-2"
    # BACKEND_1_HOST="virtuoso-2.default.svc.cluster.local"
    # BACKEND_1_PORT="8890"
    # BACKEND_1_PATH="/sparql"

# Ensure Python output is unbuffered
ENV PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && \
    apt-get install curl -y \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy application code
COPY . .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Expose the service port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

# Start the application with uvicorn
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "info"]