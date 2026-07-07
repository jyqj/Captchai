"""Proxy inventory with health tracking and per-sitekey success ranking.

A ``ProxyPool`` holds ``ProxyAsset`` objects and hands out the healthiest proxy
on ``checkout``, skipping any that are burned or cooling down. Health is tracked
via rolling success/fail counters; a run of consecutive failures parks a proxy
in ``cooldown`` for a configurable window. Per-sitekey success is tracked
separately so a proxy that reliably solves a given sitekey is preferred for it.

An empty pool is a valid, first-class state: ``checkout`` returns ``None`` and
callers fall back to the proxy supplied on the task itself (proxyless op).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ProxyAsset:
    id: str
    server: str
    username: Optional[str] = None
    password: Optional[str] = None
    kind: str = "datacenter"
    state: str = "healthy"
    success_count: int = 0
    fail_count: int = 0
    consecutive_fails: int = 0
    last_used_at: float = 0.0
    cooldown_until: float = 0.0
    cost_per_gb: float = 0.0
    bytes_used: int = 0
    sticky_session_id: Optional[str] = None
    # Per-sitekey counters: sitekey -> [success, fail]. Not part of the public
    # constructor contract but used by selection ranking and the snapshot.
    sitekey_stats: Dict[str, List[int]] = field(default_factory=dict)

    def playwright_proxy(self) -> Optional[Dict[str, str]]:
        """Return a Playwright proxy dict, or ``None`` if no server configured."""
        if not self.server:
            return None
        proxy: Dict[str, str] = {"server": self.server}
        if self.username:
            proxy["username"] = self.username
        if self.password:
            proxy["password"] = self.password
        return proxy

    def success_rate(self) -> float:
        """Rolling success rate; unused proxies get an optimistic 1.0."""
        total = self.success_count + self.fail_count
        if total == 0:
            return 1.0
        return self.success_count / total

    def sitekey_rate(self, sitekey: Optional[str]) -> Optional[float]:
        """Success rate for a specific sitekey, or ``None`` if never tried."""
        if sitekey is None:
            return None
        stats = self.sitekey_stats.get(sitekey)
        if not stats:
            return None
        total = stats[0] + stats[1]
        if total == 0:
            return None
        return stats[0] / total


def proxy_from_params(params: Dict[str, Any]) -> Optional[ProxyAsset]:
    """Build a ``ProxyAsset`` from YesCaptcha-style task proxy fields.

    Accepts either the single-string ``proxy`` form
    (``"scheme://user:pass@host:port"``) or the split
    ``proxyType``/``proxyAddress``/``proxyPort``/``proxyLogin``/``proxyPassword``
    fields. Credentials are kept out of ``server`` (Playwright wants them
    separate). Returns ``None`` when no usable proxy is described.
    """
    single = params.get("proxy")
    if single:
        return _from_single_string(str(single))

    address = params.get("proxyAddress")
    port = params.get("proxyPort")
    if not address or not port:
        return None

    scheme = (params.get("proxyType") or "http").lower()
    if scheme not in {"http", "https", "socks4", "socks5", "socks"}:
        scheme = "http"
    server = f"{scheme}://{address}:{port}"

    username = params.get("proxyLogin")
    password = params.get("proxyPassword")
    return ProxyAsset(
        id=str(uuid.uuid4()),
        server=server,
        username=str(username) if username else None,
        password=str(password) if password else None,
    )


def _from_single_string(value: str) -> Optional[ProxyAsset]:
    raw = value.strip()
    if not raw:
        return None
    if "://" in raw:
        scheme, _, rest = raw.partition("://")
    else:
        scheme, rest = "http", raw

    creds = None
    hostport = rest
    if "@" in rest:
        creds, _, hostport = rest.rpartition("@")

    username: Optional[str] = None
    password: Optional[str] = None
    if creds is not None:
        username, _, password = creds.partition(":")
        username = username or None
        password = password or None

    if not hostport:
        return None
    return ProxyAsset(
        id=str(uuid.uuid4()),
        server=f"{scheme}://{hostport}",
        username=username,
        password=password,
    )


class ProxyPool:
    """Concurrency-safe collection of proxies with health-aware selection."""

    def __init__(
        self, *, cooldown_seconds: int = 120, max_consecutive_fails: int = 3
    ) -> None:
        self._cooldown_seconds = cooldown_seconds
        self._max_consecutive_fails = max_consecutive_fails
        self._proxies: Dict[str, ProxyAsset] = {}
        # Created lazily so the pool can be constructed outside a running event
        # loop (on Python 3.9 asyncio.Lock() binds to the loop at construction).
        self._lock: Optional[asyncio.Lock] = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def add(self, proxy: ProxyAsset) -> None:
        self._proxies[proxy.id] = proxy

    def _is_available(self, proxy: ProxyAsset, now: float) -> bool:
        if proxy.state == "burned":
            return False
        if proxy.state == "cooldown":
            if proxy.cooldown_until > now:
                return False
            # Cooldown elapsed: rehabilitate lazily so it can be picked again.
            proxy.state = "healthy"
            proxy.consecutive_fails = 0
        return True

    async def checkout(
        self, *, kind: Optional[str] = None, sitekey: Optional[str] = None
    ) -> Optional[ProxyAsset]:
        """Return the best available proxy, or ``None`` if none can serve.

        Burned and cooling proxies are skipped. Candidates are ranked by
        per-sitekey success rate (when known), then overall rolling success
        rate, then least-recently-used as a tie-break to spread load.
        """
        async with self._get_lock():
            now = time.monotonic()
            candidates = [
                p
                for p in self._proxies.values()
                if self._is_available(p, now)
                and (kind is None or p.kind == kind)
            ]
            if not candidates:
                return None

            def rank(p: ProxyAsset):
                sk = p.sitekey_rate(sitekey)
                # Unknown sitekey history sorts just below a perfect record but
                # above any proven-bad one, so we still explore fresh proxies.
                sk_key = sk if sk is not None else 0.5
                return (sk_key, p.success_rate(), -p.last_used_at)

            best = max(candidates, key=rank)
            best.last_used_at = now
            best.state = "probing" if best.success_count == 0 else best.state
            return best

    async def report(
        self, proxy_id: str, *, success: bool, bytes_used: int = 0
    ) -> None:
        """Record the outcome of a solve attempt on ``proxy_id``.

        On success the consecutive-fail streak resets and the proxy returns to
        ``healthy``. After ``max_consecutive_fails`` failures in a row the proxy
        is parked in ``cooldown`` until ``now + cooldown_seconds``.
        """
        async with self._get_lock():
            proxy = self._proxies.get(proxy_id)
            if proxy is None:
                return
            proxy.bytes_used += max(0, bytes_used)
            if success:
                proxy.success_count += 1
                proxy.consecutive_fails = 0
                if proxy.state in {"cooldown", "probing"}:
                    proxy.state = "healthy"
                elif proxy.state != "burned":
                    proxy.state = "healthy"
            else:
                proxy.fail_count += 1
                proxy.consecutive_fails += 1
                if proxy.consecutive_fails >= self._max_consecutive_fails:
                    proxy.state = "cooldown"
                    proxy.cooldown_until = (
                        time.monotonic() + self._cooldown_seconds
                    )

    async def report_sitekey(
        self, proxy_id: str, sitekey: str, *, success: bool
    ) -> None:
        """Record a per-sitekey outcome used only for selection ranking."""
        async with self._get_lock():
            proxy = self._proxies.get(proxy_id)
            if proxy is None:
                return
            stats = proxy.sitekey_stats.setdefault(sitekey, [0, 0])
            if success:
                stats[0] += 1
            else:
                stats[1] += 1

    def snapshot(self) -> List[Dict[str, Any]]:
        """Serialisable view of the pool for an admin endpoint."""
        now = time.monotonic()
        result: List[Dict[str, Any]] = []
        for p in self._proxies.values():
            result.append(
                {
                    "id": p.id,
                    "server": p.server,
                    "kind": p.kind,
                    "state": p.state,
                    "success_count": p.success_count,
                    "fail_count": p.fail_count,
                    "consecutive_fails": p.consecutive_fails,
                    "success_rate": round(p.success_rate(), 4),
                    "cooldown_remaining": max(0.0, p.cooldown_until - now),
                    "cost_per_gb": p.cost_per_gb,
                    "bytes_used": p.bytes_used,
                    "sticky_session_id": p.sticky_session_id,
                    "sitekeys": {
                        sk: {"success": s[0], "fail": s[1]}
                        for sk, s in p.sitekey_stats.items()
                    },
                }
            )
        return result
