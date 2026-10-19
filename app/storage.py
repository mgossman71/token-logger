"""Append-only JSONL storage with size-based rotation.

Design goals:
  * One line per completed inference request.
  * Concurrent-safe appends (single process => asyncio.Lock serialises appends
    and rotation; blocking I/O is pushed to a thread so the event loop never
    stalls).
  * Rotation never corrupts records: we atomically ``os.replace`` the active
    file to ``.1`` and shift older backups, only then start a fresh file.
  * A logging failure must NEVER take down an inference request; callers wrap
    ``append`` in try/except, and this module is defensive by default.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, Optional


class JsonlStore:
    def __init__(self, path: str, max_bytes: int = 0, backups: int = 5):
        self.path = path
        self.max_bytes = int(max_bytes) if max_bytes else 0
        self.backups = max(0, int(backups))
        self._lock = asyncio.Lock()
        self._written = 0

    # -- public -------------------------------------------------------------
    async def append(self, record: Dict[str, Any]) -> None:
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
        data = line.encode("utf-8")
        loop = asyncio.get_running_loop()
        async with self._lock:
            await loop.run_in_executor(None, self._append_blocking, data)
            if self.max_bytes:
                await loop.run_in_executor(None, self._maybe_rotate)
            self._written += 1

    def __len__(self) -> int:  # convenience for tests
        return self._written

    # -- internals ----------------------------------------------------------
    def _append_blocking(self, data: bytes) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self.path, "ab") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())

    def _maybe_rotate(self) -> None:
        try:
            size = os.path.getsize(self.path)
        except FileNotFoundError:
            return
        if size < self.max_bytes:
            return
        self._rotate_blocking()

    def _rotate_blocking(self) -> None:
        if self.backups <= 0:
            # No backups allowed: truncate the active file instead of unbounded growth.
            with open(self.path, "wb") as fh:
                pass
            return
        # Drop the oldest backup, if it exists.
        oldest = f"{self.path}.{self.backups}"
        if os.path.exists(oldest):
            os.remove(oldest)
        # Shift .i -> .i+1, highest first.
        for i in range(self.backups - 1, 0, -1):
            src = f"{self.path}.{i}"
            dst = f"{self.path}.{i + 1}"
            if os.path.exists(src):
                os.replace(src, dst)
        # Active -> .1
        if os.path.exists(self.path):
            os.replace(self.path, f"{self.path}.1")
        # Ensure a fresh active log exists (standard logrotate behaviour).
        with open(self.path, "ab"):
            pass
