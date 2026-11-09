"""In-process event bus. Events are persisted (the activity log) and fanned out to
Server-Sent Events subscribers so the review screen updates live."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Optional

from .db import Database, EventRow, utcnow


CLOSE = "__close__"  # sentinel event: the stream should end


@dataclass
class Subscriber:
    queue: "asyncio.Queue[dict]"
    application_id: Optional[str] = None
    loop: Optional[asyncio.AbstractEventLoop] = None


@dataclass
class EventBus:
    db: Database
    subscribers: list[Subscriber] = field(default_factory=list)

    def subscribe(self, application_id: Optional[str] = None) -> Subscriber:
        sub = Subscriber(queue=asyncio.Queue(maxsize=1000), application_id=application_id, loop=asyncio.get_running_loop())
        self.subscribers.append(sub)
        return sub

    def close_all(self) -> None:
        """End every live stream (server shutting down). Safe to call from a signal
        handler: it only schedules work on each subscriber's loop."""
        for sub in list(self.subscribers):
            if sub.loop is None:
                continue
            try:
                sub.loop.call_soon_threadsafe(sub.queue.put_nowait, {"type": CLOSE})
            except RuntimeError:  # loop already closed
                pass

    def unsubscribe(self, sub: Subscriber) -> None:
        if sub in self.subscribers:
            self.subscribers.remove(sub)

    def publish(
        self,
        type: str,
        message: str = "",
        application_id: Optional[str] = None,
        data: Optional[dict[str, Any]] = None,
        persist: bool = True,
    ) -> dict:
        event: dict[str, Any] = {
            "type": type,
            "message": message,
            "application_id": application_id,
            "data": data or {},
            "ts": utcnow().isoformat(),
        }
        if persist:
            with self.db.session() as s:
                row = EventRow(application_id=application_id, type=type, message=message, data=data or {})
                s.add(row)
                s.flush()
                event["id"] = row.id
        for sub in list(self.subscribers):
            if sub.application_id and sub.application_id != application_id:
                continue
            self._deliver(sub, event)
        return event

    @staticmethod
    def _deliver(sub: Subscriber, event: dict) -> None:
        def put() -> None:
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if sub.loop is not None and running is not sub.loop:
            sub.loop.call_soon_threadsafe(put)
        else:
            put()
