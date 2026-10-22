"""YesCaptcha / AntiCaptcha compatible API models."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger(__name__)

# ``CLIENT_KEY`` is operator-configured and may be a long random secret; the
# request bound only guards against oversized bodies, not key strength.
_MAX_CLIENT_KEY_CHARS = 512

# A base64-encoded captcha image around 7 MB occupies ~10 M characters. Bound
# both the number of images and each image so one request can't pin an
# arbitrarily large body in memory before validation runs.
_MAX_IMAGE_CHARS = 10_000_000
_MAX_IMAGES = 20

_ImageString = Annotated[str, Field(max_length=_MAX_IMAGE_CHARS)]


# ── createTask ──────────────────────────────────────────────

class TaskObject(BaseModel):
    # Every property the solvers consume is declared below (see
    # docs/api-reference.md and docs/usage/), so an unknown key is a client
    # mistake (typo / unsupported YesCaptcha field) rather than something to
    # silently drop: reject it instead of producing a confusing downstream
    # failure. ``extra="allow"`` would also let unvalidated data reach solvers.
    model_config = ConfigDict(extra="forbid")

    type: str
    websiteURL: str | None = Field(default=None, max_length=2048)
    websiteKey: str | None = Field(default=None, max_length=256)
    pageAction: str | None = None
    minScore: float | None = None
    isInvisible: bool | None = None

    # ── Cloudflare Turnstile advanced params ──
    # Widgets configured with an action / cData / chlPageData must receive the
    # exact same values, otherwise the generated token is rejected server-side.
    action: str | None = None
    cData: str | None = None
    chlPageData: str | None = None

    # ── hCaptcha enterprise params ──
    # `rqdata` (a.k.a. enterprise "data") binds the token to a challenge nonce.
    rqdata: str | None = None
    enterprisePayload: dict[str, object] | None = None
    # Per-task override for hCaptcha page strategy. None => process default
    # (HCAPTCHA_REAL_PAGE). True navigates the real target and hooks
    # `hcaptcha.render` (runs the site's own anti-bot JS); False serves the
    # synthetic injected page at the target hostname (token-relay: faster and
    # doesn't depend on reproducing the target's session).
    realPage: bool | None = None

    # ── Fingerprint / session binding ──
    # Callers may force a specific User-Agent so the token they submit downstream
    # matches the one used during solving. If omitted the solver picks a default
    # and echoes it back in solution.userAgent.
    userAgent: str | None = None

    # ── Proxy (YesCaptcha-style fields) ──
    # CF / reCAPTCHA tokens are IP-bound; solving through the same egress IP the
    # caller will submit from is required for the token to validate.
    proxyType: str | None = None
    proxyAddress: str | None = None
    proxyPort: int | None = None
    proxyLogin: str | None = None
    proxyPassword: str | None = None
    # Convenience single-string form: "http://user:pass@host:port"
    proxy: str | None = None

    # Egress intent: "auto" (default, current behavior), "proxyless" (force server
    # egress, refuse task proxy), "pool" (force server-side proxy pool, fail if
    # empty), "task" (require caller-supplied proxy, fail if none). Used by the
    # scheduler to pick the right concurrency pool and by the solver to enforce
    # the caller's egress requirement.
    egress: str | None = None

    # Image captcha / classification fields
    body: str | None = None
    image: str | None = None
    images: Annotated[list[_ImageString], Field(max_length=_MAX_IMAGES)] | None = None
    question: str | None = None
    queries: (
        Annotated[list[_ImageString], Field(max_length=_MAX_IMAGES)] | str | None
    ) = None
    project_name: str | None = None


class CreateTaskRequest(BaseModel):
    clientKey: str = Field(max_length=_MAX_CLIENT_KEY_CHARS)
    task: TaskObject
    # Optional idempotency key: retries carrying the same key coalesce onto the
    # same task instead of launching a second solve (no double-spend of proxies /
    # model calls).
    idempotencyKey: str | None = Field(default=None, max_length=128)


class CreateTaskResponse(BaseModel):
    errorId: int = 0
    taskId: str | None = None
    errorCode: str | None = None
    errorDescription: str | None = None


# ── getTaskResult ───────────────────────────────────────────

class GetTaskResultRequest(BaseModel):
    clientKey: str = Field(max_length=_MAX_CLIENT_KEY_CHARS)
    taskId: str


class SolutionObject(BaseModel):
    gRecaptchaResponse: str | None = None
    text: str | None = None
    token: str | None = None
    objects: list[int] | None = None
    answer: bool | list[int] | None = None
    userAgent: str | None = None
    # WP3: fingerprint geo used for the solve, surfaced so callers can align
    # their submit context (Accept-Language header / TZ) with the solve
    # context. Optional — omitted when the solver didn't pin a fingerprint.
    timezoneId: str | None = None
    acceptLanguage: str | None = None
    # Egress identity of the solve, surfaced so callers can submit an IP-bound
    # token (enterprise hCaptcha / Turnstile / reCAPTCHA v2) from the SAME
    # egress that minted it. ``proxyKind`` is proxyless / pool_proxy /
    # task_proxy; ``egressServer`` is the credential-free proxy gateway
    # (scheme://host:port) or null for proxyless / server-egress solves.
    proxyKind: str | None = None
    egressServer: str | None = None
    # Non-fatal, caller-facing caveats about a solve the service can't enforce
    # itself — e.g. an enterprise hCaptcha token can't be server-verified
    # (no siteverify secret) and is IP-bound, so the caller must submit from the
    # same egress and confirm acceptance downstream. Omitted when empty.
    warnings: list[str] | None = None


class GetTaskResultResponse(BaseModel):
    errorId: int = 0
    status: str | None = None
    solution: SolutionObject | None = None
    errorCode: str | None = None
    errorDescription: str | None = None


# ── solve-attempt state ─────────────────────────────────────

@dataclass
class AttemptContext:
    """Typed state owned by ONE solve attempt.

    Previously every attempt-scoped fact (the checked-out proxy / warm-session
    ids, the resolved egress echo, the fingerprint geo, the vision usage
    accumulator, the per-attempt byte counter, the stage tracker and its
    timings, the detected challenge shape, whether proxy health was already
    reported) lived as loose ``_``-prefixed keys on the mutable ``params`` dict
    shared by every attempt. The retry loop had to clear a hardcoded key list
    at the top of each attempt to stop attempt 2 from reporting attempt 1's
    spend or echoing a released egress.

    This object replaces that bookkeeping: a fresh instance is created per
    retry attempt (see ``BaseBrowserSolver._solve_with_retries``), the solve
    reads and writes typed fields on it, and :meth:`sync_to_params` mirrors it
    back onto the legacy ``params`` keys at the attempt's release point. The
    mirror keeps the other solvers (hCaptcha / Turnstile / injected widget)
    and the shared ledger recorder working unchanged — they still read/write
    ``params["_vision"]`` & friends.
    """

    #: Zero-based retry index this context belongs to (diagnostics only).
    attempt: int = 0

    # ── egress binding ──
    proxy_kind: str | None = None
    egress_server: str | None = None

    # ── proxy lease / ids ──
    proxy_id: str | None = None
    session_id: str | None = None
    proxy_override: dict[str, Any] | None = None
    pool_geo: dict[str, Any] | None = None
    proxy_seed: str | None = None
    pool_proxy_mobile: bool = False

    # ── fingerprint ──
    timezone_id: str | None = None
    languages: list[str] | None = None
    is_mobile: bool = False

    # ── usage / transport accounting ──
    vision: Any | None = None
    proxy_bytes: int = 0

    # ── phase tracking ──
    phase: str | None = None
    phase_page_load_ms: int = 0
    phase_challenge_ms: int = 0

    # ── challenge shape + proxy-health bookkeeping ──
    challenge_shape: str | None = None
    proxy_reported: bool = False

    #: Attempt field -> legacy ``params`` key. This is the only place the
    #: two representations are coupled; adding a field means adding one entry.
    PARAM_KEY_BY_FIELD: ClassVar[dict[str, str]] = {
        "proxy_kind": "_proxyKind",
        "egress_server": "_egress_server",
        "proxy_id": "_pool_proxy_id",
        "session_id": "_sessionId",
        "proxy_override": "_proxy_override",
        "pool_geo": "_pool_geo",
        "proxy_seed": "_proxy_seed",
        "pool_proxy_mobile": "_pool_proxy_mobile",
        "timezone_id": "_used_timezone",
        "languages": "_used_languages",
        "is_mobile": "_is_mobile",
        "vision": "_vision",
        "proxy_bytes": "_proxy_bytes",
        "phase": "_phase",
        "phase_page_load_ms": "_phase_page_load_ms",
        "phase_challenge_ms": "_phase_challenge_ms",
        "challenge_shape": "_challenge_shape",
        "proxy_reported": "_proxy_reported",
    }

    def bind(
        self, params: dict[str, Any] | None = None, **fields: Any
    ) -> "AttemptContext":
        """Set typed fields and (optionally) mirror them onto ``params``.

        Used by the acquire/fingerprint helpers, which keep writing the legacy
        params keys so non-refactored solvers observe the same state they
        always did. With no live attempt context the caller passes ``None`` and
        the helpers degrade to the old params-only behaviour.
        """
        for name, value in fields.items():
            if name not in self.PARAM_KEY_BY_FIELD:
                raise AttributeError(f"unknown AttemptContext field {name!r}")
            setattr(self, name, value)
            if params is not None:
                params[self.PARAM_KEY_BY_FIELD[name]] = value
        return self

    def sync_to_params(self, params: dict[str, Any]) -> None:
        """Mirror every field onto its legacy ``params`` key (attempt end)."""
        for name, key in self.PARAM_KEY_BY_FIELD.items():
            params[key] = getattr(self, name)

    def reset_params(self, params: dict[str, Any]) -> None:
        """Install this fresh attempt's defaults as the legacy params mirror.

        Replaces the old ``ATTEMPT_SCOPED_KEYS`` deletion pass: a brand-new
        context is by construction empty, so overwriting the mirror with its
        defaults can never carry a previous attempt's proxy, spend or timing
        into the next one.
        """
        type(self)(attempt=self.attempt).sync_to_params(params)

    @classmethod
    def from_params(
        cls, params: dict[str, Any], *, attempt: int = 0
    ) -> "AttemptContext":
        """Build a context seeded from whatever the params mirror already holds.

        Used when a real-page solve is invoked outside the retry loop (e.g. a
        direct ``_solve_once`` call) so pre-populated legacy keys are preserved
        rather than discarded. Values are normalised defensively: the mirror is
        untyped, and a ``None`` byte count must not reach an ``int()``.
        """
        def _opt_dict(key: str) -> dict[str, Any] | None:
            value = params.get(key)
            return dict(value) if isinstance(value, dict) else None

        def _opt_list(key: str) -> list[str] | None:
            value = params.get(key)
            if isinstance(value, (list, tuple)):
                return [str(item) for item in value]
            return None

        def _int(key: str) -> int:
            try:
                return max(0, int(params.get(key) or 0))
            except (TypeError, ValueError):
                return 0

        phase = params.get("_phase")
        return cls(
            attempt=attempt,
            proxy_kind=params.get("_proxyKind"),
            egress_server=params.get("_egress_server"),
            proxy_id=params.get("_pool_proxy_id"),
            session_id=params.get("_sessionId"),
            proxy_override=_opt_dict("_proxy_override"),
            pool_geo=_opt_dict("_pool_geo"),
            proxy_seed=params.get("_proxy_seed"),
            pool_proxy_mobile=bool(params.get("_pool_proxy_mobile")),
            timezone_id=params.get("_used_timezone"),
            languages=_opt_list("_used_languages"),
            is_mobile=bool(params.get("_is_mobile")),
            vision=params.get("_vision"),
            proxy_bytes=_int("_proxy_bytes"),
            phase=str(phase) if phase else None,
            phase_page_load_ms=_int("_phase_page_load_ms"),
            phase_challenge_ms=_int("_phase_challenge_ms"),
            challenge_shape=params.get("_challenge_shape"),
            proxy_reported=bool(params.get("_proxy_reported")),
        )


# ── getBalance ──────────────────────────────────────────────

class GetBalanceRequest(BaseModel):
    clientKey: str = Field(max_length=_MAX_CLIENT_KEY_CHARS)


class GetBalanceResponse(BaseModel):
    errorId: int = 0
    balance: float = 99999.0


# ── reportCorrect / reportIncorrect ─────────────────────────

class ReportTaskRequest(BaseModel):
    clientKey: str = Field(max_length=_MAX_CLIENT_KEY_CHARS)
    taskId: str


class ReportTaskResponse(BaseModel):
    errorId: int = 0
    errorCode: str | None = None
    errorDescription: str | None = None


# ── log redaction ───────────────────────────────────────────

# Any field whose lower-cased name contains one of these is dropped outright.
_SENSITIVE_KEY_MARKERS = ("password", "secret", "token")
# Exact names (lower-cased) that carry a credential but don't match a marker.
_SENSITIVE_EXACT_KEYS = frozenset({"_clientkey", "clientkey", "proxylogin"})
# Credential fields inside a structured proxy value; server/host/scheme survive.
_PROXY_CREDENTIAL_KEYS = frozenset(
    {"user", "username", "login", "password", "pass", "auth", "credentials"}
)
# Below this length a string isn't confidently an image payload (a short
# opaque value leaks far less than a multi-megabyte base64 blob).
_BASE64_IMAGE_MIN_CHARS = 256
_BASE64_RE = re.compile(r"[A-Za-z0-9+/=\s]+")


def _looks_like_image_data(value: object) -> bool:
    """True for a long base64 blob, a ``data:`` URI, or a list of either."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("data:"):
            return True
        return len(stripped) >= _BASE64_IMAGE_MIN_CHARS and bool(
            _BASE64_RE.fullmatch(stripped)
        )
    if isinstance(value, (list, tuple)):
        return any(_looks_like_image_data(item) for item in value)
    return False


