"""A tiny fan-out bus so every open UI tab sees the same stream of events."""

from __future__ import annotations

import asyncio
import itertools
import time
from collections import deque
from typing import Any

MAX_HISTORY = 2000


class EventBus:
    """Broadcasts events to all subscribers and keeps a replayable history."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._history: deque[dict[str, Any]] = deque(maxlen=MAX_HISTORY)
        self._seq = itertools.count(1)

    def publish(self, kind: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        event = {
            "seq": next(self._seq),
            "kind": kind,
            "ts": time.time(),
            **(payload or {}),
        }
        if kind != "status":
            self._history.append(event)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A tab that cannot keep up loses the oldest event, not the connection.
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
        return event

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def history(self, kinds: set[str] | None = None, limit: int = MAX_HISTORY) -> list[dict[str, Any]]:
        items = [e for e in self._history if kinds is None or e["kind"] in kinds]
        return items[-limit:]

    def clear(self) -> None:
        self._history.clear()
