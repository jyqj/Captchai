"""Proxy inventory with health tracking and per-sitekey success ranking.

A ``ProxyPool`` holds ``ProxyAsset`` objects and hands out the healthiest proxy
on ``checkout``, skipping any that are burned or cooling down. Health is tracked
via rolling success/fail counters; a run of consecutive TRANSPORT failures
parks a proxy in ``cooldown`` for a configurable window. Later-stage failures
(parse / solve / submit) are reported via ``report(..., stage=...)`` but do not
penalize the proxy. Ratio-based exclusion requires a minimum sample count and
is a cooldown tombstone, not a permanent blacklist. Per-sitekey success is
tracked separately so a proxy that reliably solves a given sitekey is preferred
for it.

Proxy ids are stable: a truncated SHA-256 over the full egress identity
(``scheme://user:pass@host:port`` plus sticky-session pins). Re-adding a proxy
(restart / inventory reload) upserts the same key and keeps its health history;
the in-memory backend's ``add_all`` also prunes ids no longer in the loaded
inventory.

An empty pool is a valid, first-class state: ``checkout`` returns ``None`` and
callers fall back to the proxy supplied on the task itself (proxyless op).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Tuple, Union, cast, runtime_checkable

log = logging.getLogger(__name__)

# Health semantics (minimal viable, Phase-0):
# * A proxy is only excluded by its rolling success *ratio* once it has enough
#   samples for that ratio to mean anything — one bad solve must never
#   blacklist otherwise-healthy capacity.
# * Ratio-based exclusion is a cooldown tombstone (it expires), not a permanent
#   ``burned`` state. When the cooldown lapses the proxy gets a probation window
#   (its stale counters are zeroed) instead of being re-excluded forever.
DEFAULT_MIN_HEALTH_SAMPLES = 5
DEFAULT_MIN_SUCCESS_RATE = 0.2
# How often the Redis backend's background task reconciles its in-process
# availability snapshot from the shared hash. Jittered ±20% so N workers don't
# stampede Redis in lockstep.
_SNAPSHOT_REFRESH_SECONDS = 5.0


def _stable_proxy_id(
    server: str,
    username: Optional[str],
    password: Optional[str] = None,
    *,
    session: Optional[str] = None,
    sticky: Optional[str] = None,
) -> str:
    """Deterministic identity over the *full* egress tuple, not just server+user.

    Per-boot ``uuid4`` ids made every restart look like a brand-new proxy, so
    Redis accumulated orphaned entries and health stats never followed the
    same proxy across boots. Hashing the egress identity makes ``add`` an
    upsert: the same proxy always maps to the same key and keeps its health
    record.

    The identity must include the password and any explicit sticky-session
    pin: gateways commonly carry the sticky-session token in the password (or
    a ``|session=`` annotation), so hashing only ``server|username`` merged
    genuinely distinct egress identities — they then shared health history and
    bandwidth quota as if they were one proxy.

    Plain proxies (no password, no session extras) keep the legacy
    ``sha256(f"{server}|{username or ''}")`` material so their existing ids —
    and health records — survive the upgrade. Credential-bearing proxies use a
    JSON-encoded tuple so a ``|`` inside a credential cannot make two distinct
    identities collide.
    """
    session_text = str(session) if session else ""
    # An explicit ``session`` pin wins over ``sticky`` in ``_apply_geo_metadata``,
    # so canonicalise the redundant ``|sticky=true`` combination to the same
    # identity instead of hashing two spellings of the same egress pin.
    sticky_on = (
        not session_text
        and bool(sticky)
        and str(sticky).strip().lower() in {"1", "true", "yes"}
    )
    if password or session_text or sticky_on:
        material = json.dumps(
            [server, username or "", password or "", session_text, sticky_on],
            separators=(",", ":"),
        )
    else:
        material = f"{server}|{username or ''}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


# ProxyAsset fields that describe *health history* and must survive an
# ``add`` re-seed of the same stable id.
_HEALTH_FIELDS: Tuple[str, ...] = (
    "state",
    "success_count",
    "fail_count",
    "consecutive_fails",
    "last_used_at",
    "cooldown_until",
    "bytes_used",
)
# Fields that are learned once and should not be wiped by a bare inventory
# re-seed (manual geo annotations / sticky session pinning).
_LEARNED_FIELDS: Tuple[str, ...] = (
    "country",
    "timezone",
    "locale",
    "geo_probed",
    "sticky_session_id",
)


def _preserve_health(incoming: ProxyAsset, existing: ProxyAsset) -> ProxyAsset:
    """Copy health history from ``existing`` onto a re-added ``incoming`` asset.

    Called by ``add`` when the (stable) id already exists so a restart or an
    inventory reload upserts the proxy without resetting its counters, cooldown
    or bandwidth usage. Learned geo / sticky-session fields also survive unless
    the incoming asset explicitly supplies them.
    """
    for name in _HEALTH_FIELDS:
        setattr(incoming, name, getattr(existing, name))
    incoming.sitekey_stats = {
        sk: list(stats) for sk, stats in existing.sitekey_stats.items()
    }
    incoming.real_sitekey_stats = {
        sk: list(stats) for sk, stats in existing.real_sitekey_stats.items()
    }
    for name in _LEARNED_FIELDS:
        if not getattr(incoming, name, None):
            setattr(incoming, name, getattr(existing, name, None))
    if not getattr(incoming, "cost_per_gb", 0.0) and getattr(
        existing, "cost_per_gb", 0.0
    ):
        incoming.cost_per_gb = existing.cost_per_gb
    return incoming


def _proxy_available(
    proxy: ProxyAsset,
    now: float,
    *,
    cooldown_seconds: float,
    min_samples: int = DEFAULT_MIN_HEALTH_SAMPLES,
    min_success_rate: float = DEFAULT_MIN_SUCCESS_RATE,
) -> bool:
    """Availability check shared by both backends.

    Only three things can exclude a proxy:

    * ``burned`` — a hard cap (bandwidth quota); permanent by design.
    * an active cooldown (consecutive-failure park or ratio tombstone).
    * a poor rolling success ratio *after* at least ``min_samples`` attempts.

    The ratio check is a cooldown tombstone: it expires, and on expiry the
    stale counters are zeroed so the proxy gets a fresh probation window. That
    is what stops a single early failure (or an unlucky streak) from
    permanently blacklisting a proxy. May mutate ``proxy`` (lazy rehab) — the
    caller owns the object (in-memory backend) or persists it explicitly
    (Redis backend).
    """
    if proxy.state == "burned":
        return False
    if proxy.state == "cooldown":
        if proxy.cooldown_until > now:
            return False
        # Cooldown elapsed: rehabilitate lazily so it can be picked again.
        proxy.state = "healthy"
        proxy.consecutive_fails = 0
        total = proxy.success_count + proxy.fail_count
        if total >= min_samples and proxy.success_rate() < min_success_rate:
            # Ratio tombstone expiring: reset the stale record so the proxy is
            # retried on probation rather than re-tombstoned immediately.
            proxy.success_count = 0
            proxy.fail_count = 0
        return True
    total = proxy.success_count + proxy.fail_count
    if total >= min_samples and proxy.success_rate() < min_success_rate:
        proxy.state = "cooldown"
        proxy.cooldown_until = now + cooldown_seconds
        return False
    return True


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
    # WP-geo: set once ``probe_proxy_geo`` has attempted an exit-IP geo lookup
    # (success or failure) so a proxy whose IP has no resolvable geo isn't
    # re-probed on every checkout. Manually-annotated proxies (``country`` set)
    # never need probing.
    geo_probed: bool = False
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
        """Return a Playwright proxy dict, or ``None`` if no server configured.

        Sticky-session support: residential gateways pin the exit IP for a
        window when a session token is embedded in the username (or password).
        When ``username`` / ``password`` contain a ``{session}`` placeholder it
        is substituted with :attr:`sticky_session_id`, which is generated
        lazily and cached on the asset so every ``playwright_proxy()`` call for
        this proxy yields the *same* exit IP. This closes the gap where a
        rotating gateway could hand the solve one IP and the caller's
        downstream submit a different one — which gets an IP-bound token
        rejected.
        """
        if not self.server:
            return None
        proxy: Dict[str, str] = {"server": self.server}
        username = self._inject_session(self.username)
        password = self._inject_session(self.password)
        if username:
            proxy["username"] = username
        if password:
            proxy["password"] = password
        return proxy

    def _inject_session(self, value: Optional[str]) -> Optional[str]:
        """Substitute the ``{session}`` placeholder with the sticky session id.

        No placeholder → the value is returned unchanged (no behaviour change
        for non-sticky proxies). A placeholder forces a stable session id to be
        generated once and reused, so the gateway keeps the same exit IP.
        """
        if not value or "{session}" not in value:
            return value
        if not self.sticky_session_id:
            self.sticky_session_id = uuid.uuid4().hex[:16]
        return value.replace("{session}", self.sticky_session_id)

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
    username_str = str(username) if username else None
    password_str = str(password) if password else None
    asset = ProxyAsset(
        # Stable, deterministic identity (see ``_stable_proxy_id``) so a
        # restart upserts the same proxy instead of orphaning its health stats.
        # The password / sticky-session pin are part of the identity: gateways
        # that embed the session token there are distinct egress identities.
        id=_stable_proxy_id(
            server,
            username_str,
            password_str,
            session=params.get("session"),
            sticky=params.get("sticky"),
        ),
        server=server,
        username=username_str,
        password=password_str,
    )
    # Split-field form doesn't carry the |country=DE suffix; callers can still
    # pass ``country``/``timezone``/``locale``/``kind`` as separate params.
    _apply_geo_metadata(
        asset,
        country=params.get("country"),
        timezone=params.get("timezone"),
        locale=params.get("locale"),
        kind=params.get("kind"),
        session=params.get("session"),
        sticky=params.get("sticky"),
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
    session: Optional[str] = None,
    sticky: Optional[str] = None,
) -> None:
    """Mutate ``asset`` in place to apply parsed geo / kind / session metadata.

    Unknown / empty values are ignored. When ``country`` is given but
    ``timezone`` or ``locale`` are not, they are derived from ``_COUNTRY_GEO``.
    ``kind`` is normalised to one of {"datacenter","residential","mobile"} —
    anything else falls back to ``"datacenter"`` (the ProxyAsset default).

    ``session`` pins an explicit sticky session id (substituted into a
    ``{session}`` username/password placeholder). ``sticky`` (``true``/``1``)
    auto-generates one so a gateway that reads the token from the username
    keeps the same exit IP across the whole solve and any reused warm session.
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
    if session:
        asset.sticky_session_id = str(session)
    elif sticky and str(sticky).strip().lower() in {"1", "true", "yes"}:
        asset.sticky_session_id = uuid.uuid4().hex[:16]
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
    server = f"{scheme}://{hostport}"
    asset = ProxyAsset(
        # Stable identity from the full egress tuple, not a per-boot uuid4.
        # Credentials and the |session= / |sticky= metadata suffix are part of
        # the identity so two proxies that differ only in their sticky-session
        # token don't merge (and share health/quota).
        id=_stable_proxy_id(
            server,
            username,
            password,
            session=meta.get("session"),
            sticky=meta.get("sticky"),
        ),
        server=server,
        username=username,
        password=password,
    )
    _apply_geo_metadata(
        asset,
        country=meta.get("country"),
        timezone=meta.get("timezone"),
        locale=meta.get("locale"),
        kind=meta.get("kind"),
        session=meta.get("session"),
        sticky=meta.get("sticky"),
    )
    return asset


