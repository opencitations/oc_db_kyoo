import os
import json
import logging
from typing import List, Optional
from pydantic import BaseModel, field_validator

logger = logging.getLogger("oc_db_kyoo")


class BackendConfig(BaseModel):
    name: str
    host: str
    port: int
    path: str = "/"

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}{self.path}"


class AppConfig(BaseModel):
    listen_port: int = 8080
    log_level: str = "info"
    backends: List[BackendConfig]
    max_concurrent_per_backend: int = 10
    max_queue_per_backend: int = 50
    queue_timeout: int = 120
    backend_timeout: int = 900

    # Circuit breaker
    circuit_breaker_threshold: int = 3
    circuit_breaker_recovery_time: int = 15

    # Active health check
    health_check_interval: int = 10
    health_check_timeout: int = 5
    health_check_query: str = "ASK WHERE { ?s ?p ?o }"

    # Fallback backends (activated only when ALL primary backends are down)
    fallback_backends: List[BackendConfig] = []
    fallback_max_concurrent_per_backend: int = 3
    fallback_max_queue_per_backend: int = 20
    fallback_queue_timeout: int = 120
    fallback_backend_timeout: int = 120

    @field_validator("backends")
    @classmethod
    def check_backends_not_empty(cls, v):
        if not v:
            raise ValueError("At least one backend must be configured")
        return v

    @field_validator("max_concurrent_per_backend", "max_queue_per_backend",
                     "fallback_max_concurrent_per_backend", "fallback_max_queue_per_backend")
    @classmethod
    def check_positive(cls, v):
        if v < 1:
            raise ValueError("Value must be at least 1")
        return v

    @field_validator("queue_timeout", "backend_timeout",
                     "fallback_queue_timeout", "fallback_backend_timeout")
    @classmethod
    def check_timeout_positive(cls, v):
        if v < 1:
            raise ValueError("Timeout must be at least 1 second")
        return v

    @field_validator("circuit_breaker_threshold")
    @classmethod
    def check_cb_threshold(cls, v):
        if v < 1:
            raise ValueError("Circuit breaker threshold must be at least 1")
        return v

    @field_validator("circuit_breaker_recovery_time", "health_check_interval", "health_check_timeout")
    @classmethod
    def check_hc_positive(cls, v):
        if v < 1:
            raise ValueError("Value must be at least 1 second")
        return v


def _env_or_conf(env_key: str, conf_value, default, cast=str):
    """
    Priority: ENV var > conf.json value > hardcoded default.
    Returns (value, source_label) for logging.
    """
    env_val = os.getenv(env_key)
    if env_val is not None:
        return cast(env_val), "env"
    if conf_value is not None:
        return cast(conf_value), "conf.json"
    return cast(default), "default"


