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

    @field_validator("backends")
    @classmethod
    def check_backends_not_empty(cls, v):
        if not v:
            raise ValueError("At least one backend must be configured")
        return v

    @field_validator("max_concurrent_per_backend", "max_queue_per_backend")
    @classmethod
    def check_positive(cls, v):
        if v < 1:
            raise ValueError("Value must be at least 1")
        return v

    @field_validator("queue_timeout", "backend_timeout")
    @classmethod
    def check_timeout_positive(cls, v):
        if v < 1:
            raise ValueError("Timeout must be at least 1 second")
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
      - If ANY BACKEND_N_HOST env var is found, backends come ENTIRELY from env vars.
        conf.json backends are ignored to prevent ghost backends.
      - If NO BACKEND_N_HOST env vars exist, backends come from conf.json.
    """

    # ── Load conf.json as base ──────────────────────────────────────────
    c = {}
    try:
        with open(config_path) as f:
            c = json.load(f)
        logger.info(f"Loaded base config from {config_path}")
    except FileNotFoundError:
        logger.warning(f"{config_path} not found — using env vars and defaults only")
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in {config_path}: {e} — using env vars and defaults only")

    # ── Global settings (env > conf.json > default) ─────────────────────
    listen_port, lp_src = _env_or_conf("LISTEN_PORT", c.get("listen_port"), 8080, int)
    log_level, ll_src = _env_or_conf("LOG_LEVEL", c.get("log_level"), "info")
    max_concurrent, mc_src = _env_or_conf("MAX_CONCURRENT_PER_BACKEND", c.get("max_concurrent_per_backend"), 10, int)
    max_queue, mq_src = _env_or_conf("MAX_QUEUE_PER_BACKEND", c.get("max_queue_per_backend"), 50, int)
    queue_timeout, qt_src = _env_or_conf("QUEUE_TIMEOUT", c.get("queue_timeout"), 120, int)
    backend_timeout, bt_src = _env_or_conf("BACKEND_TIMEOUT", c.get("backend_timeout"), 900, int)

    # ── Backend discovery ───────────────────────────────────────────────
    # Count how many BACKEND_N_HOST env vars exist
    env_backend_count = 0
    while os.getenv(f"BACKEND_{env_backend_count}_HOST"):
        env_backend_count += 1

    backends_from_conf = c.get("backends", [])

    if env_backend_count > 0:
        # ENV VARS WIN: backends come entirely from env vars.
        # conf.json backends are NOT mixed in — this prevents ghost backends.
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
        # No env vars for backends — use conf.json as-is
        backend_source = "conf.json"
        backends = [BackendConfig(**b) for b in backends_from_conf]
    else:
        raise ValueError(
            "No backends configured. Set BACKEND_0_HOST env var "
            "or add backends to conf.json."
        )

    config = AppConfig(
        listen_port=listen_port,
        log_level=log_level,
        backends=backends,
        max_concurrent_per_backend=max_concurrent,
        max_queue_per_backend=max_queue,
        queue_timeout=queue_timeout,
        backend_timeout=backend_timeout,
    )

    # ── Logging with source info ────────────────────────────────────────
    logger.info(f"Configuration resolved ({len(config.backends)} backends from {backend_source}):")
    for b in config.backends:
        logger.info(f"  Backend '{b.name}': {b.url}")
    logger.info(f"  listen_port={config.listen_port} (from {lp_src})")
    logger.info(f"  log_level={config.log_level} (from {ll_src})")
    logger.info(f"  max_concurrent_per_backend={config.max_concurrent_per_backend} (from {mc_src})")
    logger.info(f"  max_queue_per_backend={config.max_queue_per_backend} (from {mq_src})")
    logger.info(f"  queue_timeout={config.queue_timeout}s (from {qt_src})")
    logger.info(f"  backend_timeout={config.backend_timeout}s (from {bt_src})")

    return config