class ProxyPool:
    """Concurrency-safe collection of proxies with health-aware selection."""

    def __init__(
        self,
        *,
        cooldown_seconds: int = 120,
        max_consecutive_fails: int = 3,
        max_bytes_per_proxy: int = 0,
        min_health_samples: int = DEFAULT_MIN_HEALTH_SAMPLES,
        min_success_rate: float = DEFAULT_MIN_SUCCESS_RATE,
    ) -> None:
        self._cooldown_seconds = cooldown_seconds
        self._max_consecutive_fails = max_consecutive_fails
        self._min_health_samples = max(1, int(min_health_samples))
        self._min_success_rate = float(min_success_rate)
        # Bandwidth quota per proxy in bytes; 0 disables. Residential/mobile
        # proxies are billed per GB, so a proxy that has burned through its
        # quota is retired ("burned") to cap spend regardless of its health.
        self._max_bytes_per_proxy = max_bytes_per_proxy
        self._proxies: Dict[str, ProxyAsset] = {}
        # Created lazily so the pool can be constructed outside a running event
        # loop (on Python 3.9 asyncio.Lock() binds to the loop at construction).
        self._lock: Optional[asyncio.Lock] = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def add(self, proxy: ProxyAsset) -> None:
        """Upsert a proxy by its stable id, preserving existing health history.

        Re-adding a proxy (restart / inventory reload) must not zero its
        counters, cooldown or bandwidth usage — otherwise a burned or cooling
        proxy would be retried on every boot. New copies of learned fields
        (geo / sticky session) are only overwritten when explicitly supplied.
        """
        existing = self._proxies.get(proxy.id)
        if existing is not None and existing is not proxy:
            _preserve_health(proxy, existing)
        self._proxies[proxy.id] = proxy

    def add_all(self, proxies: Iterable[ProxyAsset]) -> None:
        """Replace the inventory: upsert each proxy, then prune stale ids.

        ``add`` alone cannot know when an inventory load has finished, so a
        removed proxy would linger forever. This is the batch entry point for
        startup inventory loading; entries whose stable id is no longer in
        ``proxies`` are dropped (Redis deliberately does not prune — entries
        there may be owned by another worker).
        """
        keep: set = set()
        for proxy in proxies:
            self.add(proxy)
            keep.add(proxy.id)
        for proxy_id in list(self._proxies):
            if proxy_id not in keep:
                del self._proxies[proxy_id]

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
            for p in list(self._proxies.values())
        )

    def _is_available(self, proxy: ProxyAsset, now: float) -> bool:
        return _proxy_available(
            proxy,
            now,
            cooldown_seconds=self._cooldown_seconds,
            min_samples=self._min_health_samples,
            min_success_rate=self._min_success_rate,
        )

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
        self,
        proxy_id: str,
        *,
        success: bool,
        bytes_used: int = 0,
        stage: str = "transport",
    ) -> None:
        """Record the outcome of a solve attempt on ``proxy_id``.

        On success the consecutive-fail streak resets and the proxy returns to
        ``healthy``. On failure, only ``stage="transport"`` (the default)
        counts against proxy health: a failure in a later stage (challenge
        detection, model parsing, downstream submit — i.e. something that is
        not the proxy's fault) is logged but must not push the proxy toward
        cooldown. After ``max_consecutive_fails`` transport failures in a row
        the proxy is parked in ``cooldown`` until ``now + cooldown_seconds``.
        """
        try:
            async with self._get_lock():
                proxy = self._proxies.get(proxy_id)
                if proxy is None:
                    return
                proxy.bytes_used += max(0, bytes_used)
                if success:
                    proxy.success_count += 1
                    proxy.consecutive_fails = 0
                    if proxy.state != "burned":
                        proxy.state = "healthy"
                elif stage != "transport":
                    # Not a proxy-health event: keep it out of the
                    # success/fail ratio and the consecutive-fail streak.
                    log.info(
                        "proxy %s: non-transport failure (stage=%s) not "
                        "counted against health",
                        proxy_id,
                        stage,
                    )
                else:
                    proxy.fail_count += 1
                    proxy.consecutive_fails += 1
                    if proxy.consecutive_fails >= self._max_consecutive_fails:
                        proxy.state = "cooldown"
                        proxy.cooldown_until = (
                            time.monotonic() + self._cooldown_seconds
                        )
                # Bandwidth quota overrides health: a proxy past its byte cap is
                # burned (removed from rotation) even on a successful solve, so
                # metered residential/mobile spend stays bounded.
                if (
                    self._max_bytes_per_proxy
                    and proxy.bytes_used >= self._max_bytes_per_proxy
                ):
                    proxy.state = "burned"
        except Exception:  # noqa: BLE001 - reporting is best-effort
            log.warning(
                "proxy report failed for %s (best-effort)", proxy_id, exc_info=True
            )

    async def report_bytes(self, proxy_id: str, nbytes: int) -> None:
        """Account ``nbytes`` of bandwidth against a proxy's remaining budget.

        Separate from :meth:`report` so a caller that measures traffic (browser
        downloads, upstream submit bodies, ...) can feed the meter without
        recording a solve outcome. Decrements the remaining-bytes budget by
        incrementing ``bytes_used`` and burns the proxy once the configured
        ``max_bytes_per_proxy`` cap is crossed. Best-effort: never raises.
        """
        try:
            async with self._get_lock():
                proxy = self._proxies.get(proxy_id)
                if proxy is None:
                    return
                proxy.bytes_used += max(0, int(nbytes))
                if (
                    self._max_bytes_per_proxy
                    and proxy.bytes_used >= self._max_bytes_per_proxy
                ):
                    proxy.state = "burned"
        except Exception:  # noqa: BLE001 - bandwidth accounting is best-effort
            log.warning(
                "proxy report_bytes failed for %s (best-effort)",
                proxy_id,
                exc_info=True,
            )

    async def set_geo(
        self,
        proxy_id: str,
        *,
        country: Optional[str],
        timezone: Optional[str],
        locale: Optional[str],
        geo_probed: bool = True,
    ) -> None:
        """Persist probed exit-IP geo onto a proxy so later checkouts reuse it.

        Called after ``geo_probe.probe_proxy_geo`` resolves (or fails to
        resolve) a proxy's country, so the timezone/locale/country and the
        ``geo_probed`` guard survive across checkouts. In-memory the checked-out
        object is the live one, but writing through this method keeps the
        contract identical to the Redis backend.
        """
        async with self._get_lock():
            proxy = self._proxies.get(proxy_id)
            if proxy is None:
                return
            if country:
                proxy.country = country
            if timezone:
                proxy.timezone = timezone
            if locale:
                proxy.locale = locale
            proxy.geo_probed = geo_probed

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
                    "geo_probed": p.geo_probed,
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
        self,
        proxy_id: str,
        *,
        success: bool,
        bytes_used: int = 0,
        stage: str = "transport",
    ) -> None: ...
    async def report_bytes(self, proxy_id: str, nbytes: int) -> None: ...
    async def report_sitekey(
        self, proxy_id: str, sitekey: str, *, success: bool
    ) -> None: ...
    async def report_sitekey_real(
        self, proxy_id: str, sitekey: str, *, success: bool
    ) -> None: ...
    async def set_geo(
        self,
        proxy_id: str,
        *,
        country: Optional[str],
        timezone: Optional[str],
        locale: Optional[str],
        geo_probed: bool = True,
    ) -> None: ...
    def snapshot(self) -> List[Dict[str, Any]]: ...


