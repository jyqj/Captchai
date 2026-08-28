"""Optional provider-side token verification.

When an operator owns a sitekey and can configure its secret, a freshly minted
token can be checked against the provider's verification endpoint. The verifier
owns one reusable ``httpx.AsyncClient`` so repeated checks share connection
pools instead of paying DNS/TLS/client construction on every solve.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable, Dict, Optional, Protocol, runtime_checkable

log = logging.getLogger(__name__)

_SITEVERIFY_ENDPOINTS: Dict[str, str] = {
    "hcaptcha": "https://api.hcaptcha.com/siteverify",
    "turnstile": "https://challenges.cloudflare.com/turnstile/v0/siteverify",
    "recaptcha": "https://www.google.com/recaptcha/api/siteverify",
}


@runtime_checkable
class TokenVerifier(Protocol):
    """Verify a minted token and return ``True`` / ``False`` / unknown ``None``."""

    async def verify(
        self,
        token: str,
        *,
        provider: str,
        sitekey: str,
        remote_ip: Optional[str] = None,
    ) -> Optional[bool]: ...


HttpClientFactory = Callable[[float], Any]


def _default_http_client_factory(timeout: float) -> Any:
    import httpx

    return httpx.AsyncClient(timeout=timeout)


class HttpTokenVerifier:
    """Verify tokens through provider ``siteverify`` endpoints.

    A network/parse failure returns ``None`` and never turns a successful solve
    into an application failure. The HTTP client is lazy, shared, injectable for
    tests, and closed by :class:`src.core.services.SolverServices`.
    """

    def __init__(
        self,
        secrets: Dict[str, str],
        *,
        endpoints: Optional[Dict[str, str]] = None,
        timeout: float = 10.0,
        client_factory: Optional[HttpClientFactory] = None,
    ) -> None:
        self._secrets = dict(secrets)
        self._endpoints = {**_SITEVERIFY_ENDPOINTS, **(endpoints or {})}
        self._timeout = timeout
        self._client_factory = client_factory or _default_http_client_factory
        self._client: Any = None
        self._closed = False

    def has_secret(self, sitekey: str) -> bool:
        return sitekey in self._secrets

    def _raw_client(self) -> Any:
        if self._closed:
            raise RuntimeError("token verifier is closed")
        if self._client is None:
            self._client = self._client_factory(self._timeout)
        return self._client

    async def verify(
        self,
        token: str,
        *,
        provider: str,
        sitekey: str,
        remote_ip: Optional[str] = None,
    ) -> Optional[bool]:
        secret = self._secrets.get(sitekey)
        endpoint = self._endpoints.get(provider)
        if not secret or not endpoint:
            return None

        data: Dict[str, Any] = {"secret": secret, "response": token}
        if remote_ip:
            data["remoteip"] = remote_ip
        try:
            resp = await self._raw_client().post(endpoint, data=data)
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:  # noqa: BLE001 - never fail a solve on verify
            log.debug("token siteverify call failed (%s): %s", provider, exc)
            return None
        success = body.get("success") if isinstance(body, dict) else None
        if isinstance(success, bool):
            return success
        return None

    async def close(self) -> None:
        """Close the shared HTTP client exactly once."""
        if self._closed:
            return
        self._closed = True
        client, self._client = self._client, None
        if client is None:
            return
        closer = getattr(client, "aclose", None)
        if closer is None:
            closer = getattr(client, "close", None)
        if closer is None:
            return
        result = closer()
        if inspect.isawaitable(result):
            await result


def parse_secret_map(raw: str) -> Dict[str, str]:
    """Parse ``sitekey:secret`` comma pairs, skipping malformed entries."""
    out: Dict[str, str] = {}
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        sitekey, _, secret = entry.partition(":")
        sitekey, secret = sitekey.strip(), secret.strip()
        if sitekey and secret:
            out[sitekey] = secret
    return out


def build_token_verifier(config: Any) -> Optional[TokenVerifier]:
    """Build the opt-in verifier, or ``None`` when disabled/unconfigured."""
    if not getattr(config, "token_verify_enabled", False):
        return None
    secrets = parse_secret_map(getattr(config, "token_verify_secrets", ""))
    if not secrets:
        return None
    return HttpTokenVerifier(
        secrets,
        timeout=float(getattr(config, "token_verify_timeout", 10.0)),
    )
