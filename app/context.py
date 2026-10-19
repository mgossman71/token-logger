"""Process-wide shared runtime state.

The logger runs as a single uvicorn worker (async, event-loop based), so a
flat module holds the shared upstream ``httpx`` client and the JSONL store.
``app.main`` populates these during its lifespan; ``app.proxy`` reads them.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover
    import httpx
    from .config import Config
    from .storage import JsonlStore

config: Optional["Config"] = None
client: Optional["httpx.AsyncClient"] = None
store: Optional["JsonlStore"] = None


def reset() -> None:
    global config, client, store
    config = None
    client = None
    store = None

