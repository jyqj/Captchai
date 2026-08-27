"""Stable identities for assets loaded from static configuration.

Task-supplied proxies are intentionally ephemeral and keep their random UUIDs.
Entries loaded from ``PROXY_POOL``, however, represent durable inventory. Giving
those entries a random id on every process start causes Redis-backed pools to
accumulate duplicate rows and lose continuity in health/sitekey statistics.
"""

from __future__ import annotations

from hashlib import sha256

from .proxy_pool import ProxyAsset


def inventory_proxy_id(proxy: ProxyAsset) -> str:
    """Return a deterministic, credential-sensitive id for configured proxies.

    The digest includes the endpoint, credentials template, and proxy class.
    Secrets are never exposed in the id itself. Geo annotations are deliberately
    excluded: they describe the same egress asset and may be enriched later by
    probing without changing its identity.
    """
    canonical = "\0".join(
        (
            proxy.server.strip(),
            proxy.username or "",
            proxy.password or "",
            proxy.kind,
        )
    )
    digest = sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"inventory-{digest}"
