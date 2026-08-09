"""Small helpers for the counters shown on the status page."""

from __future__ import annotations

import time


class RateCounter:
    """Events per second over a sliding window.

    Two buckets are kept, the window just gone and the one filling now, so the
    reported rate is accurate whether it is read every second by the dashboard
    or once in a while by a script, and it decays to zero when traffic stops.
    """

    def __init__(self, window: float = 2.0):
        self.window = window
        self.total = 0
        self._current = 0
        self._previous = 0
        self._started = time.monotonic()

    def _roll(self, now: float) -> None:
        elapsed = now - self._started
        if elapsed < self.window:
            return
        self._previous = self._current if elapsed < 2 * self.window else 0
        self._current = 0
        self._started = now

    def add(self, n: int = 1) -> None:
        self._roll(time.monotonic())
        self.total += n
        self._current += n

    @property
    def rate(self) -> float:
        now = time.monotonic()
        self._roll(now)
        span = (now - self._started) + self.window
        return (self._previous + self._current) / span

    def reset(self) -> None:
        self.total = 0
        self._current = 0
        self._previous = 0
        self._started = time.monotonic()
