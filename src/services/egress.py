"""Egress classification + solution-echo helpers, decoupled from the solver.

``browser_solver`` previously mixed three concerns: the egress taxonomy /
task-proxy detection helpers, the ``BaseBrowserSolver`` lifecycle, and the
human-mouse warmup. These pure, stateless egress helpers live here so they can
be imported without pulling in the Playwright-backed base class. ``browser_solver``
re-exports them for backward compatibility, so existing
``from .browser_solver import egress_from_params`` imports keep working.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from ..assets.proxy_pool import proxy_from_params


class ProxyKind(str, Enum):
    """Explicit egress category used for scheduling and accounting."""

    PROXYLESS = "proxyless"
    TASK_PROXY = "task_proxy"
    POOL_PROXY = "pool_proxy"


def has_task_proxy(params: dict[str, Any]) -> bool:
    """Return true when the caller supplied explicit proxy fields."""

    return proxy_from_params(params) is not None


def initial_proxy_kind(params: dict[str, Any]) -> ProxyKind:
    """Classify the request before server-side pools are consulted."""

    return ProxyKind.TASK_PROXY if has_task_proxy(params) else ProxyKind.PROXYLESS


def proxy_ip_from_params(params: dict[str, Any]) -> Optional[str]:
    """Extract a stable proxy host for token-cache bucketing."""

    address = params.get("proxyAddress")
    if address:
        return str(address)
    single = params.get("proxy")
    if single and "://" in str(single):
        rest = str(single).split("://", 1)[1]
        hostport = rest.rsplit("@", 1)[-1]
        return hostport.split(":", 1)[0]
    return None


@dataclass(frozen=True)
class SolveIdentity:
    """The resolved, immutable identity of a single solve.

    One solve mints a token bound to a *coherent* identity: an egress (proxy
    kind + gateway server), a warm session (or fresh context), and a browser
    fingerprint geo (timezone + languages). Those facts were previously carried
    as a handful of loose ``_``-prefixed keys on the mutable ``params`` dict —
    ``_proxyKind`` / ``_egress_server`` / ``_pool_proxy_id`` / ``_sessionId`` /
    ``_used_timezone`` / ``_used_languages`` — an untyped "identity bus" shared
    across the acquire → solve → record → echo seams with no invariants, easy to
    read with the wrong key or forget to surface.

    :class:`SolveIdentity` freezes those into one value object with a single
    construction point (:meth:`from_params`), so downstream code (the ledger
    record, the solution echo) reads typed fields instead of fishing individual
    strings out of the dict. The dict keys remain the transport (the fingerprint
    build in ``browser.py`` and several tests read them directly), but the
    *meaning* — "this is the solve's identity" — now lives in one typed place.
    """

    proxy_kind: Optional[str] = None
    egress_server: Optional[str] = None
    proxy_id: Optional[str] = None
    session_id: Optional[str] = None
    timezone_id: Optional[str] = None
    languages: tuple[str, ...] = ()

    @property
    def accept_language(self) -> Optional[str]:
        """The ``Accept-Language`` header the solve's fingerprint presented."""
        return ", ".join(self.languages) if self.languages else None

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> "SolveIdentity":
        """Collect the resolved identity keys off ``params`` into one object.

        Missing keys (e.g. a mocked ``_acquire_context`` in tests, or a
        proxyless solve with no server geo) resolve to ``None`` / empty so the
        object is always constructible.
        """
        langs = params.get("_used_languages") or []
        return cls(
            proxy_kind=params.get("_proxyKind"),
            egress_server=params.get("_egress_server"),
            proxy_id=params.get("_pool_proxy_id"),
            session_id=params.get("_sessionId"),
            timezone_id=params.get("_used_timezone"),
            languages=tuple(langs),
        )

    def solution_fields(self) -> dict[str, Any]:
        """The egress + geo fields surfaced in the solution for IP/UA binding.

        Matches the union of :func:`egress_from_params` and
        :func:`fingerprint_geo_from_params` so callers can align their
        downstream submit egress + browser context with the solve's.
        """
        return {
            "proxyKind": self.proxy_kind,
            "egressServer": self.egress_server,
            "timezoneId": self.timezone_id,
            "acceptLanguage": self.accept_language,
        }


def egress_from_params(params: dict[str, Any]) -> dict[str, Any]:
    """Egress identity to surface in the solution for IP-binding alignment.

    Enterprise hCaptcha (and any IP-bound token — Turnstile, reCAPTCHA v2) is
    validated against the egress IP that minted it. When the service solves on
    a server-side pool proxy the caller otherwise has no way to know *which*
    egress to submit their downstream request from, so the token is rejected.

    This surfaces:

    * ``proxyKind`` — ``proxyless`` / ``pool_proxy`` / ``task_proxy`` so the
      caller knows whether the token is bound to their own proxy, the server
      pool, or the bare server IP.
    * ``egressServer`` — the proxy gateway (scheme://host:port, credentials
      stripped) used for the solve, so the caller can route their downstream
      submit through the same egress. ``None`` for proxyless solves.

    Both keys are omitted (``None``) when unknown so the solution stays
    backward compatible with YesCaptcha clients that ignore extra fields.
    """
    return {
        "proxyKind": params.get("_proxyKind"),
        "egressServer": params.get("_egress_server"),
    }


def fingerprint_geo_from_params(
    params: dict[str, Any],
) -> tuple[Optional[str], Optional[str]]:
    """Return ``(timezone_id, accept_language)`` stashed on ``params``.

    ``_stash_fingerprint_geo`` (or ``resolve_context_options`` for fresh
    contexts) writes ``_used_timezone`` and ``_used_languages`` onto params
    so any solver can surface them in the solution without re-reading the
    fingerprint. Returns ``(None, None)`` when no fingerprint geo was
    stashed (e.g. tests that mock ``_acquire_context``).
    """
    tz = params.get("_used_timezone")
    langs = params.get("_used_languages") or []
    accept = ", ".join(langs) if langs else None
    return tz, accept
