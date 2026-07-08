"""Exit-IP geo probing for proxies without manual geo annotation.

The fingerprint is only coherent when its timezone / locale match the proxy's
*exit* IP: a German exit IP presenting ``America/New_York`` + ``en-US`` is an
explicit inconsistency signal that enterprise hCaptcha / Stripe Radar score
against. Previously geo alignment relied entirely on a manual
``|country=DE`` annotation on the proxy string; an un-annotated proxy silently
fell back to a random locale/timezone.

This module closes that gap: on a proxy's first pool checkout, it makes ONE
lightweight request *through that proxy* to an IP-geo endpoint, derives the
country, and lets the caller cache/persist the derived timezone + locale onto
the :class:`~src.assets.proxy_pool.ProxyAsset`. It is strictly best-effort —
any failure leaves the proxy's geo as ``None`` so the fingerprint keeps the
pre-existing random coherent identity (no regression) — and it never runs
twice for the same proxy (guarded by ``ProxyAsset.geo_probed``).
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger(__name__)

# ``fetch`` seam: ``async (url, proxy_url, timeout) -> dict | None``. The
# default hits the network with httpx; tests inject a fake so the probe runs
# with no network.
GeoFetch = Callable[[str, Optional[str], float], Awaitable[Optional[dict]]]


def _proxy_url_with_credentials(asset: Any) -> Optional[str]:
    """Rebuild a ``scheme://user:pass@host:port`` URL for an httpx proxy.

    Uses the sticky-session-substituted credentials (via ``playwright_proxy``)
    so the probe egresses through the *same* exit IP the solve will use.
    Returns ``None`` when the proxy has no server.
    """
    pw = asset.playwright_proxy()
    if not pw or not pw.get("server"):
        return None
    parts = urlsplit(pw["server"])
    if not parts.scheme or not parts.netloc:
        return None
    user = pw.get("username")
    password = pw.get("password")
    netloc = parts.netloc
    if user:
        cred = user if not password else f"{user}:{password}"
        netloc = f"{cred}@{parts.netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _extract_country(data: Optional[dict]) -> Optional[str]:
    """Pull a 2-letter country code out of a geo endpoint's JSON payload.

    Handles the common shapes: ip-api.com (``countryCode``), ipinfo.io
    (``country``), ipapi.co (``country_code`` / ``country``). Returns an
    upper-cased 2-letter code or ``None``.
    """
    if not isinstance(data, dict):
        return None
    for key in ("countryCode", "country_code", "country"):
        value = data.get(key)
        if isinstance(value, str) and len(value.strip()) == 2:
            return value.strip().upper()
    return None


async def _httpx_fetch_json(
    url: str, proxy_url: Optional[str], timeout: float
) -> Optional[dict]:
    """Default fetch: GET ``url`` through ``proxy_url`` and parse JSON.

    Best-effort — returns ``None`` on any error (timeout, non-200, bad JSON,
    proxy down). Never raises. Supports both httpx>=0.28 (``proxy=``) and the
    older ``proxies=`` keyword.
    """
    try:
        import httpx  # noqa: WPS433 - already a hard dependency
    except Exception:  # pragma: no cover - httpx is in requirements
        return None
    try:
        try:
            client = httpx.AsyncClient(proxy=proxy_url, timeout=timeout)
        except TypeError:  # pragma: no cover - older httpx keyword
            client = httpx.AsyncClient(proxies=proxy_url, timeout=timeout)
        async with client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None
            return resp.json()
    except Exception as exc:  # noqa: BLE001 - probe must never break a solve
        log.debug("geo probe fetch failed: %s", exc)
        return None


async def probe_proxy_geo(
    asset: Any,
    *,
    url: str,
    timeout: float = 8.0,
    fetch: Optional[GeoFetch] = None,
) -> bool:
    """Probe ``asset``'s exit-IP country and apply derived timezone / locale.

    Returns ``True`` when a country was resolved and applied, ``False``
    otherwise (already annotated, already probed, no server, or the probe
    failed). Marks ``asset.geo_probed = True`` regardless of outcome so a proxy
    whose IP has no resolvable geo isn't re-probed on every checkout.
    """
    # Skip proxies that already carry geo (manual annotation wins) or were
    # already probed once (success or failure).
    if getattr(asset, "country", None) or getattr(asset, "geo_probed", False):
        return False

    proxy_url = _proxy_url_with_credentials(asset)
    if proxy_url is None:
        asset.geo_probed = True
        return False

    fetch_fn = fetch or _httpx_fetch_json
    data = await fetch_fn(url, proxy_url, timeout)
    asset.geo_probed = True

    country = _extract_country(data)
    if not country:
        return False

    # Derive timezone / locale from the country via the shared table.
    from .proxy_pool import _apply_geo_metadata

    _apply_geo_metadata(
        asset, country=country, timezone=None, locale=None, kind=None
    )
    log.info(
        "Proxy %s exit-IP geo probed: country=%s timezone=%s locale=%s",
        getattr(asset, "id", "?"),
        asset.country,
        asset.timezone,
        asset.locale,
    )
    return bool(asset.country)
