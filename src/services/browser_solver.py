"""Shared browser-solver helpers for context acquisition and proxy categories."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import Browser

from ..assets.proxy_pool import proxy_from_params
from ..assets.session_pool import PROXYLESS_KEY
from ..core.config import Config
from .browser import BrowserManager
from .captcha_errors import CaptchaError, TokenRejectedError

# Egress helpers live in ``egress.py`` (pure, no Playwright dependency) and are
# re-exported here so existing ``from .browser_solver import egress_from_params``
# imports keep working.
from .egress import (  # noqa: F401
    ProxyKind,
    SolveIdentity,
    egress_from_params,
    fingerprint_geo_from_params,
    has_task_proxy,
    initial_proxy_kind,
    proxy_ip_from_params,
)

log = logging.getLogger(__name__)


def _credentialed_egress_url(asset: Any) -> Optional[str]:
    """Build a ``scheme://user:pass@host:port`` URL from a proxy asset.

    Uses ``playwright_proxy()`` so a sticky-session ``{session}`` placeholder is
    substituted to the SAME exit IP the solve used. Returns ``None`` when the
    asset has no usable server. Only called when the operator opts in via
    ``POOL_EGRESS_EXPOSE_CREDENTIALS`` — a pool proxy's credentials are a server
    secret, but a caller with an IP-bound token needs them to route their
    downstream submit through the identical egress.
    """
    pw = asset.playwright_proxy() if asset is not None else None
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
    return urlunsplit(
        (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
    )


class SolveStage(str, Enum):
    """The stage a browser solve has reached, tracked on ``params["_phase"]``.

    Makes the previously-implicit staging first-class: an attempt stamps the
    stage it reaches as it progresses (acquire → page load → passive poll →
    interaction → visual challenge), so the retry loop can report *where* a
    solve failed and drive stage-aware escalation — e.g. only escalate the
    expensive vision tier when the failure actually reached the visual
    challenge, rather than burning a cloud-model retry on a page that never
    loaded.
    """

    ACQUIRE = "acquire"
    PAGE_LOAD = "page_load"
    PASSIVE = "passive"
    INTERACTION = "interaction"
    CHALLENGE = "challenge"
    VERIFY = "verify"


@dataclass
class SolveContext:
    context: Any
    user_agent: str
    proxy_kind: ProxyKind
    session: Any | None = None
    proxy_id: str | None = None
    session_id: str | None = None


class BaseBrowserSolver:
    """Common lifecycle and context handling for Playwright-backed solvers."""

    def __init__(
        self,
        config: Config,
        manager: BrowserManager | None = None,
        browser: Browser | None = None,
        services: Any | None = None,
    ) -> None:
        self._config = config
        self._manager = manager or BrowserManager(config)
        self._owns_manager = manager is None
        if browser is not None:
            self._manager._browser = browser  # type: ignore[attr-defined]
        self._services = services

    async def start(self) -> None:
        if self._owns_manager:
            await self._manager.start()

    async def stop(self) -> None:
        if self._owns_manager:
            await self._manager.stop()
        log.info("%s stopped", self.__class__.__name__)

    async def _acquire_context(self, params: dict[str, Any]) -> SolveContext:
        """Acquire a browser context and stamp the final proxy category.

        The egress mode is selected by ``params["egress"]``:

        * ``"auto"`` (default): caller task proxy → server-side pool proxy →
          server egress IP. Pool-proxy solves reuse a warm session bound to the
          sticky proxy; proxyless solves reuse a warm server-IP session.
        * ``"task"``: require a caller-supplied proxy and bind a fresh context
          to it. Raises if no task proxy was provided.
        * ``"pool"``: require a server-side pool proxy (sticky warm session).
          Raises if the pool is empty.
        * ``"proxyless"``: ignore any caller proxy and any pool proxy; use a
          warm session if available, else a fresh server-IP context.
        """

        egress = params.get("egress") or "auto"

        if egress == "task":
            return await self._acquire_task(params)
        if egress == "pool":
            return await self._acquire_pool(params)
        if egress == "proxyless":
            return await self._acquire_proxyless(params)
        # auto: task proxy → pool proxy → proxyless (server egress).
        if has_task_proxy(params):
            return await self._acquire_task(params)
        if self._pool_has_available():
            try:
                return await self._acquire_pool(params)
            except RuntimeError as exc:
                if "egress=pool" not in str(exc):
                    raise
                # Pool emptied between ``has_available`` and ``checkout``:
                # fall through to the proxyless path rather than failing.
        return await self._acquire_proxyless(params)

    def _pool_has_available(self) -> bool:
        """Sync peek: is there a server-side proxy pool with available proxies?"""
        if self._services is None:
            return False
        proxy_pool = getattr(self._services, "proxy_pool", None)
        return proxy_pool is not None and proxy_pool.has_available()

    def _stash_fingerprint_geo(
        self, solve_context: SolveContext, params: dict[str, Any]
    ) -> None:
        """Stash the solve's fingerprint timezone / languages onto ``params``.

        Warm-session solves carry the fingerprint on ``solve_context.session``
        (``BrowserSession.fingerprint``); fresh-context solves already had it
        stashed by ``resolve_context_options``. This helper unifies the two
        paths so the solver's ``solve()`` can read
        ``params["_used_timezone"]`` / ``params["_used_languages"]`` and
        surface them in the solution (``SolutionObject.timezoneId`` /
        ``acceptLanguage``) for callers to align their submit context.
        """
        session = getattr(solve_context, "session", None)
        fp = getattr(session, "fingerprint", None) if session is not None else None
        if fp is not None:
            params["_used_timezone"] = fp.timezone_id
            params["_used_languages"] = list(fp.languages)
            # Touch modality follows the fingerprint: a mobile session drives
            # touch taps / scroll motion instead of mouse events.
            params["_is_mobile"] = bool(fp.is_mobile)
        # Fresh-context path: resolve_context_options already stashed them.

    async def _acquire_task(self, params: dict[str, Any]) -> SolveContext:
        """Bind a fresh context to the caller-supplied proxy (no session reuse)."""
        if not has_task_proxy(params):
            raise RuntimeError(
                "egress=task requires a caller-supplied proxy "
                "(proxy / proxyAddress+proxyPort fields)"
            )
        params["_proxyKind"] = ProxyKind.TASK_PROXY.value
        task_asset = proxy_from_params(params)
        if task_asset is not None:
            # Credential-free gateway (scheme://host:port) surfaced back to the
            # caller so they can align their downstream submit egress.
            params["_egress_server"] = task_asset.server
        context, user_agent = await self._manager.new_context(params)
        return SolveContext(
            context=context,
            user_agent=user_agent,
            proxy_kind=ProxyKind.TASK_PROXY,
        )

    async def _acquire_pool(self, params: dict[str, Any]) -> SolveContext:
        """Check out a sticky pool proxy and reuse (or build) a warm session for it.

        Requires a non-empty server-side proxy pool. Raises if no pool proxy is
        available. When a ``SessionPool`` is wired, the pool proxy is paired
        with a warm session bound to that proxy's bucket (so the same sticky
        proxy keeps a coherent fingerprint + cookie jar across solves).
        Otherwise a fresh context is bound to the proxy via
        ``_proxy_override``.

        The proxy's exit-IP geo (``timezone`` / ``locale`` / ``country``) is
        stashed onto ``params`` as ``_pool_geo`` and its ``id`` as
        ``_proxy_seed`` so a fresh-context build (no session pool) can produce
        a fingerprint aligned with the proxy's egress. Warm sessions seed the
        fingerprint themselves inside ``SessionPool.checkout``.
        """
        proxy_pool = (
            getattr(self._services, "proxy_pool", None)
            if self._services is not None
            else None
        )
        if proxy_pool is None:
            raise RuntimeError(
                "egress=pool requires a server-side proxy but the pool is empty"
            )
        # WP5: a caller may require a specific proxy kind (e.g. enterprise
        # hCaptcha forces residential-or-mobile). ``_required_proxy_kinds`` is
        # the preferred form (a list/tuple of accepted kinds); the legacy
        # ``_required_proxy_kind`` (single str) is wrapped into a 1-tuple for
        # uniform handling. ``None`` falls back to any kind.
        required_kinds = params.get("_required_proxy_kinds")
        if required_kinds is None:
            legacy = params.get("_required_proxy_kind")
            required_kinds = (legacy,) if legacy else None
        pool_proxy = await proxy_pool.checkout(
            kind=required_kinds, sitekey=params.get("websiteKey")
        )
        if pool_proxy is None:
            if required_kinds:
                kinds_str = " or ".join(required_kinds)
                raise RuntimeError(
                    f"egress=pool requires a {kinds_str} pool proxy but "
                    "none is available"
                )
            raise RuntimeError(
                "egress=pool requires a server-side proxy but the pool is empty"
            )
        params["_pool_proxy_id"] = pool_proxy.id
        params["_proxyKind"] = ProxyKind.POOL_PROXY.value
        # Gateway of the checked-out pool proxy surfaced so the caller can route
        # their downstream (IP-bound) submit through the same egress. Default is
        # credential-free (the pool proxy is a server secret); when the operator
        # opts in via POOL_EGRESS_EXPOSE_CREDENTIALS we surface the full
        # credentialed URL (sticky session substituted) so the caller can
        # actually reach the SAME exit IP — otherwise an enterprise/Stripe
        # token minted here is bound to an IP the caller can never reproduce.
        params["_egress_server"] = pool_proxy.server
        if getattr(self._config, "pool_egress_expose_credentials", False):
            full = _credentialed_egress_url(pool_proxy)
            if full:
                params["_egress_server"] = full
        # WP-geo: if the proxy has no geo annotation, probe its exit IP once
        # (through the proxy itself) and cache the derived timezone/locale so a
        # German exit IP presents Europe/Berlin + de-DE instead of a random
        # locale. Runs before the fingerprint is built (fresh context) or the
        # warm session is checked out (which seeds its fingerprint from the
        # proxy geo), so both paths pick up the probed geo.
        await self._maybe_probe_geo(proxy_pool, pool_proxy)
        # WP3: thread the proxy's exit-IP geo + a deterministic seed so a
        # fresh-context build (no session pool) produces a fingerprint
        # aligned with the proxy's egress. Warm sessions read these from the
        # proxy directly inside ``SessionPool.checkout``.
        params["_pool_geo"] = {
            "timezone": pool_proxy.timezone,
            "locale": pool_proxy.locale,
            "country": pool_proxy.country,
        }
        params["_proxy_seed"] = pool_proxy.id
        # P1-4: a mobile pool proxy drives an Android Chrome fingerprint (a
        # carrier IP with a desktop fingerprint is a contradiction). Read by
        # resolve_context_options for the fresh-context path; the warm-session
        # path reads proxy.kind directly in SessionPool.checkout.
        params["_pool_proxy_mobile"] = pool_proxy.kind == "mobile"

        session_pool = (
            getattr(self._services, "session_pool", None)
            if self._services is not None
            else None
        )
        # Enterprise solves can force a fresh context per solve (bypassing warm
        # session reuse) so a single sticky proxy's cookie jar / fingerprint
        # isn't reused across different sitekeys — reusing one warm session to
        # hammer the same sitekey builds a suspicious pattern enterprise risk
        # models cluster on. ``_force_fresh_context`` is set by the hCaptcha
        # solver for enterprise variants when ``ENTERPRISE_FRESH_CONTEXT`` is on.
        if session_pool is not None and not params.get("_force_fresh_context"):
            session = await session_pool.checkout(
                key=pool_proxy.id, proxy=pool_proxy, sitekey=params.get("websiteKey")
            )
            params["_sessionId"] = session.id
            return SolveContext(
                context=session.context,
                user_agent=session.user_agent,
                proxy_kind=ProxyKind.POOL_PROXY,
                session=session,
                proxy_id=pool_proxy.id,
                session_id=session.id,
            )

        # No session pool: fall back to a fresh context bound to the proxy.
        pw_proxy = pool_proxy.playwright_proxy()
        if pw_proxy:
            params["_proxy_override"] = pw_proxy
        context, user_agent = await self._manager.new_context(params)
        return SolveContext(
            context=context,
            user_agent=user_agent,
            proxy_kind=ProxyKind.POOL_PROXY,
            proxy_id=pool_proxy.id,
        )

    async def _maybe_probe_geo(self, proxy_pool: Any, pool_proxy: Any) -> None:
        """Probe + cache a pool proxy's exit-IP geo when it has none.

        Best-effort and one-shot per proxy (guarded by ``geo_probed``). A
        resolved country derives timezone/locale via the shared table and is
        persisted through ``proxy_pool.set_geo`` so later checkouts (including
        on other workers via Redis) skip the probe. Any failure is swallowed:
        the proxy keeps unset geo and the fingerprint falls back to a random
        coherent identity (pre-existing behaviour).
        """
        if not getattr(self._config, "proxy_geo_probe", False):
            return
        if getattr(pool_proxy, "country", None) or getattr(
            pool_proxy, "geo_probed", False
        ):
            return
        set_geo = getattr(proxy_pool, "set_geo", None)
        if set_geo is None:
            return
        try:
            from ..assets.geo_probe import probe_proxy_geo

            url = getattr(
                self._config, "proxy_geo_probe_url", "http://ip-api.com/json"
            )
            await probe_proxy_geo(pool_proxy, url=url)
            await set_geo(
                pool_proxy.id,
                country=pool_proxy.country,
                timezone=pool_proxy.timezone,
                locale=pool_proxy.locale,
                geo_probed=True,
            )
        except Exception as exc:  # noqa: BLE001 - probe must never fail a solve
            log.debug("proxy geo probe failed for %s: %s", pool_proxy.id, exc)

    async def _acquire_proxyless(
        self, params: dict[str, Any]
    ) -> SolveContext:
        """Use a warm server-IP session if available, else a fresh context."""
        params["_proxyKind"] = ProxyKind.PROXYLESS.value
        if self._services is not None:
            session_pool = getattr(self._services, "session_pool", None)
            if session_pool is not None:
                session = await session_pool.checkout(
                    key=PROXYLESS_KEY,
                    proxy=None,
                    sitekey=params.get("websiteKey"),
                )
                params["_sessionId"] = session.id
                return SolveContext(
                    context=session.context,
                    user_agent=session.user_agent,
                    proxy_kind=ProxyKind.PROXYLESS,
                    session=session,
                    session_id=session.id,
                )
        context, user_agent = await self._manager.new_context(params)
        return SolveContext(
            context=context,
            user_agent=user_agent,
            proxy_kind=ProxyKind.PROXYLESS,
        )

    async def _release_context(
        self, solve_context: SolveContext, solved: bool, params: dict[str, Any]
    ) -> None:
        """Return or close the browser context and report proxy-pool health.

        For warm sessions (pool or proxyless) the context outlives the solve:
        release it back to the session pool, which handles retirement based on
        reputation / max_solves. Per-solve byte attribution is skipped — a
        warm session accumulates bytes across solves that aren't meaningfully
        attributable to a single task. For pool sessions, the pool proxy is
        reported per solve so the proxy-pool health tracker stays current.

        For fresh contexts (task_proxy or proxyless/pool fallback when no
        session pool is wired) the per-context byte counter (set by
        BrowserManager's response listener) is read out before close so the
        proxy pool and ledger can attribute bandwidth to this solve. For
        task_proxy there is no pool proxy to report; for the pool-fallback
        fresh path the pool proxy is reported with the bytes.
        """
        bytes_used = 0
        if solve_context.session is None:
            # Fresh context (task_proxy / pool-fallback / proxyless-fallback):
            # read the accumulated byte count before closing.
            bytes_used = int(getattr(solve_context.context, "_omc_bytes_used", 0))
            params["_proxy_bytes"] = bytes_used
            try:
                await solve_context.context.close()
            except Exception:
                pass
        else:
            # Warm session (pool or proxyless): release back to the session
            # pool; per-solve byte attribution is skipped (the context outlives
            # this solve). Pool-proxy health is still reported per solve below.
            if self._services is not None:
                await self._services.session_pool.release(
                    solve_context.session, success=solved, burned=not solved
                )

        proxy_id = solve_context.proxy_id or params.pop("_pool_proxy_id", None)
        if proxy_id and self._services is not None:
            proxy_pool = getattr(self._services, "proxy_pool", None)
            if proxy_pool is not None:
                await proxy_pool.report(proxy_id, success=solved, bytes_used=bytes_used)
                sitekey = params.get("websiteKey")
                if sitekey:
                    await proxy_pool.report_sitekey(proxy_id, sitekey, success=solved)

    async def _solve_with_retries(
        self,
        params: dict[str, Any],
        *,
        sitekey: str,
        client_key: Optional[str],
        attempt_fn: "Callable[[], Awaitable[tuple[str, str]]]",
        build_solution: "Callable[[str, str], dict[str, Any]]",
        provider: str,
        default_task_type: str,
        default_challenge_shape: str = "widget",
        on_error: "Optional[Callable[[int, Exception, str], None]]" = None,
        verify_provider: Optional[str] = None,
    ) -> dict[str, Any]:
        """Shared attempt loop for every browser solver (WP-pipeline).

        Collapses the four near-identical retry loops (hCaptcha / Turnstile /
        reCAPTCHA v2 / v3) into one template: per-attempt timing + record,
        classified-error fast-fail (a non-retryable :class:`CaptchaError` such
        as a rate limit raises immediately instead of hammering the egress),
        exponential backoff with jitter between retryable attempts, and
        solution assembly (token + UA + fingerprint geo + egress echo). The
        provider-specific bits are injected:

        * ``attempt_fn`` runs one solve attempt → ``(token, user_agent)``.
        * ``build_solution`` maps ``(token, user_agent)`` to the provider's
          solution dict (e.g. ``gRecaptchaResponse`` vs ``token``).
        * ``on_error`` is an optional hook (attempt index, exc, reached stage)
          for per-provider stage-aware escalation (hCaptcha bumps its vision
          tier only when the failure reached the visual challenge).
        """
        last_error: Exception | None = None
        retries = int(self._config.captcha_retries)
        for attempt in range(retries):
            started = time.monotonic()
            # Reset the stage tracker for this attempt; the attempt stamps
            # ``params["_phase"]`` as it advances so a failure knows where it
            # stopped (see SolveStage / stage-aware on_error below).
            params["_phase"] = SolveStage.ACQUIRE.value
            try:
                token, user_agent = await attempt_fn()
                await self._record(
                    params,
                    sitekey,
                    client_key,
                    "ready",
                    started,
                    task_type=default_task_type,
                    challenge_shape=default_challenge_shape,
                )
                # WP6 token-trust closure: when siteverify is configured for
                # this sitekey, verify the token now and feed the verdict into
                # the real-outcome accounting (same buckets /reportIncorrect
                # writes). A definitive rejection retries on a fresh egress
                # rather than returning a token the provider already refused.
                verdict = await self._verify_and_close(
                    params, sitekey, client_key, token, verify_provider
                )
                if verdict is False:
                    last_error = TokenRejectedError(
                        f"{provider} token rejected by siteverify"
                    )
                    log.warning(
                        "%s attempt %d/%d: token rejected by siteverify",
                        provider,
                        attempt + 1,
                        retries,
                    )
                    if attempt < retries - 1:
                        await self._sleep_backoff(attempt)
                    continue
                # The solve's resolved identity (egress + fingerprint geo) as one
                # immutable object rather than four loose params reads. Surfaced
                # in the solution so the caller can align their downstream submit
                # egress + browser context with the context that minted the token.
                identity = SolveIdentity.from_params(params)
                solution = build_solution(token, user_agent)
                solution.setdefault("userAgent", user_agent)
                solution.update(identity.solution_fields())
                return solution
            except CaptchaError as exc:
                last_error = exc
                await self._record(
                    params,
                    sitekey,
                    client_key,
                    exc.outcome,
                    started,
                    task_type=default_task_type,
                    challenge_shape=default_challenge_shape,
                )
                stage = params.get("_phase", SolveStage.ACQUIRE.value)
                log.warning(
                    "%s attempt %d/%d: %s (retryable=%s, stage=%s)",
                    provider,
                    attempt + 1,
                    retries,
                    exc,
                    exc.retryable,
                    stage,
                )
                if not exc.retryable:
                    raise
                if on_error is not None:
                    on_error(attempt, exc, stage)
                if attempt < retries - 1:
                    await self._sleep_backoff(attempt)
            except Exception as exc:
                last_error = exc
                await self._record(
                    params,
                    sitekey,
                    client_key,
                    "failed",
                    started,
                    task_type=default_task_type,
                    challenge_shape=default_challenge_shape,
                )
                stage = params.get("_phase", SolveStage.ACQUIRE.value)
                log.warning(
                    "%s attempt %d/%d failed at stage=%s: %s",
                    provider,
                    attempt + 1,
                    retries,
                    stage,
                    exc,
                )
                if on_error is not None:
                    on_error(attempt, exc, stage)
                if attempt < retries - 1:
                    await self._sleep_backoff(attempt)

        raise RuntimeError(
            f"{provider} failed after {retries} attempts: {last_error}"
        )

    async def _sleep_backoff(self, attempt: int) -> None:
        """Exponential backoff with jitter between retryable attempts.

        Replaces the fixed 2s sleep: ``base * 2**attempt`` capped at
        ``retry_backoff_max``, plus up to 25% jitter so concurrent solves that
        all fail at once don't retry in lockstep (thundering herd). Rate-limit
        errors are non-retryable and never reach here — they fail fast.
        """
        base = float(getattr(self._config, "retry_backoff_base", 1.0))
        cap = float(getattr(self._config, "retry_backoff_max", 8.0))
        delay = min(cap, base * (2 ** attempt))
        delay += random.uniform(0.0, delay * 0.25)
        await asyncio.sleep(delay)

    async def _record(
        self,
        params: dict[str, Any],
        sitekey: str,
        client_key: Optional[str],
        outcome: str,
        started: float,
        *,
        task_type: str | None = None,
        challenge_shape: str = "widget",
    ) -> None:
        """Append a SolveRecord to the shared ledger and update accounting.

        Reads vision stats from params["_vision"] when present (hCaptcha/Turnstile
        set it after challenge dispatch) and phase timings stashed on params
        (``_phase_page_load_ms`` / ``_phase_challenge_ms``) so the ledger can
        break ``wall_ms`` down into page-load / challenge / vision time. Metering
        failures are swallowed so they never fail a solve.
        """
        if self._services is None:
            return
        from ..consumption.ledger import SolveRecord, estimate_cost

        vision = params.get("_vision")
        model = getattr(vision, "last_model", None)
        in_tok = getattr(vision, "total_input_tokens", 0) or 0
        out_tok = getattr(vision, "total_output_tokens", 0) or 0
        calls = getattr(vision, "total_vision_calls", 0) or 0
        vision_ms = int(getattr(vision, "total_vision_ms", 0) or 0)
        cost = estimate_cost(model or "local", in_tok, out_tok)
        # One typed read of the solve's egress/session identity instead of
        # three separate ``params.get("_...")`` lookups spread across the record.
        identity = SolveIdentity.from_params(params)

        try:
            await self._services.ledger.record(
                SolveRecord(
                    task_id=str(params.get("_taskId") or ""),
                    sitekey=sitekey,
                    task_type=task_type or params.get("type", "unknown"),
                    proxy_id=identity.proxy_id,
                    session_id=identity.session_id,
                    proxy_kind=identity.proxy_kind,
                    model=model,
                    # hCaptcha stashes the detected shape; fall back to the
                    # provider default (e.g. "widget" / "audio").
                    challenge_shape=params.get("_challenge_shape") or challenge_shape,
                    vision_calls=calls,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    proxy_bytes=int(params.get("_proxy_bytes", 0)),
                    wall_ms=int((time.monotonic() - started) * 1000),
                    page_load_ms=int(params.get("_phase_page_load_ms", 0) or 0),
                    challenge_ms=int(params.get("_phase_challenge_ms", 0) or 0),
                    vision_ms=vision_ms,
                    outcome=outcome,
                    est_cost_usd=cost,
                    client_key=client_key,
                )
            )
            await self._services.accounting.record(
                sitekey,
                outcome,
                proxy_kind=identity.proxy_kind,
                model=model,
            )
        except Exception as exc:
            log.debug("ledger record failed: %s", exc)

    async def _verify_and_close(
        self,
        params: dict[str, Any],
        sitekey: str,
        client_key: Optional[str],
        token: str,
        provider_key: Optional[str],
    ) -> Optional[bool]:
        """Verify the token via siteverify and close the real-outcome loop.

        Returns the tri-state verdict: ``True`` accepted, ``False`` rejected,
        ``None`` unknown (no verifier wired, no secret for the sitekey, or a
        transient verify error). A definitive verdict is fed into the same
        real-outcome path ``/reportCorrect`` uses so proxy health / routing
        learn from ground truth automatically. ``None`` leaves the loop
        caller-driven (pre-WP6 behaviour).
        """
        if self._services is None or not provider_key:
            return None
        verifier = getattr(self._services, "token_verifier", None)
        if verifier is None:
            return None
        try:
            verdict = await verifier.verify(
                token,
                provider=provider_key,
                sitekey=sitekey,
                remote_ip=self._proxy_ip(params),
            )
        except Exception as exc:  # noqa: BLE001 - verify never fails a solve
            log.debug("token verification raised: %s", exc)
            return None
        if verdict is None:
            return None
        await self._close_real_outcome(params, sitekey, success=verdict)
        return verdict

    async def _close_real_outcome(
        self, params: dict[str, Any], sitekey: str, *, success: bool
    ) -> None:
        """Feed a real (siteverify) outcome into accounting + proxy + session.

        Delegates to the shared :func:`~src.services.outcome.record_real_outcome`
        module — the same fan-out ``/reportCorrect`` // ``/reportIncorrect`` uses
        — driven from the live solve's :class:`SolveIdentity` instead of a stored
        ledger record. Every downstream call is guarded there so verification can
        never fail a solve.
        """
        if self._services is None:
            return
        from .outcome import record_real_outcome

        identity = SolveIdentity.from_params(params)
        model = getattr(params.get("_vision"), "last_model", None)
        await record_real_outcome(
            self._services, identity, sitekey, success=success, model=model
        )

    def _proxy_ip(self, params: dict[str, Any]) -> Optional[str]:
        return proxy_ip_from_params(params)

    async def _human_click_in_frame(
        self,
        page: Any,
        frame_locator: Any,
        selector: str,
        *,
        timeout_ms: int = 10_000,
        touch: bool = False,
    ) -> None:
        """Click an element inside an iframe with a human-like pointer path.

        hCaptcha / Turnstile collect ``motionData`` from the moment the widget
        loads, and the checkbox click dynamics are scored too — a raw
        ``locator.click()`` teleports the pointer with zero travel/dwell, one of
        the strongest automation tells. This reuses :func:`human_click` (the
        same eased/jittered path the tile clicks already use): a
        ``FrameLocator``'s ``bounding_box()`` returns page-relative coordinates,
        so moving ``page.mouse`` along a path to that box lands the click
        correctly. Falls back to ``locator.click()`` when humanisation is
        disabled, the geometry can't be read, or the page has no usable mouse
        (tests / invisible widgets).
        """
        # ``.first`` so a selector matching multiple elements (e.g. Turnstile's
        # ``input[type="checkbox"], label``) resolves to a single element for
        # both bounding_box() and click() (matches the pre-P1-5 ``.first`` call).
        locator = frame_locator.locator(selector).first
        if getattr(self._config, "human_mouse_enabled", True):
            try:
                from ..parsing.shapes.human_cursor import human_click, human_tap_box

                box = await locator.bounding_box()
                if box:
                    jitter = float(
                        getattr(self._config, "human_mouse_jitter_ms", 90)
                    )
                    # Mobile fingerprint → trusted touch tap; else eased mouse
                    # path. A mouse click on a phone context contradicts the
                    # touch-capable fingerprint hCaptcha mobile scores.
                    if touch:
                        tapped = await human_tap_box(page, box)
                        if tapped is not None:
                            return
                    result = await human_click(page, box, jitter_ms=jitter)
                    if result is not None:
                        return
            except Exception as exc:  # noqa: BLE001 - fall back to plain click
                log.debug("human checkbox click failed, falling back: %s", exc)
        await locator.click(timeout=timeout_ms)

    async def _human_mouse(
        self, page: Any, *, seconds: "float | None" = None, touch: bool = False
    ) -> None:
        """Seed a realistic pre-interaction motion timeline (wander + scroll).

        hCaptcha scores a *continuous* ``motionData`` timeline. The previous
        warmup was a single ~0.5s eased move, which leaves an almost-empty
        motion buffer — a strong "automation / invisible-every-time" tell. This
        traces several eased sub-movements to random on-viewport points with
        human dwell between them (see :func:`human_wander`), plus an occasional
        small scroll, so the buffer looks like a human landing on and reading a
        page. Gated by ``human_mouse_enabled`` (off in tests / when a caller
        wants a deterministic no-motion solve).

        ``touch=True`` (a mobile fingerprint) fills the window with scroll
        gestures + dwell instead — a phone has no hovering cursor, so tracing
        ``mouse.move`` on a touch context would itself be a contradiction.

        ``seconds`` overrides the wander duration for the passive window; the
        default is ``config.human_passive_motion_seconds``.
        """
        if not getattr(self._config, "human_mouse_enabled", True):
            return
        from ..parsing.shapes.human_cursor import human_touch_scroll, human_wander

        budget = (
            seconds
            if seconds is not None
            else float(getattr(self._config, "human_passive_motion_seconds", 1.4))
        )
        if touch:
            await human_touch_scroll(page, seconds=budget)
            return
        jitter = float(getattr(self._config, "human_mouse_jitter_ms", 80))
        viewport = self._viewport_size(page)
        cursor = await human_wander(
            page, seconds=budget, viewport=viewport, jitter_ms=jitter
        )
        # An occasional short scroll — a human settling onto a page usually
        # nudges the wheel. Best-effort; a fake page without wheel is a no-op.
        if cursor is not None and random.random() < 0.5:
            try:
                await page.mouse.wheel(0, random.randint(60, 240))
                await asyncio.sleep(random.uniform(0.15, 0.5))
            except Exception:  # noqa: BLE001 - scroll is best-effort
                pass

    @staticmethod
    def _viewport_size(page: Any) -> "tuple[int, int] | None":
        """Best-effort ``(width, height)`` of the page viewport, else ``None``."""
        try:
            vp = getattr(page, "viewport_size", None)
            if isinstance(vp, dict) and vp.get("width") and vp.get("height"):
                return int(vp["width"]), int(vp["height"])
        except Exception:  # noqa: BLE001
            pass
        return None