# How long a per-sitekey stats key lives without a fresh report. TTL bounds
# cardinality over time even when proxies churn through many sitekeys.
_DEFAULT_SITEKEY_TTL_SECONDS = 7 * 24 * 60 * 60
# At most this many sitekeys are tracked per proxy per bucket (token-obtained /
# real outcome). The least active entries are evicted; selection ranking only
# needs the recent, hot sitekeys.
_DEFAULT_MAX_SITEKEYS_PER_PROXY = 64

# Atomic checkout: availability (burned / cooldown / ratio tombstone), kind
# filter, ranking (real-outcome sitekey rate → token sitekey rate → overall
# success rate → least-recently-used) and the reservation write all happen in
# one server-side script. Two workers therefore cannot both reserve the same
# proxy based on the same stale read.
_CHECKOUT_LUA = r"""
-- KEYS[1] = SET of proxy ids
-- ARGV[1] proxy hash key prefix        ARGV[6] minimum health samples
-- ARGV[2] requested sitekey ('' = any) ARGV[7] minimum success rate
-- ARGV[3] now (epoch seconds, string)  ARGV[8] token sitekey key prefix
-- ARGV[4] accepted kinds, csv         ARGV[9] real-outcome sitekey key prefix
-- ARGV[5] cooldown seconds
local ids_key = KEYS[1]
local proxy_prefix = ARGV[1]
local sitekey = ARGV[2]
local now_text = ARGV[3]
local now = tonumber(now_text)
local kinds_csv = ARGV[4]
local cooldown = tonumber(ARGV[5])
local min_samples = tonumber(ARGV[6])
local min_rate = tonumber(ARGV[7])
local token_prefix = ARGV[8]
local real_prefix = ARGV[9]

local function to_num(value, default)
  if value == nil or value == false then return default end
  local number = tonumber(value)
  if number == nil then return default end
  return number
end

local function kind_matches(kind)
  if kinds_csv == '' then return true end
  for accepted in string.gmatch(kinds_csv, '([^,]+)') do
    if accepted == kind then return true end
  end
  return false
end

local function sitekey_rate(prefix, proxy_id)
  if sitekey == '' then return nil end
  local stats = redis.call(
    'HMGET', prefix .. proxy_id .. ':' .. sitekey, 'ok', 'fail')
  local ok = tonumber(stats[1] or '')
  local fail = tonumber(stats[2] or '')
  if ok == nil and fail == nil then return nil end
  ok = ok or 0
  fail = fail or 0
  local total = ok + fail
  if total <= 0 then return nil end
  return ok / total
end

local best_id = nil
local best_rate = nil
local best_overall = nil
local best_last_used = nil

local ids = redis.call('SMEMBERS', ids_key)
for index = 1, #ids do
  local proxy_id = ids[index]
  local key = proxy_prefix .. proxy_id
  local flat = redis.call('HGETALL', key)
  if #flat > 0 then
    local fields = {}
    for position = 1, #flat, 2 do
      fields[flat[position]] = flat[position + 1]
    end
    local state = fields['state'] or 'healthy'
    local success = to_num(fields['success_count'], 0)
    local fail = to_num(fields['fail_count'], 0)
    local consecutive = to_num(fields['consecutive_fails'], 0)
    local cooldown_until = to_num(fields['cooldown_until'], 0)
    local last_used = to_num(fields['last_used_at'], 0)
    local available = true
    local dirty = false
    if state == 'burned' then
      available = false
    elseif state == 'cooldown' then
      if cooldown_until > now then
        available = false
      else
        -- Cooldown elapsed: rehabilitate on probation, zeroing stale ratio
        -- counters so the proxy is not immediately re-tombstoned.
        state = 'healthy'
        consecutive = 0
        dirty = true
        local total = success + fail
        if total >= min_samples and (success / total) < min_rate then
          success = 0
          fail = 0
          dirty = true
        end
      end
    else
      local total = success + fail
      if total >= min_samples and (success / total) < min_rate then
        state = 'cooldown'
        cooldown_until = now + cooldown
        dirty = true
        available = false
      end
    end
    if dirty then
      redis.call('HSET', key,
        'state', state,
        'success_count', tostring(success),
        'fail_count', tostring(fail),
        'consecutive_fails', tostring(consecutive),
        'cooldown_until', tostring(cooldown_until))
    end
    if available and kind_matches(fields['kind'] or 'datacenter') then
      local sk_rate = sitekey_rate(real_prefix, proxy_id)
      if sk_rate == nil then
        sk_rate = sitekey_rate(token_prefix, proxy_id)
      end
      if sk_rate == nil then sk_rate = 0.5 end
      local total = success + fail
      local overall = 1.0
      if total > 0 then overall = success / total end
      local better = false
      if best_id == nil then
        better = true
      elseif sk_rate > best_rate then
        better = true
      elseif sk_rate == best_rate then
        if overall > best_overall then
          better = true
        elseif overall == best_overall and last_used < best_last_used then
          better = true
        end
      end
      if better then
        best_id = proxy_id
        best_rate = sk_rate
        best_overall = overall
        best_last_used = last_used
      end
    end
  end
end

if best_id == nil then
  return {}
end

-- Reserve the winner atomically: the reservation is what makes a concurrent
-- checkout prefer a different proxy on its (least-recently-used) tie-break.
local best_key = proxy_prefix .. best_id
redis.call('HSET', best_key, 'last_used_at', now_text)
local best_success = to_num(redis.call('HGET', best_key, 'success_count'), 0)
if best_success == 0 then
  redis.call('HSET', best_key, 'state', 'probing')
end
return redis.call('HGETALL', best_key)
"""

# Atomic report: counter updates plus the conditional cooldown / burn
# transitions are applied to individual hash fields in one round trip.
_REPORT_LUA = r"""
-- KEYS[1] proxy hash key
-- ARGV[1] success ('1'/'0')          ARGV[5] cooldown seconds
-- ARGV[2] bytes_used                 ARGV[6] max consecutive fails
-- ARGV[3] stage                      ARGV[7] max bytes per proxy (0 = off)
-- ARGV[4] now (epoch seconds)
local key = KEYS[1]
if redis.call('EXISTS', key) == 0 then return {} end
local success = ARGV[1] == '1'
local bytes = tonumber(ARGV[2]) or 0
if bytes < 0 then bytes = 0 end
local stage = ARGV[3]
local now = tonumber(ARGV[4])
local cooldown = tonumber(ARGV[5])
local max_fails = tonumber(ARGV[6])
local max_bytes = tonumber(ARGV[7])
if bytes > 0 then
  redis.call('HINCRBY', key, 'bytes_used', bytes)
end
if success then
  redis.call('HINCRBY', key, 'success_count', 1)
  redis.call('HSET', key, 'consecutive_fails', 0)
  if redis.call('HGET', key, 'state') ~= 'burned' then
    redis.call('HSET', key, 'state', 'healthy')
  end
elseif stage == 'transport' then
  redis.call('HINCRBY', key, 'fail_count', 1)
  local consecutive = redis.call('HINCRBY', key, 'consecutive_fails', 1)
  if consecutive >= max_fails then
    redis.call('HSET', key, 'state', 'cooldown',
               'cooldown_until', tostring(now + cooldown))
  end
end
if max_bytes > 0 then
  local used = tonumber(redis.call('HGET', key, 'bytes_used') or '0') or 0
  if used >= max_bytes then
    redis.call('HSET', key, 'state', 'burned')
  end
end
return redis.call('HGETALL', key)
"""