def _redact_proxy_url(value: str) -> str:
    """Drop userinfo (user:pass@) from a proxy URL, keeping scheme+host."""
    if "://" not in value:
        # Authority-style value without a scheme ("user:pass@host:port"), which
        # ``proxyAddress`` / ``proxy`` callers also use: ``urlsplit`` would
        # misread the userinfo as a scheme, so strip it directly.
        if "@" in value:
            return value.rsplit("@", 1)[-1]
        return value
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if not parsed.scheme:
        return value
    host = parsed.netloc.rsplit("@", 1)[-1]
    return f"{parsed.scheme}://{host}"


def _redact_proxy(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _redact_proxy_url(item) if isinstance(item, str) else item
            for key, item in value.items()
            if str(key).lower() not in _PROXY_CREDENTIAL_KEYS
        }
    if isinstance(value, str):
        return _redact_proxy_url(value)
    return value


def redact_params(params: dict) -> dict:
    """Return a log- and store-safe copy of task params.

    Drops every internal (``_``-prefixed) key — that covers the caller's key
    (``_clientKey``), solver plumbing that may hold live objects
    (``_vision`` / ``_AudioMeter`` / ``_proxy_override``) and egress data
    that may embed credentials (``_egress_server``) — plus proxy credentials
    (a proxy string loses its ``user:pass@`` userinfo, with or without a
    scheme; a proxy dict loses its user/password fields), base64 image
    payloads (``images`` / ``queries`` when they look like image data) and
    any key containing ``password`` / ``secret`` / ``token``.

    The result is guaranteed to survive ``json.dumps`` (the Redis task store
    persists it verbatim): a value that is not a JSON scalar/container is
    dropped instead of letting one opaque object abort the whole record
    write. The input mapping is never mutated, so callers can log the
    redacted copy next to the original in-memory params.
    """
    if not isinstance(params, dict):
        return params  # type: ignore[return-value]

    redacted: dict = {}
    for key, value in params.items():
        lowered = str(key).lower()
        # Internal solver state never belongs in a log line or the store:
        # ``_clientKey`` is a credential, ``_vision`` / ``_AudioMeter`` are
        # live objects that break ``json.dumps``, ``_egress_server`` may embed
        # user:pass, and ``_proxy_override.username`` is a sticky-session token.
        if lowered.startswith("_"):
            continue
        if lowered in _SENSITIVE_EXACT_KEYS or any(
            marker in lowered for marker in _SENSITIVE_KEY_MARKERS
        ):
            continue
        if lowered == "proxy":
            redacted[key] = _redact_proxy(value)
        elif lowered in {"images", "queries"} and _looks_like_image_data(value):
            # Heavy image payloads are dropped entirely; task type / question
            # still identify the request in logs.
            continue
        elif isinstance(value, dict):
            redacted[key] = redact_params(value)
        elif isinstance(value, list):
            redacted[key] = [
                redact_params(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            redacted[key] = value

    safe: dict = {}
    for key, value in redacted.items():
        try:
            json.dumps(value)
        except Exception:  # noqa: BLE001 - persistence must never be aborted
            log.debug("Dropping non-JSON-serializable task param %r", key)
            continue
        safe[key] = value
    return safe
