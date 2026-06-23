"""Thread-safe token-bucket rate limiter shared by all SEC API callers.

SEC EDGAR guidelines: no more than 10 requests/second from a single IP.
We use 8 req/s to leave a safety margin. All SECAPIClient and SECURLDetector
instances share one bucket, so the limit holds even with parallel workers.
"""

import threading
import time


class RateLimiter:
    """Token-bucket rate limiter.  Thread-safe; all callers share one bucket."""

    def __init__(self, rate: float = 8.0) -> None:
        self._rate = rate          # tokens (requests) refilled per second
        self._tokens = float(rate) # start full
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until one token is available, then consume it."""
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self._rate,
                    self._tokens + (now - self._last) * self._rate,
                )
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                sleep_for = (1.0 - self._tokens) / self._rate
            time.sleep(sleep_for)


# Global singleton – imported by sec_client.py and sec_url_detector.py
_global_rate_limiter = RateLimiter(rate=8.0)
