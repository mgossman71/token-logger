"""Application entrypoint: Starlette app factory + shared runtime wiring.

Run with uvicorn::

    uvicorn app.main:app --host 0.0.0.0 --port 11435

The app is fully asynchronous, single-worker, and forwards Ollama traffic
transparently while recording token usage. A small read-only usage dashboard is
served on the same port (``/`` and ``/dashboard``) and can be disabled with
``DASHBOARD_ENABLED=0``.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import List, Optional

import httpx
from starlette.applications import Starlette
from starlette.routing import Route

from . import context
from .config import Config
from .proxy import chat_handler, generate_handler, openai_handler, passthrough_handler
from .storage import JsonlStore


def _build_client(cfg: Config) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=cfg.backend_url,
        timeout=httpx.Timeout(
            connect=cfg.connect_timeout,
            read=cfg.read_timeout,
            write=30.0,
            pool=cfg.connect_timeout,
        ),
        limits=httpx.Limits(
            max_connections=cfg.max_connections,
            max_keepalive_connections=cfg.max_keepalive_connections,
        ),
        follow_redirects=False,
        http1=True,
    )


def _dashboard_enabled() -> bool:
    return os.environ.get("DASHBOARD_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _routes(cfg: Config) -> List[Route]:
    base = [
        Route("/api/chat", chat_handler, methods=["POST"]),
        Route("/api/generate", generate_handler, methods=["POST"]),
        # Ollama's OpenAI-compatible endpoints (e.g. Cline "OpenAI Compatible").
        Route("/v1/chat/completions", openai_handler, methods=["POST"]),
        Route("/v1/completions", openai_handler, methods=["POST"]),
        # Everything else (including /api/version, /api/tags, /api/show,
        # /api/pull, /api/embed, ...) is forwarded transparently.
        Route("/{path:path}", passthrough_handler, methods=["GET", "POST"]),
    ]
    if not _dashboard_enabled():
        return base
    # Dashboard routes are registered BEFORE the {path:path} catch-all so they
    # take precedence. None of them collide with Ollama's /api/* endpoints.
    from .web import dashboard_data, dashboard_index  # deferred import: only when
                                                     # the dashboard is enabled

    dashboard = [
        Route("/dashboard/data", dashboard_data, methods=["GET"]),
        Route("/dashboard", dashboard_index, methods=["GET"]),
        Route("/", dashboard_index, methods=["GET"]),
    ]
    return dashboard + base


def create_app(config: Optional[Config] = None) -> Starlette:
    cfg = config or Config.from_env()

    @asynccontextmanager
    async def lifespan(app: Starlette):
        context.config = cfg
        context.client = _build_client(cfg)
        context.store = JsonlStore(cfg.usage_path, cfg.log_max_bytes, cfg.log_backups)
        try:
            yield
        finally:
            if context.client is not None:
                try:
                    await context.client.aclose()
                except Exception:
                    pass
            context.reset()

    app = Starlette(routes=_routes(cfg), lifespan=lifespan, debug=False)
    app.state.config = cfg
    return app


# Imported as `app.main:app` by uvicorn.
app = create_app()
