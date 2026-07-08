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
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Protocol, Tuple, Union, runtime_checkable


def _normalize_kinds(
    kind: Optional[Union[str, Iterable[str]]],
) -> Optional[frozenset]:
    """Normalize a ``kind`` argument into a ``frozenset`` of accepted kinds.

    Accepts:
      * ``None`` → no kind filter (returns ``None``).
      * a single ``str`` → a one-element frozenset (backward compatible).
      * a list / tuple / set / frozenset of str → a frozenset of those kinds.

    Empty collections normalize to ``None`` (no filter) so callers that build
    the kinds list dynamically don't accidentally exclude every proxy when the
    list happens to be empty.
    """
    if kind is None:
        return None
    if isinstance(kind, str):
        return frozenset({kind})
    kinds = frozenset(str(k) for k in kind)
    return kinds or None


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
    # Exit-IP geo metadata (WP3). When ``country`` is supplied but
    # ``timezone`` / ``locale`` are not, they are derived from
    # ``_COUNTRY_GEO`` below. All three stay ``None`` for proxies with no
    # geo annotation — the fingerprint then draws a random coherent identity
    # (current behavior, no regression).
    country: Optional[str] = None
    timezone: Optional[str] = None
    locale: Optional[str] = None
    # Per-sitekey counters: sitekey -> [success, fail]. Not part of the public
    # constructor contract but used by selection ranking and the snapshot.
    # ``sitekey_stats`` is the *token-obtained* bucket — written by the solver
    # after every pool solve with ``success=solved`` (did we get a token?).
    sitekey_stats: Dict[str, List[int]] = field(default_factory=dict)
    # ``real_sitekey_stats`` is the *real-outcome* bucket — written by the
    # /reportCorrect // /reportIncorrect endpoints with ``success=correct``
    # (was the token actually accepted downstream?). ``ProxyPool.checkout``
    # ranks by the real bucket when it has data and falls back to the
    # token-obtained bucket otherwise, so client feedback wins once reported
    # but a fresh proxy is still explored before any data lands.
    real_sitekey_stats: Dict[str, List[int]] = field(default_factory=dict)

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
        """Success rate for a specific sitekey, or ``None`` if never tried.

        This is the *token-obtained* rate (did we get a token?), written by
        ``ProxyPool.report_sitekey`` from the solver's per-solve outcome.
        Selection ranking prefers ``real_sitekey_rate`` when available.
        """
        if sitekey is None:
            return None
        stats = self.sitekey_stats.get(sitekey)
        if not stats:
            return None
        total = stats[0] + stats[1]
        if total == 0:
            return None
        return stats[0] / total

    def real_sitekey_rate(self, sitekey: Optional[str]) -> Optional[float]:
        """Real-outcome success rate for a sitekey, or ``None`` if unreported.

        Mirrors ``sitekey_rate`` but over ``real_sitekey_stats`` — the bucket
        fed by ``ProxyPool.report_sitekey_real`` from the
        /reportCorrect // /reportIncorrect endpoints. Returns ``None`` when no
        client has reported yet so the caller can fall back to the
        token-obtained rate.
        """
        if sitekey is None:
            return None
        stats = self.real_sitekey_stats.get(sitekey)
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

    The single-string form may carry pipe-separated geo / kind metadata
    suffixes — e.g. ``"http://user:pass@host:port|country=DE|kind=residential"``.
    Unknown keys are ignored. When ``country`` is given without an explicit
    ``timezone`` / ``locale``, those are derived from the built-in
    ``_COUNTRY_GEO`` table so a German exit IP presents Europe/Berlin + de-DE
    to the page rather than en-US/New_York.
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
    asset = ProxyAsset(
        id=str(uuid.uuid4()),
        server=server,
        username=str(username) if username else None,
        password=str(password) if password else None,
    )
    # Split-field form doesn't carry the |country=DE suffix; callers can still
    # pass ``country``/``timezone``/``locale``/``kind`` as separate params.
    _apply_geo_metadata(
        asset,
        country=params.get("country"),
        timezone=params.get("timezone"),
        locale=params.get("locale"),
        kind=params.get("kind"),
    )
    return asset