# Atomic byte accounting: increment the field and burn on cap, one round trip.
_REPORT_BYTES_LUA = r"""
-- KEYS[1] proxy hash key
-- ARGV[1] nbytes    ARGV[2] max bytes per proxy (0 = off)
local key = KEYS[1]
if redis.call('EXISTS', key) == 0 then return {} end
local bytes = tonumber(ARGV[1]) or 0
if bytes < 0 then bytes = 0 end
redis.call('HINCRBY', key, 'bytes_used', bytes)
local max_bytes = tonumber(ARGV[2]) or 0
if max_bytes > 0 then
  local used = tonumber(redis.call('HGET', key, 'bytes_used') or '0') or 0
  if used >= max_bytes then
    redis.call('HSET', key, 'state', 'burned')
  end
end
return redis.call('HGETALL', key)
"""


class RedisProxyPool:
    """Redis-backed proxy pool with cross-process shared health/state.

    Storage layout (everything under ``key_prefix``):

    * ``{prefix}:ids`` — SET of proxy ids (the inventory index).
    * ``{prefix}:p:{id}`` — HASH of scalar proxy fields. Mutations update
      individual fields (``HINCRBY`` / ``HSET``); a whole blob is never
      rewritten.
    * ``{prefix}:sk:{id}:{sitekey}`` — token-obtained counters (``ok`` /
      ``fail``) with a rolling TTL.
    * ``{prefix}:skr:{id}:{sitekey}`` — real-outcome counters (token accepted
      downstream), same shape.
    * ``{prefix}:skz:{id}`` / ``{prefix}:skrz:{id}`` — activity ZSETs that cap
      each proxy at ``max_sitekeys_per_proxy`` sitekeys per bucket (the least
      active entries are evicted).
    * ``{prefix}:proxies`` — legacy JSON-blob hash from earlier versions. It is
      read once per id on ``add`` so health history survives the upgrade, and
      then rewritten in the hash layout; it is never written again.

    ``checkout`` is a single ``EVAL``: health transitions, ranking and the
    reservation write happen atomically server-side, so concurrent workers
    cannot both act on the same stale read. A client that rejects ``EVAL``
    (fakeredis without Lua, a managed Redis with scripting disabled, ...)
    transparently falls back to the lock-protected pure-Python path.

    ``report`` / ``report_bytes`` are single-round-trip pipelined writes of the
    affected hash fields; the conditional cooldown / burn transition runs in a
    small server-side script inside that pipeline. The lock path is used only
    when scripting is unavailable.

    ``has_available`` is sync (the scheduler calls it without awaiting) and
    must not block the event loop: it serves reads from an in-process
    availability snapshot instead of doing redis I/O. The snapshot is updated
    on every local mutation (add / checkout / report ...) and reconciled from
    the shared hashes by a background asyncio task (created lazily on first
    async use, ~5s period with jitter) so changes made by other workers are
    picked up. Any error returns False so the scheduler falls back to the
    proxyless semaphore safely (a wrong guess still runs the task, just on a
    suboptimal semaphore).

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
        max_bytes_per_proxy: int = 0,
        min_health_samples: int = DEFAULT_MIN_HEALTH_SAMPLES,
        min_success_rate: float = DEFAULT_MIN_SUCCESS_RATE,
        sitekey_ttl_seconds: int = _DEFAULT_SITEKEY_TTL_SECONDS,
        max_sitekeys_per_proxy: int = _DEFAULT_MAX_SITEKEYS_PER_PROXY,
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
        self._min_health_samples = max(1, int(min_health_samples))
        self._min_success_rate = float(min_success_rate)
        self._max_bytes_per_proxy = max_bytes_per_proxy
        self._sitekey_ttl = max(1, int(sitekey_ttl_seconds))
        self._max_sitekeys = max(1, int(max_sitekeys_per_proxy))
        self._prefix = key_prefix
        self._lock_key = f"{key_prefix}:lock"
        self._lock_timeout = lock_timeout_seconds
        # Legacy JSON-blob hash (read-only migration source).
        self._proxies_key = f"{key_prefix}:proxies"
        # New per-field layout.
        self._ids_key = f"{key_prefix}:ids"
        self._proxy_prefix = f"{key_prefix}:p:"
        self._sitekey_prefix = f"{key_prefix}:sk:"
        self._sitekey_zset_prefix = f"{key_prefix}:skz:"
        self._real_sitekey_prefix = f"{key_prefix}:skr:"
        self._real_sitekey_zset_prefix = f"{key_prefix}:skrz:"
        # Flipped to False the first time the client rejects EVAL so a
        # scripting-less Redis is not retried on every operation.
        self._eval_supported = True
        self._redis = aioredis.from_url(url, decode_responses=True)
        # Sync client used only by snapshot() (an explicitly sync admin read).
        # decode_responses=True so JSON comes back as str, not bytes.
        self._sync_redis = sync_redis.from_url(url, decode_responses=True)
        # In-process availability mirror: id -> ProxyAsset, seeded by add()
        # and refreshed from Redis in the background. ``has_available`` reads
        # only this, so the scheduler never blocks on redis I/O.
        self._snapshot: Dict[str, ProxyAsset] = {}
        self._refresh_task: Optional[asyncio.Task] = None
        self._refresh_loop: Optional[asyncio.AbstractEventLoop] = None
        self._refresh_period = _SNAPSHOT_REFRESH_SECONDS
        # Started immediately when the pool is built inside a running loop so
        # a worker that shares Redis inventory (and loads none locally) still
        # sees ``has_available`` become true without waiting for a checkout.
        self._ensure_refresh_task()

    # ── availability snapshot (no I/O on the sync read path) ────

    def _ensure_refresh_task(self) -> None:
        """Start the snapshot refresher on first async use (idempotent)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # sync-only caller; add()/mutations keep the snapshot warm
        task = self._refresh_task
        if task is not None and not task.done() and self._refresh_loop is loop:
            return
        if task is not None and not task.done():
            # Created under a previous event loop (e.g. tests calling
            # asyncio.run per phase); retire it and rebind.
            task.cancel()
        self._refresh_loop = loop
        self._refresh_task = loop.create_task(self._refresh_snapshot_loop())

    async def _refresh_snapshot_loop(self) -> None:
        """Periodically reconcile the in-process snapshot from Redis.

        Jittered so N workers don't all ``HGETALL`` on the same tick.
        """
        while True:
            try:
                await self._refresh_snapshot()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a stale snapshot is not fatal
                log.debug("proxy availability refresh failed", exc_info=True)
            await asyncio.sleep(
                self._refresh_period * random.uniform(0.8, 1.2)
            )

    def _refresh_snapshot_sync(self) -> None:
        """Reconcile the availability snapshot with the sync client.

        Runs in a worker thread (see :meth:`_refresh_snapshot`): a thread
        future is cancellable even while an in-flight client read is wedged,
        which keeps ``close()`` prompt on every client implementation.
        """
        raw_ids = self._sync_redis.smembers(self._ids_key)
        ids = sorted(self._text(proxy_id) for proxy_id in raw_ids)
        if not ids:
            self._snapshot = {}
            return
        pipe = self._sync_redis.pipeline(transaction=False)
        for proxy_id in ids:
            pipe.hgetall(self._proxy_key(proxy_id))
        rows = pipe.execute()
        refreshed: Dict[str, ProxyAsset] = {}
        for proxy_id, fields in zip(ids, rows):
            if not fields:
                continue
            try:
                refreshed[proxy_id] = self._deserialize_fields(fields)
            except (KeyError, TypeError, ValueError):
                continue
        self._snapshot = refreshed

    async def _refresh_snapshot(self) -> None:
        await asyncio.to_thread(self._refresh_snapshot_sync)

    def _remember(self, proxy: ProxyAsset) -> None:
        """Update one entry of the in-process availability snapshot."""
        self._snapshot[proxy.id] = proxy

    async def _stop_refresh_task(self) -> None:
        """Cancel the background refresher, tolerating cross-loop teardown."""
        task = self._refresh_task
        self._refresh_task = None
        loop = self._refresh_loop
        self._refresh_loop = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            return
        if loop is not None and loop is not running:
            return  # task belongs to a loop we cannot await from here
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - teardown is best-effort
            pass

    # ── serialisation ──────────────────────────────────────────

    @staticmethod
    def _text(value: Any) -> str:
        """Normalise a redis reply value (str / bytes / None) to text."""
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        return str(value)

    @classmethod
    def _flat_to_dict(cls, flat: Any) -> Dict[str, str]:
        """Pair a flat ``HGETALL`` / Lua reply into a field mapping."""
        values = list(flat or [])
        return {
            cls._text(values[index]): cls._text(values[index + 1])
            for index in range(0, len(values) - 1, 2)
        }

    @staticmethod
    def _as_int(value: Any, default: int = 0) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _hset_mapping(fields: Dict[str, str]) -> Mapping[Any, Any]:
        """Widen ``str`` hash fields for redis-py's ``hset(mapping=...)``.

        redis-py types the mapping as ``Mapping[FieldT, EncodableT]`` where
        ``FieldT`` is a union alias, and ``Mapping`` keys are invariant, so
        ``Dict[str, str]`` is rejected even though every actual field name is a
        ``str``. We always build string-keyed field dicts, so the cast is
        runtime-truthful.
        """
        return cast("Mapping[Any, Any]", fields)

    @classmethod
    def _fields(cls, proxy: ProxyAsset) -> Dict[str, str]:
        """Flat hash fields for a proxy; sitekey stats live in their own keys."""
        return {
            "id": proxy.id,
            "server": proxy.server,
            "username": proxy.username or "",
            "password": proxy.password or "",
            "kind": proxy.kind,
            "state": proxy.state,
            "success_count": str(int(proxy.success_count)),
            "fail_count": str(int(proxy.fail_count)),
            "consecutive_fails": str(int(proxy.consecutive_fails)),
            "last_used_at": str(float(proxy.last_used_at)),
            "cooldown_until": str(float(proxy.cooldown_until)),
            "cost_per_gb": str(float(proxy.cost_per_gb)),
            "bytes_used": str(int(proxy.bytes_used)),
            "sticky_session_id": proxy.sticky_session_id or "",
            "country": proxy.country or "",
            "timezone": proxy.timezone or "",
            "locale": proxy.locale or "",
            "geo_probed": "1" if proxy.geo_probed else "0",
        }

    @classmethod
    def _deserialize_fields(cls, raw: Any) -> ProxyAsset:
        """Rebuild a ``ProxyAsset`` from a flat field mapping (hash layout)."""
        fields = {
            cls._text(key): cls._text(value) for key, value in dict(raw or {}).items()
        }
        if not fields:
            raise KeyError("empty proxy hash")
        return ProxyAsset(
            id=fields.get("id", ""),
            server=fields.get("server", ""),
            username=fields.get("username") or None,
            password=fields.get("password") or None,
            kind=fields.get("kind") or "datacenter",
            state=fields.get("state") or "healthy",
            success_count=cls._as_int(fields.get("success_count")),
            fail_count=cls._as_int(fields.get("fail_count")),
            consecutive_fails=cls._as_int(fields.get("consecutive_fails")),
            last_used_at=float(fields.get("last_used_at") or 0.0),
            cooldown_until=float(fields.get("cooldown_until") or 0.0),
            cost_per_gb=float(fields.get("cost_per_gb") or 0.0),
            bytes_used=cls._as_int(fields.get("bytes_used")),
            sticky_session_id=fields.get("sticky_session_id") or None,
            country=fields.get("country") or None,
            timezone=fields.get("timezone") or None,
            locale=fields.get("locale") or None,
            geo_probed=fields.get("geo_probed") in {"1", "true", "True"},
        )

    @staticmethod
    def _serialize(proxy: ProxyAsset) -> str:
        """Legacy JSON-blob encoding (kept for older workers / migrations)."""
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
                "geo_probed": proxy.geo_probed,
                "sitekey_stats": {
                    sk: list(stats) for sk, stats in proxy.sitekey_stats.items()
                },
                "real_sitekey_stats": {
                    sk: list(stats) for sk, stats in proxy.real_sitekey_stats.items()
                },
            }
        )

    @classmethod
    def _deserialize(cls, blob: Union[str, Dict[str, Any]]) -> ProxyAsset:
        """Rebuild a proxy from a hash mapping or a legacy JSON blob."""
        if isinstance(blob, dict):
            return cls._deserialize_fields(blob)
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
            geo_probed=bool(data.get("geo_probed", False)),
            sitekey_stats={
                sk: [int(x) for x in stats] for sk, stats in sk_stats.items()
            },
            real_sitekey_stats={
                sk: [int(x) for x in stats] for sk, stats in real_sk_stats.items()
            },
        )

    # ── key / field helpers ────────────────────────────────────

    def _proxy_key(self, proxy_id: str) -> str:
        return self._proxy_prefix + proxy_id

    def _sitekey_key(
        self, proxy_id: str, sitekey: str, *, real: bool = False
    ) -> str:
        prefix = self._real_sitekey_prefix if real else self._sitekey_prefix
        return f"{prefix}{proxy_id}:{sitekey}"

    def _sitekey_zset(self, proxy_id: str, *, real: bool = False) -> str:
        prefix = (
            self._real_sitekey_zset_prefix if real else self._sitekey_zset_prefix
        )
        return prefix + proxy_id

    @classmethod
    def _stats_pair(cls, values: Any) -> Optional[List[int]]:
        """Convert an ``HMGET ok fail`` reply into ``[ok, fail]`` (or None)."""
        if not values:
            return None
        if all(value is None for value in values):
            return None
        return [cls._as_int(values[0]), cls._as_int(values[1])]

    @classmethod
    def _stats_from_hash(cls, row: Any) -> Optional[List[int]]:
        """Convert a sitekey stats hash into ``[ok, fail]`` (or None)."""
        data = {cls._text(k): cls._text(v) for k, v in dict(row or {}).items()}
        if not data:
            return None
        return [cls._as_int(data.get("ok")), cls._as_int(data.get("fail"))]

    def _read_sitekeys_sync(
        self, proxy_id: str, *, real: bool
    ) -> Dict[str, List[int]]:
        """Read one proxy's sitekey bucket (sync; used by add() and snapshot())."""
        zkey = self._sitekey_zset(proxy_id, real=real)
        members = [
            self._text(member)
            for member in self._sync_redis.zrange(zkey, 0, -1)
        ]
        if not members:
            return {}
        pipe = self._sync_redis.pipeline(transaction=False)
        for sitekey in members:
            pipe.hgetall(self._sitekey_key(proxy_id, sitekey, real=real))
        rows = pipe.execute()
        stats: Dict[str, List[int]] = {}
        for sitekey, row in zip(members, rows):
            pair = self._stats_from_hash(row)
            if pair is not None:
                stats[sitekey] = pair
        return stats

    def _load_proxy_sync(self, proxy_id: str) -> Optional[ProxyAsset]:
        """Load fields + sitekey buckets with the sync client.

        Falls back to the legacy JSON blob so an upgrade preserves history for
        inventory entries written by an older worker.
        """
        fields = self._sync_redis.hgetall(self._proxy_key(proxy_id))
        proxy: Optional[ProxyAsset] = None
        if fields:
            try:
                proxy = self._deserialize_fields(fields)
            except (KeyError, TypeError, ValueError):
                proxy = None
        else:
            blob = self._sync_redis.hget(self._proxies_key, proxy_id)
            if not blob:
                return None
            try:
                # ``decode_responses=True`` makes HGET return ``str``; ``_text``
                # also tolerates a bytes reply from a non-decoding client.
                proxy = self._deserialize(self._text(blob))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                return None
        if proxy is None:
            return None
        # New-format buckets win; on a legacy blob with no migrated buckets
        # keep the sitekey stats embedded in the JSON so they are re-seeded.
        token_stats = self._read_sitekeys_sync(proxy_id, real=False)
        if token_stats:
            proxy.sitekey_stats = token_stats
        real_stats = self._read_sitekeys_sync(proxy_id, real=True)
        if real_stats:
            proxy.real_sitekey_stats = real_stats
        return proxy

    def _seed_sitekeys(
        self,
        pipe: Any,
        proxy: ProxyAsset,
        stats: Dict[str, List[int]],
        *,
        real: bool,
    ) -> None:
        """Queue hash/ZSET writes for a proxy's seeded sitekey buckets."""
        if not stats:
            return
        zkey = self._sitekey_zset(proxy.id, real=real)
        for sitekey, values in stats.items():
            pair = list(values or [0, 0])
            key = self._sitekey_key(proxy.id, sitekey, real=real)
            pipe.hset(
                key,
                mapping={"ok": str(int(pair[0])), "fail": str(int(pair[1]))},
            )
            pipe.zadd(
                zkey,
                {sitekey: max(1, int(pair[0]) + int(pair[1]))},
                nx=True,
            )
            pipe.expire(key, self._sitekey_ttl)
        pipe.expire(zkey, self._sitekey_ttl)

    # ── Lua / fallback plumbing ────────────────────────────────

    @staticmethod
    def _rank(proxy: ProxyAsset, sitekey: Optional[str]):
        """Selection tuple; ``max()`` prefers LRU on a full tie."""
        real_sk = proxy.real_sitekey_rate(sitekey)
        if real_sk is not None:
            sk_key = real_sk
        else:
            tok_sk = proxy.sitekey_rate(sitekey)
            sk_key = tok_sk if tok_sk is not None else 0.5
        return (sk_key, proxy.success_rate(), -proxy.last_used_at)

    @staticmethod
    def _eval_unsupported(exc: BaseException) -> bool:
        """Heuristic: did the client reject EVAL because scripting is absent?"""
        name = type(exc).__name__.lower()
        if name in {"attributeerror", "notimplementederror"}:
            return True
        if not name.endswith("error"):
            return False
        message = str(exc).lower()
        markers = (
            "unknown command",
            "'eval'",
            "`eval`",
            "not supported",
            "unsupported",
            "not allowed",
            "noperm",
            "disabled",
        )
        return any(marker in message for marker in markers)

    async def _run_script(self, script: str, key: str, *args: Any) -> Any:
        """Run a script through a pipeline (one round trip)."""
        pipe = self._redis.pipeline(transaction=False)
        pipe.eval(script, 1, key, *args)
        result = await pipe.execute()
        return result[0] if result else None

    def _disable_eval(self, exc: BaseException) -> None:
        self._eval_supported = False
        log.warning(
            "proxy pool: EVAL rejected (%s: %s); using the pure-Python "
            "fallback path for this pool",
            type(exc).__name__,
            exc,
        )

    # ── lock helper (fallback path only) ───────────────────────

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
            return
        except Exception:
            pass
        # Scripting is unavailable (this is the fallback path): a plain
        # GET+DEL keeps the 5s lock TTL from stalling every later fallback
        # operation behind an expired lock. The check/delete window is tiny
        # and only reachable without EVAL, where correctness is best-effort.
        try:
            current = await self._redis.get(self._lock_key)
            if self._text(current) == token:
                await self._redis.delete(self._lock_key)
        except Exception:
            pass

    # ── public API ──────────────────────────────────────────────

    def add(self, proxy: ProxyAsset) -> None:
        """Seed a proxy into the pool (sync — called at startup).

        Upserts by the proxy's stable id so a restart keeps the entry's health
        history instead of overwriting it with zeroed counters. Health is read
        from the per-field hash (or, on the first boot after an upgrade, from
        the legacy JSON blob) and written back as individual hash fields plus
        the id index; no whole blob is rewritten.

        Raises if redis is unreachable: if ``REDIS_URL`` is configured and
        redis is down at startup, fail loud rather than silently running with
        an empty pool (matches ``RedisCostLedger`` which also raises).
        """
        existing = self._load_proxy_sync(proxy.id)
        if existing is not None and existing is not proxy:
            _preserve_health(proxy, existing)
        pipe = self._sync_redis.pipeline(transaction=False)
        pipe.hset(self._proxy_key(proxy.id), mapping=self._hset_mapping(self._fields(proxy)))
        pipe.sadd(self._ids_key, proxy.id)
        self._seed_sitekeys(pipe, proxy, proxy.sitekey_stats, real=False)
        self._seed_sitekeys(pipe, proxy, proxy.real_sitekey_stats, real=True)
        pipe.execute()
        self._remember(proxy)

    def add_all(self, proxies: Iterable[ProxyAsset]) -> None:
        """Upsert a loaded inventory (no pruning: Redis is shared state)."""
        for proxy in proxies:
            self.add(proxy)

    def has_available(
        self, *, kind: Optional[Union[str, Iterable[str]]] = None
    ) -> bool:
        """Sync best-effort peek: True if any proxy is healthy / out of cooldown.

        Served entirely from the in-process availability snapshot — the hot
        path performs **no redis I/O** (the scheduler calls this without
        awaiting; a sync round-trip here blocks the event loop). The snapshot
        is kept current by local mutations and reconciled from Redis by a
        background task every ~5s, so another worker's changes appear within
        one refresh period. Any error returns False so the scheduler falls
        back to the proxyless semaphore safely.

        ``kind`` accepts ``None`` (any kind), a single ``str``, or a
        collection of str (any of those kinds matches).
        """
        kinds = _normalize_kinds(kind)
        try:
            now = time.time()
            for proxy in list(self._snapshot.values()):
                if kinds is not None and proxy.kind not in kinds:
                    continue
                if _proxy_available(
                    proxy,
                    now,
                    cooldown_seconds=self._cooldown_seconds,
                    min_samples=self._min_health_samples,
                    min_success_rate=self._min_success_rate,
                ):
                    return True
            return False
        except Exception:  # noqa: BLE001 - best-effort availability guess
            return False

    def _is_available(self, proxy: ProxyAsset, now: float) -> bool:
        return _proxy_available(
            proxy,
            now,
            cooldown_seconds=self._cooldown_seconds,
            min_samples=self._min_health_samples,
            min_success_rate=self._min_success_rate,
        )

    async def checkout(
        self,
        *,
        kind: Optional[Union[str, Iterable[str]]] = None,
        sitekey: Optional[str] = None,
    ) -> Optional[ProxyAsset]:
        """Return the best available proxy, or ``None`` if none can serve.

        Selection, health/cooldown transitions and the reservation write run
        atomically inside ``_CHECKOUT_LUA``; concurrent workers therefore see
        each other's reservations instead of racing on a stale read. Ranking is
        the same as the in-memory pool: per-sitekey rate, then overall success
        rate, then least-recently-used. When the client rejects ``EVAL`` the
        lock-protected Python implementation takes over.

        ``kind`` accepts ``None`` (any kind), a single ``str``, or a
        collection of str (any of those kinds matches) so enterprise hCaptcha
        can accept residential OR mobile in one call.
        """
        kinds = _normalize_kinds(kind)
        self._ensure_refresh_task()
        now = time.time()
        if self._eval_supported:
            try:
                flat = await self._run_script(
                    _CHECKOUT_LUA,
                    self._ids_key,
                    self._proxy_prefix,
                    sitekey or "",
                    str(now),
                    ",".join(sorted(kinds)) if kinds else "",
                    str(self._cooldown_seconds),
                    str(self._min_health_samples),
                    str(self._min_success_rate),
                    self._sitekey_prefix,
                    self._real_sitekey_prefix,
                )
            except Exception as exc:  # noqa: BLE001
                if not self._eval_unsupported(exc):
                    raise
                self._disable_eval(exc)
            else:
                if not flat:
                    return None
                if not isinstance(flat, (list, tuple)):
                    # A stub client can answer EVAL with a non-script result;
                    # treat that as "scripting not usable" and fall back.
                    self._disable_eval(
                        RuntimeError(f"unexpected EVAL reply: {flat!r}")
                    )
                else:
                    proxy = self._deserialize_fields(self._flat_to_dict(flat))
                    self._remember(proxy)
                    return proxy
        return await self._checkout_locked(kinds=kinds, sitekey=sitekey, now=now)

    async def _checkout_locked(
        self,
        *,
        kinds: Optional[frozenset],
        sitekey: Optional[str],
        now: float,
    ) -> Optional[ProxyAsset]:
        """Pure-Python checkout used when ``EVAL`` is unavailable.

        Serialised on the ``{prefix}:lock`` key and reading every proxy hash in
        one pipeline. Only the requested sitekey's stats are loaded — that is
        all the ranking needs.
        """
        token = await self._acquire_lock()
        try:
            raw_ids = await self._redis.smembers(self._ids_key)
            ids = sorted(self._text(proxy_id) for proxy_id in raw_ids)
            if not ids:
                return None
            pipe = self._redis.pipeline(transaction=False)
            for proxy_id in ids:
                pipe.hgetall(self._proxy_key(proxy_id))
                if sitekey:
                    pipe.hmget(self._sitekey_key(proxy_id, sitekey), "ok", "fail")
                    pipe.hmget(
                        self._sitekey_key(proxy_id, sitekey, real=True),
                        "ok",
                        "fail",
                    )
            rows = await pipe.execute()
            stride = 3 if sitekey else 1
            candidates: List[ProxyAsset] = []
            changed: List[ProxyAsset] = []
            for index, proxy_id in enumerate(ids):
                fields = rows[index * stride]
                if not fields:
                    continue
                try:
                    proxy = self._deserialize_fields(fields)
                except (KeyError, TypeError, ValueError):
                    continue
                if sitekey:
                    token_pair = self._stats_pair(rows[index * stride + 1])
                    real_pair = self._stats_pair(rows[index * stride + 2])
                    if token_pair is not None:
                        proxy.sitekey_stats[sitekey] = token_pair
                    if real_pair is not None:
                        proxy.real_sitekey_stats[sitekey] = real_pair
                before = (
                    proxy.state,
                    proxy.success_count,
                    proxy.fail_count,
                    proxy.consecutive_fails,
                    proxy.cooldown_until,
                )
                available = self._is_available(proxy, now)
                if before != (
                    proxy.state,
                    proxy.success_count,
                    proxy.fail_count,
                    proxy.consecutive_fails,
                    proxy.cooldown_until,
                ):
                    # Ratio tombstone set / probation reset applied: persist it
                    # so the exclusion (and its expiry) is shared across workers.
                    changed.append(proxy)
                if available and (kinds is None or proxy.kind in kinds):
                    candidates.append(proxy)
            if changed:
                write = self._redis.pipeline(transaction=False)
                for proxy in changed:
                    write.hset(
                        self._proxy_key(proxy.id), mapping=self._hset_mapping(self._fields(proxy))
                    )
                await write.execute()
                for proxy in changed:
                    self._remember(proxy)
            if not candidates:
                return None
            best = max(candidates, key=lambda p: self._rank(p, sitekey))
            best.last_used_at = now
            if best.success_count == 0:
                best.state = "probing"
            await self._redis.hset(
                self._proxy_key(best.id), mapping=self._hset_mapping(self._fields(best))
            )
            self._remember(best)
            return best
        finally:
            await self._release_lock(token)

    async def report(
        self,
        proxy_id: str,
        *,
        success: bool,
        bytes_used: int = 0,
        stage: str = "transport",
    ) -> None:
        """Record the outcome of a solve attempt on ``proxy_id``.

        Best-effort: a redis hiccup is logged, never raised into the solve
        path. The happy path is a single pipelined ``EVAL`` that increments
        just the affected hash fields and applies the conditional cooldown /
        burn transitions atomically. Only ``stage="transport"`` failures count
        against proxy health; later-stage failures are logged but leave the
        ratio/streak untouched. When scripting is unavailable the locked
        Python path runs instead.
        """
        self._ensure_refresh_task()
        try:
            if not success and stage != "transport":
                # Not a proxy-health event (e.g. downstream submit / model
                # parse failure): do not pollute the rolling ratio.
                log.info(
                    "proxy %s: non-transport failure (stage=%s) not counted "
                    "against health",
                    proxy_id,
                    stage,
                )
            proxy: Optional[ProxyAsset] = None
            if self._eval_supported:
                try:
                    flat = await self._run_script(
                        _REPORT_LUA,
                        self._proxy_key(proxy_id),
                        "1" if success else "0",
                        str(max(0, int(bytes_used))),
                        stage,
                        str(time.time()),
                        str(self._cooldown_seconds),
                        str(self._max_consecutive_fails),
                        str(self._max_bytes_per_proxy),
                    )
                except Exception as exc:  # noqa: BLE001
                    if not self._eval_unsupported(exc):
                        raise
                    self._disable_eval(exc)
                else:
                    if flat and not isinstance(flat, (list, tuple)):
                        self._disable_eval(
                            RuntimeError(f"unexpected EVAL reply: {flat!r}")
                        )
                    elif flat:
                        proxy = self._deserialize_fields(
                            self._flat_to_dict(flat)
                        )
            if proxy is None and not self._eval_supported:
                proxy = await self._report_locked(
                    proxy_id,
                    success=success,
                    bytes_used=bytes_used,
                    stage=stage,
                )
            if proxy is not None:
                self._remember(proxy)
        except Exception:  # noqa: BLE001 - reporting is best-effort
            log.warning(
                "proxy report failed for %s (best-effort)", proxy_id, exc_info=True
            )

    async def _report_locked(
        self, proxy_id: str, *, success: bool, bytes_used: int, stage: str
    ) -> Optional[ProxyAsset]:
        """Lock-protected report used when ``EVAL`` is unavailable."""
        token = await self._acquire_lock()
        try:
            key = self._proxy_key(proxy_id)
            fields = await self._redis.hgetall(key)
            if not fields:
                return None
            try:
                proxy = self._deserialize_fields(fields)
            except (KeyError, TypeError, ValueError):
                return None
            proxy.bytes_used += max(0, int(bytes_used))
            if success:
                proxy.success_count += 1
                proxy.consecutive_fails = 0
                if proxy.state != "burned":
                    proxy.state = "healthy"
            elif stage == "transport":
                proxy.fail_count += 1
                proxy.consecutive_fails += 1
                if proxy.consecutive_fails >= self._max_consecutive_fails:
                    proxy.state = "cooldown"
                    proxy.cooldown_until = (
                        time.time() + self._cooldown_seconds
                    )
            # Bandwidth quota overrides health (see in-memory ProxyPool.report).
            if (
                self._max_bytes_per_proxy
                and proxy.bytes_used >= self._max_bytes_per_proxy
            ):
                proxy.state = "burned"
            await self._redis.hset(key, mapping=self._hset_mapping(self._fields(proxy)))
            return proxy
        finally:
            await self._release_lock(token)

    async def report_bytes(self, proxy_id: str, nbytes: int) -> None:
        """Account ``nbytes`` of bandwidth against a proxy's remaining budget.

        Decrements the remaining-bytes budget (``max_bytes_per_proxy``) and
        burns the proxy when it is exhausted, without recording a solve
        outcome. Best-effort: a redis hiccup is logged, never raised. One
        pipelined ``EVAL`` updates the single ``bytes_used`` field and the
        burn transition together; the locked path is the no-scripting fallback.
        """
        self._ensure_refresh_task()
        try:
            proxy: Optional[ProxyAsset] = None
            if self._eval_supported:
                try:
                    flat = await self._run_script(
                        _REPORT_BYTES_LUA,
                        self._proxy_key(proxy_id),
                        str(max(0, int(nbytes))),
                        str(self._max_bytes_per_proxy),
                    )
                except Exception as exc:  # noqa: BLE001
                    if not self._eval_unsupported(exc):
                        raise
                    self._disable_eval(exc)
                else:
                    if flat and not isinstance(flat, (list, tuple)):
                        self._disable_eval(
                            RuntimeError(f"unexpected EVAL reply: {flat!r}")
                        )
                    elif flat:
                        proxy = self._deserialize_fields(
                            self._flat_to_dict(flat)
                        )
            if proxy is None and not self._eval_supported:
                proxy = await self._report_bytes_locked(proxy_id, nbytes)
            if proxy is not None:
                self._remember(proxy)
        except Exception:  # noqa: BLE001 - bandwidth accounting is best-effort
            log.warning(
                "proxy report_bytes failed for %s (best-effort)",
                proxy_id,
                exc_info=True,
            )

    async def _report_bytes_locked(
        self, proxy_id: str, nbytes: int
    ) -> Optional[ProxyAsset]:
        """Lock-protected byte accounting used when ``EVAL`` is unavailable."""
        token = await self._acquire_lock()
        try:
            key = self._proxy_key(proxy_id)
            fields = await self._redis.hgetall(key)
            if not fields:
                return None
            try:
                proxy = self._deserialize_fields(fields)
            except (KeyError, TypeError, ValueError):
                return None
            proxy.bytes_used += max(0, int(nbytes))
            if (
                self._max_bytes_per_proxy
                and proxy.bytes_used >= self._max_bytes_per_proxy
            ):
                proxy.state = "burned"
            await self._redis.hset(key, mapping=self._hset_mapping(self._fields(proxy)))
            return proxy
        finally:
            await self._release_lock(token)

    async def _record_sitekey(
        self,
        proxy_id: str,
        sitekey: str,
        *,
        success: bool,
        real: bool,
    ) -> None:
        """Pipeline-increment one (proxy, sitekey) bucket and trim it to N.

        Each bucket is its own TTL-bounded key, so cardinality is bounded both
        by time and by ``max_sitekeys_per_proxy`` (least active evicted). No
        lock is needed: the counter update is a single ``HINCRBY``.
        """
        if not await self._redis.exists(self._proxy_key(proxy_id)):
            return
        key = self._sitekey_key(proxy_id, sitekey, real=real)
        zkey = self._sitekey_zset(proxy_id, real=real)
        field = "ok" if success else "fail"
        pipe = self._redis.pipeline(transaction=False)
        pipe.hincrby(key, field, 1)
        pipe.zincrby(zkey, 1, sitekey)
        pipe.expire(key, self._sitekey_ttl)
        pipe.expire(zkey, self._sitekey_ttl)
        pipe.zcard(zkey)
        results = await pipe.execute()
        count = self._as_int(results[-1] if results else 0)
        if count <= self._max_sitekeys:
            return
        victims = [
            self._text(victim)
            for victim in await self._redis.zrange(
                zkey, 0, count - self._max_sitekeys - 1
            )
        ]
        if not victims:
            return
        trim = self._redis.pipeline(transaction=False)
        for victim in victims:
            trim.delete(self._sitekey_key(proxy_id, victim, real=real))
            trim.zrem(zkey, victim)
        trim.expire(zkey, self._sitekey_ttl)
        await trim.execute()

    async def report_sitekey(
        self, proxy_id: str, sitekey: str, *, success: bool
    ) -> None:
        """Record a per-sitekey token-obtained outcome for selection ranking.

        Written by the solver on every pool solve. Real downstream outcomes go
        through ``report_sitekey_real`` so the two signals stay separate.
        Stored in ``{prefix}:sk:{id}:{sitekey}`` (TTL-bounded, capped ZSET).
        """
        self._ensure_refresh_task()
        await self._record_sitekey(
            proxy_id, sitekey, success=success, real=False
        )

    async def report_sitekey_real(
        self, proxy_id: str, sitekey: str, *, success: bool
    ) -> None:
        """Record a per-sitekey real-outcome (token accepted downstream).

        Written by /reportCorrect // /reportIncorrect. Mirrors
        ``report_sitekey`` but targets ``{prefix}:skr:...`` so checkout ranking
        can prefer the real signal when present and fall back to the
        token-obtained bucket when no client has reported yet.
        """
        self._ensure_refresh_task()
        await self._record_sitekey(
            proxy_id, sitekey, success=success, real=True
        )

    async def set_geo(
        self,
        proxy_id: str,
        *,
        country: Optional[str],
        timezone: Optional[str],
        locale: Optional[str],
        geo_probed: bool = True,
    ) -> None:
        """Persist probed exit-IP geo so later cross-process checkouts reuse it.

        Only the geo fields are written (one pipelined HSET); the rest of the
        proxy hash is left untouched.
        """
        self._ensure_refresh_task()
        key = self._proxy_key(proxy_id)
        if not await self._redis.exists(key):
            return
        fields: Dict[str, str] = {"geo_probed": "1" if geo_probed else "0"}
        if country:
            fields["country"] = country
        if timezone:
            fields["timezone"] = timezone
        if locale:
            fields["locale"] = locale
        pipe = self._redis.pipeline(transaction=False)
        pipe.hset(key, mapping=self._hset_mapping(fields))
        pipe.hgetall(key)
        result = await pipe.execute()
        flat = result[-1] if result else None
        if flat:
            self._remember(self._deserialize_fields(flat))

    @staticmethod
    def _snapshot_row(proxy: ProxyAsset, now: float) -> Dict[str, Any]:
        return {
            "id": proxy.id,
            "server": proxy.server,
            "kind": proxy.kind,
            "state": proxy.state,
            "success_count": proxy.success_count,
            "fail_count": proxy.fail_count,
            "consecutive_fails": proxy.consecutive_fails,
            "success_rate": round(proxy.success_rate(), 4),
            "cooldown_remaining": max(0.0, proxy.cooldown_until - now),
            "cost_per_gb": proxy.cost_per_gb,
            "bytes_used": proxy.bytes_used,
            "sticky_session_id": proxy.sticky_session_id,
            "country": proxy.country,
            "timezone": proxy.timezone,
            "locale": proxy.locale,
            "geo_probed": proxy.geo_probed,
            "sitekeys": {
                sk: {"success": stats[0], "fail": stats[1]}
                for sk, stats in proxy.sitekey_stats.items()
            },
            "real_sitekeys": {
                sk: {"success": stats[0], "fail": stats[1]}
                for sk, stats in proxy.real_sitekey_stats.items()
            },
        }

    def snapshot(self) -> List[Dict[str, Any]]:
        """Serialisable view of the pool for an admin endpoint (sync best-effort).

        Reads the id index, every proxy hash and the sitekey ZSETs in
        pipelines; per-sitekey values come from their own TTL-bounded keys.
        """
        now = time.time()
        result: List[Dict[str, Any]] = []
        try:
            ids = sorted(
                self._text(proxy_id)
                for proxy_id in self._sync_redis.smembers(self._ids_key)
            )
            if not ids:
                return []
            pipe = self._sync_redis.pipeline(transaction=False)
            for proxy_id in ids:
                pipe.hgetall(self._proxy_key(proxy_id))
                pipe.zrange(self._sitekey_zset(proxy_id), 0, -1)
                pipe.zrange(self._sitekey_zset(proxy_id, real=True), 0, -1)
            rows = pipe.execute()
            proxies: List[ProxyAsset] = []
            requests: List[Tuple[str, str, bool]] = []
            for index, proxy_id in enumerate(ids):
                fields = rows[index * 3]
                if not fields:
                    continue
                try:
                    proxy = self._deserialize_fields(fields)
                except (KeyError, TypeError, ValueError):
                    continue
                proxies.append(proxy)
                for member in rows[index * 3 + 1] or []:
                    requests.append((proxy_id, self._text(member), False))
                for member in rows[index * 3 + 2] or []:
                    requests.append((proxy_id, self._text(member), True))
            stats: Dict[str, Dict[bool, Dict[str, List[int]]]] = {
                proxy.id: {False: {}, True: {}} for proxy in proxies
            }
            if requests:
                stat_pipe = self._sync_redis.pipeline(transaction=False)
                for proxy_id, sitekey, real in requests:
                    stat_pipe.hgetall(
                        self._sitekey_key(proxy_id, sitekey, real=real)
                    )
                stat_rows = stat_pipe.execute()
                for (proxy_id, sitekey, real), row in zip(requests, stat_rows):
                    pair = self._stats_from_hash(row)
                    if pair is not None:
                        stats[proxy_id][real][sitekey] = pair
            for proxy in proxies:
                buckets = stats.get(proxy.id, {False: {}, True: {}})
                proxy.sitekey_stats = buckets[False]
                proxy.real_sitekey_stats = buckets[True]
                result.append(self._snapshot_row(proxy, now))
        except Exception:
            return []
        return result

    async def close(self) -> None:
        """Cancel the snapshot refresher and release both Redis connections."""
        await self._stop_refresh_task()
        try:
            await self._redis.aclose()
        except Exception:
            pass
        try:
            self._sync_redis.close()
        except Exception:
            pass

    async def aclose(self) -> None:
        """Alias for :meth:`close` (matches the async-resource naming used by
        the other Redis backends)."""
        await self.close()


