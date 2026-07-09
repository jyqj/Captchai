"""Optional token-trust verification — auto-close the real-outcome loop.

The browser solvers mint a token and only *structurally* check it
(``len(token) > 20``). Whether the provider actually *accepts* that token is
otherwise learned only if the caller later hits ``/reportCorrect`` /
``/reportIncorrect`` — which is optional, and many callers never do. So the
proxy-health / routing ``real``-outcome buckets (which exist precisely to
distinguish "we obtained a token" from "the token was accepted downstream")
stay empty and the optimistic token-obtained signal is all selection has.

When the operator can supply a sitekey's *secret* (they own the target, or hold
a test key), this module verifies a freshly minted token against the provider's
``siteverify`` endpoint and returns a verdict the solver feeds straight into the
same real-outcome accounting the report endpoints use — closing the trust loop
from ground truth without waiting on the caller.

Entirely opt-in: with no secret configured (the default), :func:`build_token_verifier`
returns ``None`` and solving behaves exactly as before.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Protocol, runtime_checkable

log = logging.getLogger(__name__)

# Well-known provider siteverify endpoints, keyed by the normalized provider
# string the solvers pass (``hcaptcha`` / ``turnstile`` / ``recaptcha``).
_SITEVERIFY_ENDPOINTS: Dict[str, str] = {
    "hcaptcha": "https://api.hcaptcha.com/siteverify",
    "turnstile": "https://challenges.cloudflare.com/turnstile/v0/siteverify",
    "recaptcha": "https://www.google.com/recaptcha/api/siteverify",
}


@runtime_checkable
class TokenVerifier(Protocol):
    """Verifies a minted token against the provider. Returns a tri-state verdict.

    ``True``  — provider accepted the token (real success).
    ``False`` — provider rejected the token (real failure).
    ``None``  — verdict unknown (no secret for this sitekey, verification
                disabled, or a transient error): the caller keeps the existing
                caller-driven report loop and never fails the solve on this.
    """

    async def verify(
        self,
        token: str,
        *,
        provider: str,
        sitekey: str,
        remote_ip: Optional[str] = None,
    ) -> Optional[bool]: ...


class HttpTokenVerifier:
    """Verify tokens by POSTing to the provider's ``siteverify`` endpoint.

    Configured with a ``{sitekey: secret}`` map; a token for a sitekey with no
    configured secret verifies to ``None`` (unknown), so partial configuration
    (verify the sitekeys you own, leave the rest caller-driven) is fine. Any
    network / parse error also returns ``None`` — verification must never turn a
    successful solve into a failure on its own.
    """

    def __init__(
        self,
        secrets: Dict[str, str],
        *,
        endpoints: Optional[Dict[str, str]] = None,
        timeout: float = 10.0,
    ) -> None:
        self._secrets = dict(secrets)
        self._endpoints = {**_SITEVERIFY_ENDPOINTS, **(endpoints or {})}
        self._timeout = timeout

    def has_secret(self, sitekey: str) -> bool:
        return sitekey in self._secrets

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
        import httpx  # local import: keeps this module import-light for tests

        data: Dict[str, Any] = {"secret": secret, "response": token}
        if remote_ip:
            data["remoteip"] = remote_ip
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(endpoint, data=data)
                resp.raise_for_status()
                body = resp.json()
        except Exception as exc:  # noqa: BLE001 - never fail a solve on verify
            log.debug("token siteverify call failed (%s): %s", provider, exc)
            return None
        success = body.get("success") if isinstance(body, dict) else None
        if isinstance(success, bool):
            return success
        return None


def parse_secret_map(raw: str) -> Dict[str, str]:
    """Parse ``"sitekey1:secret1,sitekey2:secret2"`` into a ``{sitekey: secret}``.

    Blank / malformed entries are skipped. Secrets may themselves contain ``:``
    (split only on the first), so URL-ish secrets survive.
    """
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
    """Return a configured verifier, or ``None`` when token verification is off.

    Off by default: requires ``TOKEN_VERIFY_ENABLED=true`` *and* at least one
    ``sitekey:secret`` pair in ``TOKEN_VERIFY_SECRETS``. Without both, returns
    ``None`` and the solvers skip verification entirely.
    """
    if not getattr(config, "token_verify_enabled", False):
        return None
    secrets = parse_secret_map(getattr(config, "token_verify_secrets", ""))
    if not secrets:
        return None
    return HttpTokenVerifier(
        secrets,
        timeout=float(getattr(config, "token_verify_timeout", 10.0)),
    )
