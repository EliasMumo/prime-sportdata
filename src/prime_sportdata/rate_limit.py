"""Per-host minimum-interval limiter (SPEC: polite crawling, per-host >= 1 s).

``acquire(host)`` blocks until at least ``interval`` seconds have passed since
the previous acquire for the *same* host; different hosts never block each
other. Per-host locks mean concurrent callers on one host are serialized
without serializing the whole process.

Clock and sleep are injectable so tests are deterministic and never actually
sleep.
"""

import threading
import time
from collections.abc import Callable


class RateLimiter:
    """Serializes consecutive hits to one host to >= ``interval`` apart."""

    def __init__(
        self,
        interval: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._interval = interval
        self._clock = clock
        self._sleep = sleep
        self._last_hit: dict[str, float] = {}
        self._host_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def acquire(self, host: str) -> None:
        """Block until this call is >= ``interval`` after the previous one for
        ``host``; returns immediately if none was recorded or it was long
        enough ago. Updates the host's last-hit stamp on success."""
        with self._locks_guard:
            lock = self._host_locks.get(host)
            if lock is None:
                lock = self._host_locks.setdefault(host, threading.Lock())
        with lock:
            now = self._clock()
            last = self._last_hit.get(host)
            if last is not None:
                wait = self._interval - (now - last)
                if wait > 0:
                    self._sleep(wait)
                    now = self._clock()
            self._last_hit[host] = now
