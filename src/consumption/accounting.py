"""Rolling success-rate accounting used for routing/selection decisions."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, Deque, Dict, Tuple

_Key = Tuple[str, Any, Any]


class SuccessAccounting:
    """Rolling per-(sitekey[, proxy_kind, model]) success stats to drive routing/selection."""

    def __init__(self, window: int = 100) -> None:
        self._window = window
        self._outcomes: Dict[_Key, Deque[bool]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(sitekey: str, proxy_kind: str | None, model: str | None) -> _Key:
        return (sitekey, proxy_kind, model)

    async def record(
        self,
        sitekey: str,
        outcome: str,
        *,
        proxy_kind: str | None = None,
        model: str | None = None,
    ) -> None:
        success = outcome == "ready"
        async with self._lock:
            key = self._key(sitekey, proxy_kind, model)
            bucket = self._outcomes.get(key)
            if bucket is None:
                bucket = deque(maxlen=self._window)
                self._outcomes[key] = bucket
            bucket.append(success)

    async def success_rate(
        self,
        sitekey: str,
        *,
        proxy_kind: str | None = None,
        model: str | None = None,
    ) -> float:
        async with self._lock:
            bucket = self._outcomes.get(self._key(sitekey, proxy_kind, model))
            if not bucket:
                return 1.0  # optimistic default when no data yet
            return sum(bucket) / len(bucket)

    async def stats(self, sitekey: str) -> dict[str, Any]:
        """Aggregate stats across all (proxy_kind, model) buckets for a sitekey."""
        async with self._lock:
            attempts = 0
            successes = 0
            for (key_sitekey, _proxy, _model), bucket in self._outcomes.items():
                if key_sitekey != sitekey:
                    continue
                attempts += len(bucket)
                successes += sum(bucket)
        rate = (successes / attempts) if attempts else 1.0
        return {"attempts": attempts, "successes": successes, "rate": rate}
