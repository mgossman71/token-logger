"""Application entrypoint: Starlette app factory + shared runtime wiring.

Run with uvicorn::

    uvicorn app.main:app --host 0.0.0.0 --port 11435

The app is fully asynchronous, single-worker, and forwards Ollama traffic
transparently while recording token usage.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional

import httpx
from starlette.applications import Starlette
from starlette.routing import Route

from . import context
from .config import Config
from .proxy import chat_handler, generate_handler, passthrough_handler
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

    routes = [
        Route("/api/chat", chat_handler, methods=["POST"]),
        Route("/api/generate", generate_handler, methods=["POST"]),
        # Everything else (including /api/version, /api/tags, /api/show,
        # /api/pull, /api/embed, ...) is forwarded transparently.
        Route("/{path:path}", passthrough_handler, methods=["GET", "POST"]),
    ]
    app = Starlette(routes=routes, lifespan=lifespan, debug=False)
    app.state.config = cfg
    return app


# Imported as `app.main:app` by uvicorn.
app = create_app()