def load_config(config_path: str = "conf.json") -> AppConfig:
    """
    Load configuration with strict priority:
      1. Environment variables  (Kubernetes / Docker Compose / shell)
      2. conf.json              (local development defaults)
      3. Hardcoded defaults     (last resort)

    Backend discovery:
      - Primary: BACKEND_N_HOST env vars (N=0,1,2,...)
      - Fallback: FALLBACK_N_HOST env vars (N=0,1,2,...)
      - If env vars exist for a pool, conf.json backends for that pool are ignored.
    """

    # -- Load conf.json as base
    c = {}
    try:
        with open(config_path) as f:
            c = json.load(f)
        logger.info(f"Loaded base config from {config_path}")
    except FileNotFoundError:
        logger.warning(f"{config_path} not found - using env vars and defaults only")
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in {config_path}: {e} - using env vars and defaults only")

    # -- Global settings (env > conf.json > default)
    listen_port, lp_src = _env_or_conf("LISTEN_PORT", c.get("listen_port"), 8080, int)
    log_level, ll_src = _env_or_conf("LOG_LEVEL", c.get("log_level"), "info")
    max_concurrent, mc_src = _env_or_conf("MAX_CONCURRENT_PER_BACKEND", c.get("max_concurrent_per_backend"), 10, int)
    max_queue, mq_src = _env_or_conf("MAX_QUEUE_PER_BACKEND", c.get("max_queue_per_backend"), 50, int)
    queue_timeout, qt_src = _env_or_conf("QUEUE_TIMEOUT", c.get("queue_timeout"), 120, int)
    backend_timeout, bt_src = _env_or_conf("BACKEND_TIMEOUT", c.get("backend_timeout"), 900, int)

    # Circuit breaker
    cb_threshold, cbt_src = _env_or_conf("CIRCUIT_BREAKER_THRESHOLD", c.get("circuit_breaker_threshold"), 3, int)
    cb_recovery, cbr_src = _env_or_conf("CIRCUIT_BREAKER_RECOVERY_TIME", c.get("circuit_breaker_recovery_time"), 15, int)

    # Health check
    hc_interval, hci_src = _env_or_conf("HEALTH_CHECK_INTERVAL", c.get("health_check_interval"), 10, int)
    hc_timeout, hct_src = _env_or_conf("HEALTH_CHECK_TIMEOUT", c.get("health_check_timeout"), 5, int)
    hc_query, hcq_src = _env_or_conf("HEALTH_CHECK_QUERY", c.get("health_check_query"), "ASK WHERE { ?s ?p ?o }")

    # Fallback pool settings
    fb_max_concurrent, fmc_src = _env_or_conf("FALLBACK_MAX_CONCURRENT_PER_BACKEND", c.get("fallback_max_concurrent_per_backend"), 3, int)
    fb_max_queue, fmq_src = _env_or_conf("FALLBACK_MAX_QUEUE_PER_BACKEND", c.get("fallback_max_queue_per_backend"), 20, int)
    fb_queue_timeout, fqt_src = _env_or_conf("FALLBACK_QUEUE_TIMEOUT", c.get("fallback_queue_timeout"), 120, int)
    fb_backend_timeout, fbt_src = _env_or_conf("FALLBACK_BACKEND_TIMEOUT", c.get("fallback_backend_timeout"), 120, int)

    # -- Primary backend discovery
    env_backend_count = 0
    while os.getenv(f"BACKEND_{env_backend_count}_HOST"):
        env_backend_count += 1

    backends_from_conf = c.get("backends", [])

    if env_backend_count > 0:
        backend_source = "env"
        backends = []
        for i in range(env_backend_count):
            backend = BackendConfig(
                name=os.getenv(f"BACKEND_{i}_NAME", f"backend-{i}"),
                host=os.getenv(f"BACKEND_{i}_HOST"),
                port=int(os.getenv(f"BACKEND_{i}_PORT", 8890)),
                path=os.getenv(f"BACKEND_{i}_PATH", "/"),
            )
            backends.append(backend)
    elif backends_from_conf:
        backend_source = "conf.json"
        backends = [BackendConfig(**b) for b in backends_from_conf]
    else:
        raise ValueError(
            "No backends configured. Set BACKEND_0_HOST env var "
            "or add backends to conf.json."
        )

    # -- Fallback backend discovery
    env_fallback_count = 0
    while os.getenv(f"FALLBACK_{env_fallback_count}_HOST"):
        env_fallback_count += 1

    fallback_from_conf = c.get("fallback_backends", [])

    if env_fallback_count > 0:
        fallback_source = "env"
        fallback_backends = []
        for i in range(env_fallback_count):
            fb = BackendConfig(
                name=os.getenv(f"FALLBACK_{i}_NAME", f"fallback-{i}"),
                host=os.getenv(f"FALLBACK_{i}_HOST"),
                port=int(os.getenv(f"FALLBACK_{i}_PORT", 8890)),
                path=os.getenv(f"FALLBACK_{i}_PATH", "/"),
            )
            fallback_backends.append(fb)
    elif fallback_from_conf:
        fallback_source = "conf.json"
        fallback_backends = [BackendConfig(**b) for b in fallback_from_conf]
    else:
        fallback_source = "none"
        fallback_backends = []

    config = AppConfig(
        listen_port=listen_port,
        log_level=log_level,
        backends=backends,
        max_concurrent_per_backend=max_concurrent,
        max_queue_per_backend=max_queue,
        queue_timeout=queue_timeout,
        backend_timeout=backend_timeout,
        circuit_breaker_threshold=cb_threshold,
        circuit_breaker_recovery_time=cb_recovery,
        health_check_interval=hc_interval,
        health_check_timeout=hc_timeout,
        health_check_query=hc_query,
        fallback_backends=fallback_backends,
        fallback_max_concurrent_per_backend=fb_max_concurrent,
        fallback_max_queue_per_backend=fb_max_queue,
        fallback_queue_timeout=fb_queue_timeout,
        fallback_backend_timeout=fb_backend_timeout,
    )

    # -- Logging with source info
    logger.info(f"Configuration resolved ({len(config.backends)} primary backends from {backend_source}):")
    for b in config.backends:
        logger.info(f"  Primary '{b.name}': {b.url}")
    logger.info(f"  listen_port={config.listen_port} (from {lp_src})")
    logger.info(f"  log_level={config.log_level} (from {ll_src})")
    logger.info(f"  max_concurrent_per_backend={config.max_concurrent_per_backend} (from {mc_src})")
    logger.info(f"  max_queue_per_backend={config.max_queue_per_backend} (from {mq_src})")
    logger.info(f"  queue_timeout={config.queue_timeout}s (from {qt_src})")
    logger.info(f"  backend_timeout={config.backend_timeout}s (from {bt_src})")
    logger.info(f"  circuit_breaker_threshold={config.circuit_breaker_threshold} (from {cbt_src})")
    logger.info(f"  circuit_breaker_recovery_time={config.circuit_breaker_recovery_time}s (from {cbr_src})")
    logger.info(f"  health_check_interval={config.health_check_interval}s (from {hci_src})")
    logger.info(f"  health_check_timeout={config.health_check_timeout}s (from {hct_src})")
    logger.info(f"  health_check_query={config.health_check_query} (from {hcq_src})")

    if fallback_backends:
        logger.info(f"Fallback pool ({len(fallback_backends)} backends from {fallback_source}):")
        for b in fallback_backends:
            logger.info(f"  Fallback '{b.name}': {b.url}")
        logger.info(f"  fallback_max_concurrent={fb_max_concurrent} (from {fmc_src})")
        logger.info(f"  fallback_max_queue={fb_max_queue} (from {fmq_src})")
        logger.info(f"  fallback_queue_timeout={fb_queue_timeout}s (from {fqt_src})")
        logger.info(f"  fallback_backend_timeout={fb_backend_timeout}s (from {fbt_src})")
    else:
        logger.info("No fallback backends configured")

    return config