# Built-in country → (timezone, locale) table for exit-IP geo alignment.
# Covers the common residential-proxy exit countries; an unknown country
# leaves timezone/locale as ``None`` so the fingerprint falls back to a
# random coherent identity (no regression).
_COUNTRY_GEO: Dict[str, Tuple[str, str]] = {
    "US": ("America/New_York", "en-US"),
    "DE": ("Europe/Berlin", "de-DE"),
    "GB": ("Europe/London", "en-GB"),
    "FR": ("Europe/Paris", "fr-FR"),
    "ES": ("Europe/Madrid", "es-ES"),
    "RU": ("Europe/Moscow", "ru-RU"),
    "BR": ("America/Sao_Paulo", "pt-BR"),
    "JP": ("Asia/Tokyo", "ja-JP"),
    "IN": ("Asia/Kolkata", "hi-IN"),
    "CA": ("America/Toronto", "en-CA"),
}


def _apply_geo_metadata(
    asset: ProxyAsset,
    *,
    country: Optional[str],
    timezone: Optional[str],
    locale: Optional[str],
    kind: Optional[str],
) -> None:
    """Mutate ``asset`` in place to apply parsed geo / kind metadata.

    Unknown / empty values are ignored. When ``country`` is given but
    ``timezone`` or ``locale`` are not, they are derived from ``_COUNTRY_GEO``.
    ``kind`` is normalised to one of {"datacenter","residential","mobile"} —
    anything else falls back to ``"datacenter"`` (the ProxyAsset default).
    """
    if country:
        asset.country = str(country).upper()
    if timezone:
        asset.timezone = str(timezone)
    if locale:
        asset.locale = str(locale)
    if kind:
        norm = str(kind).lower()
        if norm in {"datacenter", "residential", "mobile"}:
            asset.kind = norm
    # Derive missing timezone / locale from country when possible.
    if asset.country and (not asset.timezone or not asset.locale):
        derived = _COUNTRY_GEO.get(asset.country)
        if derived is not None:
            tz, loc = derived
            if not asset.timezone:
                asset.timezone = tz
            if not asset.locale:
                asset.locale = loc


