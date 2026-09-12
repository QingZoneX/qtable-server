from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator, Dict, Mapping


class NotificationBroker:
    """In-process realtime fan-out mirroring the existing table/board brokers.

    Persisted notifications remain the source of truth. Broker events contain
    identifiers only; clients reconnect by querying persisted unread/list data.
    """

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()

    async def publish(self, payload: Mapping[str, Any]) -> None:
        event = dict(payload)
        async with self._lock:
            for queue in list(self._subscribers):
                queue.put_nowait(event)

    async def subscribe(self) -> AsyncGenerator[Dict[str, Any], None]:  # type: ignore[type-arg]
        queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
        async with self._lock:
            self._subscribers.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            async with self._lock:
                self._subscribers.discard(queue)


notification_broker = NotificationBroker()