def build_proxy_pool(config: Any) -> "ProxyPool | RedisProxyPool":
    """Select a proxy-pool backend: Redis when configured, else in-memory.

    Using Redis means proxy health, cooldown state, and per-sitekey stats are
    shared across workers and survive a restart, so a proxy that burned on
    worker A isn't retried by worker B.
    """
    max_bytes = _gb_to_bytes(getattr(config, "proxy_max_gb", 0.0))
    min_samples = int(
        getattr(config, "proxy_min_health_samples", DEFAULT_MIN_HEALTH_SAMPLES)
    )
    min_rate = float(
        getattr(config, "proxy_min_success_rate", DEFAULT_MIN_SUCCESS_RATE)
    )
    redis_url = getattr(config, "redis_url", None)
    if redis_url:
        return RedisProxyPool(
            redis_url,
            cooldown_seconds=getattr(config, "proxy_cooldown", 120),
            max_consecutive_fails=getattr(config, "proxy_max_consecutive_fails", 3),
            max_bytes_per_proxy=max_bytes,
            min_health_samples=min_samples,
            min_success_rate=min_rate,
        )
    return ProxyPool(
        cooldown_seconds=getattr(config, "proxy_cooldown", 120),
        max_consecutive_fails=getattr(config, "proxy_max_consecutive_fails", 3),
        max_bytes_per_proxy=max_bytes,
        min_health_samples=min_samples,
        min_success_rate=min_rate,
    )


def _gb_to_bytes(gb: float) -> int:
    """Convert a GB quota to bytes; non-positive / missing means unlimited (0)."""
    try:
        value = float(gb)
    except (TypeError, ValueError):
        return 0
    return int(value * 1024**3) if value > 0 else 0