def _parse_metadata_suffix(hostport: str) -> Tuple[str, Dict[str, str]]:
    """Split a ``host:port|key=value|key=value`` string into (hostport, meta).

    The geo / kind metadata is appended after the proxy URL with ``|`` as the
    separator (e.g. ``"1.2.3.4:8080|country=DE|kind=residential"``). Unknown
    keys are collected but ignored by the caller. Returns the bare hostport
    and the metadata dict. Pass the *post-credential* hostport (not the full
    proxy URL) so a password containing ``|`` is preserved.
    """
    if "|" not in hostport:
        return hostport, {}
    parts = hostport.split("|")
    host = parts[0]
    meta: Dict[str, str] = {}
    for chunk in parts[1:]:
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        key, _, value = chunk.partition("=")
        meta[key.strip().lower()] = value.strip()
    return host, meta


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

    # Parse the |key=value metadata suffix from the hostport (NOT from the
    # full rest) so a password containing ``|`` is preserved — credentials
    # were already extracted above via rpartition("@").
    hostport, meta = _parse_metadata_suffix(hostport)

    if not hostport:
        return None
    asset = ProxyAsset(
        id=str(uuid.uuid4()),
        server=f"{scheme}://{hostport}",
        username=username,
        password=password,
    )
    _apply_geo_metadata(
        asset,
        country=meta.get("country"),
        timezone=meta.get("timezone"),
        locale=meta.get("locale"),
        kind=meta.get("kind"),
    )
    return asset


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

    def has_available(
        self, *, kind: Optional[Union[str, Iterable[str]]] = None
    ) -> bool:
        """Sync peek: True if any proxy is currently available (no checkout).

        Best-effort read without the lock — a concurrent ``report`` may flip a
        proxy's state mid-scan, but the caller (the scheduler) only uses this
        to pick a concurrency pool, and a wrong guess still runs the task
        (just on a suboptimal semaphore). Avoids acquiring the lock on every
        task enqueue.

        ``kind`` accepts ``None`` (any kind), a single ``str``, or a
        collection of str (any of those kinds matches).
        """
        kinds = _normalize_kinds(kind)
        now = time.monotonic()
        return any(
            self._is_available(p, now)
            and (kinds is None or p.kind in kinds)
            for p in self._proxies.values()
        )

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
        self,
        *,
        kind: Optional[Union[str, Iterable[str]]] = None,
        sitekey: Optional[str] = None,
    ) -> Optional[ProxyAsset]:
        """Return the best available proxy, or ``None`` if none can serve.

        Burned and cooling proxies are skipped. Candidates are ranked by
        per-sitekey success rate (when known), then overall rolling success
        rate, then least-recently-used as a tie-break to spread load.

        ``kind`` accepts ``None`` (any kind), a single ``str``, or a
        collection of str (any of those kinds matches) so enterprise hCaptcha
        can accept residential OR mobile in one call.
        """
        kinds = _normalize_kinds(kind)
        async with self._get_lock():
            now = time.monotonic()
            candidates = [
                p
                for p in self._proxies.values()
                if self._is_available(p, now)
                and (kinds is None or p.kind in kinds)
            ]
            if not candidates:
                return None

            def rank(p: ProxyAsset):
                # Real-outcome data (token actually accepted downstream) wins
                # when a client has reported; otherwise fall back to the
                # token-obtained rate; if neither bucket has data we treat the
                # sitekey as unknown (0.5) so a fresh proxy is still explored
                # ahead of any proven-bad one.
                real_sk = p.real_sitekey_rate(sitekey)
                if real_sk is not None:
                    sk_key = real_sk
                else:
                    tok_sk = p.sitekey_rate(sitekey)
                    sk_key = tok_sk if tok_sk is not None else 0.5
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
        """Record a per-sitekey token-obtained outcome for selection ranking.

        Written by the solver on every pool solve (``success=solved`` — did we
        obtain a token?). Real downstream outcomes go through
        ``report_sitekey_real`` so the two signals stay in separate buckets
        and client feedback doesn't get diluted by optimistic token counts.
        """
        async with self._get_lock():
            proxy = self._proxies.get(proxy_id)
            if proxy is None:
                return
            stats = proxy.sitekey_stats.setdefault(sitekey, [0, 0])
            if success:
                stats[0] += 1
            else:
                stats[1] += 1

    async def report_sitekey_real(
        self, proxy_id: str, sitekey: str, *, success: bool
    ) -> None:
        """Record a per-sitekey real-outcome (token accepted downstream).

        Written by /reportCorrect // /reportIncorrect. Mirrors
        ``report_sitekey`` but targets ``real_sitekey_stats`` so checkout
        ranking can prefer the real signal when present and fall back to the
        token-obtained bucket when no client has reported yet.
        """
        async with self._get_lock():
            proxy = self._proxies.get(proxy_id)
            if proxy is None:
                return
            stats = proxy.real_sitekey_stats.setdefault(sitekey, [0, 0])
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
                    "country": p.country,
                    "timezone": p.timezone,
                    "locale": p.locale,
                    "sitekeys": {
                        sk: {"success": s[0], "fail": s[1]}
                        for sk, s in p.sitekey_stats.items()
                    },
                    "real_sitekeys": {
                        sk: {"success": s[0], "fail": s[1]}
                        for sk, s in p.real_sitekey_stats.items()
                    },
                }
            )
        return result


# ── Protocol + Redis backend ─────────────────────────────────────────


