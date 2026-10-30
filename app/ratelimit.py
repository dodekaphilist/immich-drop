"""Tiny in-memory failure limiter for login and invite-password attempts."""
import time
from typing import Dict, List


class RateLimiter:
    """Blocks a key after `limit` failures within `window` seconds."""

    _MAX_KEYS = 10_000

    def __init__(self, limit: int, window: float) -> None:
        self.limit = limit
        self.window = window
        self._hits: Dict[str, List[float]] = {}

    def _recent(self, key: str) -> List[float]:
        cutoff = time.monotonic() - self.window
        hits = [t for t in self._hits.get(key, []) if t > cutoff]
        if hits:
            self._hits[key] = hits
        else:
            self._hits.pop(key, None)
        return hits

    def blocked(self, key: str) -> bool:
        return len(self._recent(key)) >= self.limit

    def fail(self, key: str) -> None:
        if len(self._hits) >= self._MAX_KEYS:
            for stale in list(self._hits):
                self._recent(stale)
            if len(self._hits) >= self._MAX_KEYS:
                self._hits.clear()  # under attack with random keys: forget instead of growing
        self._hits.setdefault(key, []).append(time.monotonic())

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)
