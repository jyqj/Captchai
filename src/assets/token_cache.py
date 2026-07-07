"""TTL cache for reusable captcha widget tokens.

Some widget tokens (e.g. short-lived reCAPTCHA/Turnstile responses) can be
reused within a narrow window *provided they are replayed from the same egress
IP and User-Agent they were minted with* — otherwise the target rejects them.
The cache key is therefore ``(sitekey, proxy_ip, user_agent)`` and every entry
carries a TTL slightly under the token's real lifetime.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Dict, Optional, Tuple

_Key = Tuple[str, str, str]


class TokenCache:
    """Async-safe LRU+TTL cache keyed on (sitekey, proxy_ip, user_agent)."""

    def __init__(self, ttl_seconds: int = 110, max_size: int = 1024) -> None:
        self._ttl = ttl_seconds
        self._max_size = max_size
        # key -> (token, expires_at). OrderedDict gives us LRU eviction.
        self._entries: "OrderedDict[_Key, Tuple[str, float]]" = OrderedDict()
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(sitekey: str, proxy_ip: Optional[str], user_agent: str) -> _Key:
        # A missing proxy IP (proxyless) is a distinct, stable bucket.
        return (sitekey, proxy_ip or "", user_agent)

    async def get(
        self, sitekey: str, proxy_ip: Optional[str], user_agent: str
    ) -> Optional[str]:
        """Return a live token for the key, or ``None`` if missing/expired."""
        key = self._key(sitekey, proxy_ip, user_agent)
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            token, expires_at = entry
            if expires_at <= time.monotonic():
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return token

    async def put(
        self, sitekey: str, proxy_ip: Optional[str], user_agent: str, token: str
    ) -> None:
        """Store ``token`` under the key with a fresh TTL, evicting LRU if full."""
        key = self._key(sitekey, proxy_ip, user_agent)
        expires_at = time.monotonic() + self._ttl
        async with self._lock:
            self._entries[key] = (token, expires_at)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_size:
                self._entries.popitem(last=False)

    async def purge_expired(self) -> int:
        """Drop all expired entries; return the number removed."""
        now = time.monotonic()
        async with self._lock:
            expired = [
                key for key, (_, exp) in self._entries.items() if exp <= now
            ]
            for key in expired:
                del self._entries[key]
            return len(expired)

    def __len__(self) -> int:
        return len(self._entries)