@runtime_checkable
class ProxyPoolProtocol(Protocol):
    """Surface callers depend on. Both ProxyPool and RedisProxyPool satisfy it."""

    def add(self, proxy: ProxyAsset) -> None: ...
    def has_available(
        self, *, kind: Optional[Union[str, Iterable[str]]] = None
    ) -> bool: ...
    async def checkout(
        self,
        *,
        kind: Optional[Union[str, Iterable[str]]] = None,
        sitekey: Optional[str] = None,
    ) -> Optional[ProxyAsset]: ...
    async def report(
        self, proxy_id: str, *, success: bool, bytes_used: int = 0
    ) -> None: ...
    async def report_sitekey(
        self, proxy_id: str, sitekey: str, *, success: bool
    ) -> None: ...
    async def report_sitekey_real(
        self, proxy_id: str, sitekey: str, *, success: bool
    ) -> None: ...
    def snapshot(self) -> List[Dict[str, Any]]: ...


class RedisProxyPool:
    """Redis-backed proxy pool with cross-process shared health/state.

    Proxies are stored as JSON blobs in a Redis hash ``{prefix}:proxies``
    (proxy_id → blob). The blob carries every ``ProxyAsset`` field so a
    checkout can reconstruct a full ``ProxyAsset`` for the caller. Per-sitekey
    stats are nested in the blob under ``sitekey_stats`` — a single ``HGET``
    keeps the hot path O(1) in round-trips (a separate
    ``{prefix}:sitekeys:{id}`` hash would double the round trips on checkout).

    Mutations (``checkout`` / ``report`` / ``report_sitekey``) serialize on a
    Redis lock ``{prefix}:lock`` (``SET NX EX``) so two workers can't race on
    the read-modify-write of a proxy's counters. The lock is short-lived (5s)
    and auto-releases on crash; if a worker can't acquire it within 10s it
    proceeds without the lock (degraded but not deadlocked — v1 best-effort).

    ``has_available`` is sync (the scheduler calls it without awaiting). It
    uses a *sync* ``redis`` client (separate from the async one) to ``HVALS``
    the proxies hash and check whether any proxy is healthy / out of cooldown.
    The check is inherently racy and best-effort: any redis error returns
    False so the scheduler falls back to the proxyless semaphore safely (a
    wrong guess still runs the task, just on a suboptimal semaphore).

    Time basis: ``last_used_at`` and ``cooldown_until`` are stored as
    ``time.time()`` (epoch seconds) so they're comparable across processes,
    unlike ``time.monotonic()`` whose origin is per-process. The in-memory
    backend uses ``time.monotonic()`` but those values are never compared
    cross-process, so the divergence is safe.

    A restart no longer zeroes pool health: success/fail counters, cooldown
    state, and per-sitekey stats all survive in Redis.
    """

    def __init__(
        self,
        url: str,
        *,
        cooldown_seconds: int = 120,
        max_consecutive_fails: int = 3,
        key_prefix: str = "captcha:proxy",
        lock_timeout_seconds: int = 5,
    ) -> None:
        try:
            import redis.asyncio as aioredis  # noqa: WPS433
            import redis as sync_redis  # noqa: WPS433
        except ImportError as exc:  # pragma: no cover - exercised only w/o redis
            raise RuntimeError(
                "REDIS_URL is set but the 'redis' package is not installed; "
                "add redis>=4.2 to requirements or unset REDIS_URL"
            ) from exc
        self._cooldown_seconds = cooldown_seconds
        self._max_consecutive_fails = max_consecutive_fails
        self._prefix = key_prefix
        self._lock_key = f"{key_prefix}:lock"
        self._lock_timeout = lock_timeout_seconds
        self._proxies_key = f"{key_prefix}:proxies"
        self._redis = aioredis.from_url(url, decode_responses=True)
        # Sync client used only by has_available / snapshot (sync callers).
        # decode_responses=True so JSON comes back as str, not bytes.
        self._sync_redis = sync_redis.from_url(url, decode_responses=True)

    # ── serialisation ──────────────────────────────────────────

    @staticmethod
    def _serialize(proxy: ProxyAsset) -> str:
        return json.dumps(
            {
                "id": proxy.id,
                "server": proxy.server,
                "username": proxy.username,
                "password": proxy.password,
                "kind": proxy.kind,
                "state": proxy.state,
                "success_count": proxy.success_count,
                "fail_count": proxy.fail_count,
                "consecutive_fails": proxy.consecutive_fails,
                "last_used_at": proxy.last_used_at,
                "cooldown_until": proxy.cooldown_until,
                "cost_per_gb": proxy.cost_per_gb,
                "bytes_used": proxy.bytes_used,
                "sticky_session_id": proxy.sticky_session_id,
                "country": proxy.country,
                "timezone": proxy.timezone,
                "locale": proxy.locale,
                "sitekey_stats": {
                    sk: list(stats) for sk, stats in proxy.sitekey_stats.items()
                },
                "real_sitekey_stats": {
                    sk: list(stats) for sk, stats in proxy.real_sitekey_stats.items()
                },
            }
        )

    @staticmethod
    def _deserialize(blob: str) -> ProxyAsset:
        data = json.loads(blob)
        sk_stats = data.get("sitekey_stats") or {}
        real_sk_stats = data.get("real_sitekey_stats") or {}
        return ProxyAsset(
            id=data["id"],
            server=data.get("server", ""),
            username=data.get("username"),
            password=data.get("password"),
            kind=data.get("kind", "datacenter"),
            state=data.get("state", "healthy"),
            success_count=int(data.get("success_count", 0)),
            fail_count=int(data.get("fail_count", 0)),
            consecutive_fails=int(data.get("consecutive_fails", 0)),
            last_used_at=float(data.get("last_used_at", 0.0)),
            cooldown_until=float(data.get("cooldown_until", 0.0)),
            cost_per_gb=float(data.get("cost_per_gb", 0.0)),
            bytes_used=int(data.get("bytes_used", 0)),
            sticky_session_id=data.get("sticky_session_id"),
            country=data.get("country"),
            timezone=data.get("timezone"),
            locale=data.get("locale"),
            sitekey_stats={
                sk: [int(x) for x in stats] for sk, stats in sk_stats.items()
            },
            real_sitekey_stats={
                sk: [int(x) for x in stats] for sk, stats in real_sk_stats.items()
            },
        )

    # ── lock helper ─────────────────────────────────────────────

    async def _acquire_lock(self) -> str:
        """Spin until we get the lock or 10s elapse. Empty token = failed."""
        import secrets

        token = secrets.token_hex(8)
        deadline = time.time() + 10.0
        while time.time() < deadline:
            ok = await self._redis.set(
                self._lock_key, token, nx=True, ex=self._lock_timeout
            )
            if ok:
                return token
            await asyncio.sleep(0.05)
        return ""

    async def _release_lock(self, token: str) -> None:
        if not token:
            return
        # Only DEL if the value matches — avoids releasing someone else's lock
        # if ours expired mid-operation and was reacquired.
        script = (
            "if redis.call('get', KEYS[1]) == ARGV[1] "
            "then return redis.call('del', KEYS[1]) else return 0 end"
        )
        try:
            await self._redis.eval(script, 1, self._lock_key, token)
        except Exception:
            pass

    # ── public API ──────────────────────────────────────────────

    def add(self, proxy: ProxyAsset) -> None:
        """Seed a proxy into the pool (sync — called at startup).

        Raises if redis is unreachable: if ``REDIS_URL`` is configured and
        redis is down at startup, fail loud rather than silently running with
        an empty pool (matches ``RedisCostLedger`` which also raises).
        """
        self._sync_redis.hset(
            self._proxies_key, proxy.id, self._serialize(proxy)
        )

    def has_available(
        self, *, kind: Optional[Union[str, Iterable[str]]] = None
    ) -> bool:
        """Sync best-effort peek: True if any proxy is healthy / out of cooldown.

        Uses the *sync* redis client because the scheduler calls this without
        awaiting. The check is a full ``HVALS`` of the proxies hash — racy
        but acceptable for v1 (the scheduler only uses this to pick a
        semaphore; a wrong guess still runs the task). Any redis error
        returns False so the scheduler falls back to the proxyless semaphore
        safely.

        ``kind`` accepts ``None`` (any kind), a single ``str``, or a
        collection of str (any of those kinds matches).
        """
        kinds = _normalize_kinds(kind)
        try:
            now = time.time()
            for blob in self._sync_redis.hvals(self._proxies_key):
                try:
                    data = json.loads(blob)
                except (json.JSONDecodeError, TypeError):
                    continue
                state = data.get("state", "healthy")
                if state == "burned":
                    continue
                if state == "cooldown" and float(
                    data.get("cooldown_until", 0.0)
                ) > now:
                    continue
                if kinds is not None and data.get("kind", "datacenter") not in kinds:
                    continue
                return True
            return False
        except Exception:
            return False

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
        self,
        *,
        kind: Optional[Union[str, Iterable[str]]] = None,
        sitekey: Optional[str] = None,
    ) -> Optional[ProxyAsset]:
        """Return the best available proxy, or ``None`` if none can serve.

        Same ranking as the in-memory pool: per-sitekey rate, then overall
        success rate, then least-recently-used. Serialised on a Redis lock so
        two workers can't both pick the same proxy.

        ``kind`` accepts ``None`` (any kind), a single ``str``, or a
        collection of str (any of those kinds matches) so enterprise hCaptcha
        can accept residential OR mobile in one call.
        """
        kinds = _normalize_kinds(kind)
        token = await self._acquire_lock()
        try:
            now = time.time()
            raw = await self._redis.hgetall(self._proxies_key)
            candidates: List[ProxyAsset] = []
            for _id, blob in raw.items():
                try:
                    p = self._deserialize(blob)
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue
                if self._is_available(p, now) and (
                    kinds is None or p.kind in kinds
                ):
                    candidates.append(p)
            if not candidates:
                return None

            def rank(p: ProxyAsset):
                # Real-outcome data (token actually accepted downstream) wins
                # when a client has reported; otherwise fall back to the
                # token-obtained rate; if neither bucket has data we treat the
                # sitekey as unknown (0.5) so a fresh proxy is still explored
                # ahead of any proven-bad one.
                real_sk = p.real_sitekey_rate(sitekey)
                if real_sk is not None:
                    sk_key = real_sk
                else:
                    tok_sk = p.sitekey_rate(sitekey)
                    sk_key = tok_sk if tok_sk is not None else 0.5
                return (sk_key, p.success_rate(), -p.last_used_at)

            best = max(candidates, key=rank)
            best.last_used_at = now
            if best.success_count == 0:
                best.state = "probing"
            await self._redis.hset(
                self._proxies_key, best.id, self._serialize(best)
            )
            return best
        finally:
            await self._release_lock(token)

    async def report(
        self, proxy_id: str, *, success: bool, bytes_used: int = 0
    ) -> None:
        """Record the outcome of a solve attempt on ``proxy_id``."""
        token = await self._acquire_lock()
        try:
            raw = await self._redis.hget(self._proxies_key, proxy_id)
            if raw is None:
                return
            try:
                proxy = self._deserialize(raw)
            except (json.JSONDecodeError, KeyError, TypeError):
                return
            proxy.bytes_used += max(0, bytes_used)
            if success:
                proxy.success_count += 1
                proxy.consecutive_fails = 0
                if proxy.state != "burned":
                    proxy.state = "healthy"
            else:
                proxy.fail_count += 1
                proxy.consecutive_fails += 1
                if proxy.consecutive_fails >= self._max_consecutive_fails:
                    proxy.state = "cooldown"
                    proxy.cooldown_until = (
                        time.time() + self._cooldown_seconds
                    )
            await self._redis.hset(
                self._proxies_key, proxy_id, self._serialize(proxy)
            )
        finally:
            await self._release_lock(token)

    async def report_sitekey(
        self, proxy_id: str, sitekey: str, *, success: bool
    ) -> None:
        """Record a per-sitekey token-obtained outcome for selection ranking.

        Written by the solver on every pool solve. Real downstream outcomes go
        through ``report_sitekey_real`` so the two signals stay separate.
        """
        token = await self._acquire_lock()
        try:
            raw = await self._redis.hget(self._proxies_key, proxy_id)
            if raw is None:
                return
            try:
                proxy = self._deserialize(raw)
            except (json.JSONDecodeError, KeyError, TypeError):
                return
            stats = proxy.sitekey_stats.setdefault(sitekey, [0, 0])
            if success:
                stats[0] += 1
            else:
                stats[1] += 1
            await self._redis.hset(
                self._proxies_key, proxy_id, self._serialize(proxy)
            )
        finally:
            await self._release_lock(token)

    async def report_sitekey_real(
        self, proxy_id: str, sitekey: str, *, success: bool
    ) -> None:
        """Record a per-sitekey real-outcome (token accepted downstream).

        Written by /reportCorrect // /reportIncorrect. Mirrors
        ``report_sitekey`` but targets ``real_sitekey_stats`` so checkout
        ranking can prefer the real signal when present and fall back to the
        token-obtained bucket when no client has reported yet.
        """
        token = await self._acquire_lock()
        try:
            raw = await self._redis.hget(self._proxies_key, proxy_id)
            if raw is None:
                return
            try:
                proxy = self._deserialize(raw)
            except (json.JSONDecodeError, KeyError, TypeError):
                return
            stats = proxy.real_sitekey_stats.setdefault(sitekey, [0, 0])
            if success:
                stats[0] += 1
            else:
                stats[1] += 1
            await self._redis.hset(
                self._proxies_key, proxy_id, self._serialize(proxy)
            )
        finally:
            await self._release_lock(token)

    def snapshot(self) -> List[Dict[str, Any]]:
        """Serialisable view of the pool for an admin endpoint (sync best-effort)."""
        now = time.time()
        result: List[Dict[str, Any]] = []
        try:
            for _id, blob in self._sync_redis.hgetall(self._proxies_key).items():
                try:
                    p = self._deserialize(blob)
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue
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
                        "country": p.country,
                        "timezone": p.timezone,
                        "locale": p.locale,
                        "sitekeys": {
                            sk: {"success": s[0], "fail": s[1]}
                            for sk, s in p.sitekey_stats.items()
                        },
                        "real_sitekeys": {
                            sk: {"success": s[0], "fail": s[1]}
                            for sk, s in p.real_sitekey_stats.items()
                        },
                    }
                )
        except Exception:
            return []
        return result

    async def close(self) -> None:
        """Release both Redis connections (async + sync)."""
        try:
            await self._redis.aclose()
        except Exception:
            pass
        try:
            self._sync_redis.close()
        except Exception:
            pass


def build_proxy_pool(config: Any) -> "ProxyPool | RedisProxyPool":
    """Select a proxy-pool backend: Redis when configured, else in-memory.

    Using Redis means proxy health, cooldown state, and per-sitekey stats are
    shared across workers and survive a restart, so a proxy that burned on
    worker A isn't retried by worker B.
    """
    redis_url = getattr(config, "redis_url", None)
    if redis_url:
        return RedisProxyPool(
            redis_url,
            cooldown_seconds=getattr(config, "proxy_cooldown", 120),
            max_consecutive_fails=getattr(config, "proxy_max_consecutive_fails", 3),
        )
    return ProxyPool(
        cooldown_seconds=getattr(config, "proxy_cooldown", 120),
        max_consecutive_fails=getattr(config, "proxy_max_consecutive_fails", 3),
    )
