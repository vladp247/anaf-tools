"""Async rate limiter — ANAF allows 1 req/sec; we use 2s gap for safety."""
import asyncio
import time
from backend.utils.logger import get_logger

log = get_logger(__name__)


class AsyncRateLimiter:
    def __init__(self, delay: float = 2.0):
        self.delay = delay
        self._lock = asyncio.Lock()
        self._last: float = 0.0

    async def acquire(self):
        async with self._lock:
            elapsed = time.monotonic() - self._last
            wait = self.delay - elapsed
            if wait > 0:
                log.debug("Rate limit wait: %.2fs", wait)
                await asyncio.sleep(wait)
            self._last = time.monotonic()


_limiter: AsyncRateLimiter | None = None


def get_bulk_limiter() -> AsyncRateLimiter:
    global _limiter
    if _limiter is None:
        from config import Config
        _limiter = AsyncRateLimiter(Config.BULK_RATE_DELAY)
    return _limiter
