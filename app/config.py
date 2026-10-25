"""Environment-driven configuration for the logger."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return raw if raw not in (None, "") else default


@dataclass(frozen=True)
class Config:
    # Identity
    backend_name: str
    # The Ollama server this instance proxies to.
    backend_url: str
    # Listen
    listen_host: str
    listen_port: int
    # Storage
    data_dir: str
    log_max_bytes: int
    log_backups: int
    # Timeouts
    connect_timeout: float
    read_timeout: float
    total_timeout: float
    # Concurrency
    max_connections: int
    max_keepalive_connections: int
    # Stat-extraction tail buffer
    tail_buffer_bytes: int
    # Pricing
    pricing_file: str
    # IANA timezone for day boundaries ("today", daily buckets) and displayed
    # times. The dashboard can override it per viewer.
    timezone: str = "UTC"

    @property
    def usage_path(self) -> str:
        return os.path.join(self.data_dir, "usage.jsonl")

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            backend_name=_str("BACKEND_NAME", "default"),
            backend_url=_str("OLLAMA_BACKEND_URL", "http://127.0.0.1:11434").rstrip("/"),
            listen_host=_str("LISTEN_HOST", "0.0.0.0"),
            listen_port=_int("LISTEN_PORT", 11435),
            data_dir=_str("DATA_DIR", "./data"),
            log_max_bytes=_int("LOG_MAX_BYTES", 100 * 1024 * 1024),
            log_backups=_int("LOG_BACKUPS", 5),
            connect_timeout=_float("CONNECT_TIMEOUT", 10.0),
            read_timeout=_float("READ_TIMEOUT", 1800.0),
            total_timeout=_float("TOTAL_TIMEOUT", 1800.0),
            max_connections=_int("MAX_CONNECTIONS", 200),
            max_keepalive_connections=_int("MAX_KEEPALIVE_CONNECTIONS", 60),
            tail_buffer_bytes=_int("TAIL_BUFFER_BYTES", 8 * 1024 * 1024),
            pricing_file=_str("PRICING_FILE", "pricing.json"),
            timezone=_str("TIMEZONE", "UTC"),
        )
