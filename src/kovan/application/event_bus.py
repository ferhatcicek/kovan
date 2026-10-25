"""Basit asyncio yayın/abone olay yolu. Thread'lerden güvenle yayın yapılabilir."""
from __future__ import annotations

import asyncio


class Subscriber:
    def __init__(self, maxsize: int = 20000):
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.log_projects: set[int] = set()
        self.dropped = 0

    def wants(self, event: dict) -> bool:
        if event["type"] == "log":
            return event["project_id"] in self.log_projects
        return True


class EventBus:
    def __init__(self) -> None:
        self._subs: set[Subscriber] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @property
    def has_subscribers(self) -> bool:
        return bool(self._subs)

    def subscribe(self) -> Subscriber:
        sub = Subscriber()
        self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subs.discard(sub)

    def publish(self, event: dict) -> None:
        """Herhangi bir thread'den çağrılabilir."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._dispatch(event)
        else:
            loop.call_soon_threadsafe(self._dispatch, event)

    def _dispatch(self, event: dict) -> None:
        for sub in list(self._subs):
            if sub.wants(event):
                try:
                    sub.queue.put_nowait(event)
                except asyncio.QueueFull:
                    sub.dropped += 1  # geri basınç: yavaş istemci log kaybeder, panel şişmez
