"""Month-to-date spend for clients that show it all the time (the spend chip, the tray): `usage.changed`.

Every ledger row and every budget change pokes the watcher. It waits for a quiet second (a trailing
debounce, bounded so a steady stream of paid utterances still updates within a few seconds), then publishes
the budget state only if something a client shows changed: the spend rounded to $0.0001, the budget and its
thresholds, a free allowance, or the month itself (the first row of a new month). Kokoro reading writes a
$0 row per utterance; none of those reach a client.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from open_speech.api.schemas import BudgetState
from open_speech.api.ws_schema import UsageChanged
from open_speech.service.events import EventBus

log = logging.getLogger(__name__)

SPEND_STEP = Decimal("0.0001")


class UsageWatcher:
    def __init__(
        self,
        state: Callable[[], BudgetState],
        month: Callable[[], str],
        events: EventBus,
        *,
        delay_s: float = 1.0,
        max_wait_s: float = 3.0,
    ) -> None:
        self._state = state
        self._month = month
        self._events = events
        self.delay_s = delay_s
        self.max_wait_s = max_wait_s
        self._loop: asyncio.AbstractEventLoop | None = None
        self._handle: asyncio.TimerHandle | None = None
        self._first: float | None = None  # loop time of the oldest poke not yet flushed
        self._last: tuple[Any, ...] | None = None  # what was last published (or current at start)

    # ------------------------------------------------------------------ lifecycle

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Begin publishing on `loop`. What is true now counts as already known to clients (they read
        `GET /usage/budget` on connect), so the first message is the first real change."""
        self._loop = loop
        try:
            self._last = self._fingerprint(self._state())
        except Exception:  # noqa: BLE001
            log.exception("couldn't read the budget state")

    def stop(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
        self._handle = None
        self._first = None
        self._loop = None

    # ------------------------------------------------------------------ input

    def poke(self, *_: Any) -> None:
        """Something that may change the spend or the budget happened. Thread-safe (ledger rows are written
        from synthesis threads too); accepts and ignores listener arguments."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._arm()
        else:
            loop.call_soon_threadsafe(self._arm)

    def _arm(self) -> None:
        loop = self._loop
        if loop is None:
            return
        now = loop.time()
        if self._first is None:
            self._first = now
        due = min(now + self.delay_s, self._first + self.max_wait_s)
        if self._handle is not None:
            self._handle.cancel()
        self._handle = loop.call_later(max(0.0, due - now), self._flush)

    # ------------------------------------------------------------------ output

    def _fingerprint(self, b: BudgetState) -> tuple[Any, ...]:
        return (
            self._month(),
            Decimal(b.spent_usd).quantize(SPEND_STEP),
            Decimal(b.monthly_usd),
            b.warn_at,
            b.hard_stop,
            tuple((f.provider, f.unit, f.used, f.limit, f.resets_on) for f in b.free_tiers),
        )

    def _flush(self) -> None:
        self._handle = None
        self._first = None
        try:
            state = self._state()
            key = self._fingerprint(state)
        except Exception:  # noqa: BLE001 — never let a budget read break the event loop
            log.exception("couldn't read the budget state")
            return
        if key == self._last:
            return
        self._last = key
        self._events.publish(UsageChanged(budget=state))
