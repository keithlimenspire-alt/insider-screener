"""Thread-safe token-bucket rate limiter shared by all SEC fetchers."""
import threading
import time


class RateLimiter:
    def __init__(self, max_per_sec: float):
        self._interval = 1.0 / max_per_sec
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def acquire(self) -> None:
        """Block until a request slot is available."""
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._interval
        wait = slot - now
        if wait > 0:
            time.sleep(wait)
