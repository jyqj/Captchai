"""Human-like cursor motion for challenge interactions.

hCaptcha — and enterprise risk models in particular — score mouse dynamics:
the *shape* of the pointer path, per-step timing, and the click dwell, collected
as ``motionData`` and submitted alongside the answer. A raw Playwright
``locator.click()`` teleports the pointer to the element centre and clicks with
zero travel, zero dwell, and zero variance — one of the strongest automation
signals a solver can emit, and a common reason enterprise tokens are minted but
then rejected downstream.

These helpers move ``page.mouse`` along an eased, jittered, slightly-arced path
to a randomised point inside the target element, add a short pre-click dwell and
a realistic press duration, and thread the resulting cursor position back to the
caller so consecutive clicks trace a continuous path instead of teleporting
between tiles.

Design:

* :func:`ease_path` is a **pure** function (deterministic when handed a seeded
  ``random.Random``) so the motion curve is unit-testable with no browser.
* :func:`human_click` is a thin async driver over ``page.mouse``; every mouse
  capability is probed with ``getattr`` and the whole thing is guarded so a fake
  page in tests (or a page whose mouse lacks ``down``/``up``) degrades to
  ``None`` and lets the caller fall back to ``locator.click()``.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, List, Optional, Tuple

Point = Tuple[float, float]


# Overshoot shape ``t**4 * (1 - t)`` peaks at t=0.8 (a=4 → a/(a+1)); this
# constant normalises that peak to 1.0 so ``overshoot_px`` is the real peak
# displacement past the target along the travel axis. f(0)=f(1)=0, so the path
# still starts at ``start`` and lands EXACTLY on ``end``.
_OVERSHOOT_PEAK = (0.8**4) * 0.2


def ease_path(
    start: Point,
    end: Point,
    *,
    steps: int = 24,
    jitter: float = 1.4,
    arc: float = 6.0,
    rng: "random.Random | None" = None,
) -> List[Point]:
    """Return an eased, jittered, gently-arced path from ``start`` to ``end``.

    Models a real hand movement rather than a straight interpolation:

    * a **smoothstep** ease so the pointer accelerates then decelerates;
    * a small **overshoot-and-correct** past the target late in the travel
      (``t**4*(1-t)``, peaking near t=0.8) — the single most human trait of a
      pointer landing on a target, and absent from every linear/eased-only
      solver path;
    * a perpendicular **arc** that peaks mid-path; and
    * **correlated** (random-walk, mean-reverting) jitter instead of independent
      per-step uniform noise, because real tremor is autocorrelated — successive
      samples drift together rather than resampling white noise each step.

    The final point is forced to exactly ``end`` so the click lands on the
    intended pixel. Returns exactly ``steps`` points.
    """
    r = rng if rng is not None else random.Random()
    x0, y0 = start
    x1, y1 = end
    dx, dy = (x1 - x0), (y1 - y0)
    dist = (dx * dx + dy * dy) ** 0.5
    # Cap the arc height relative to the travel distance so short hops don't
    # loop wildly; randomise the sign so the bow isn't always the same way.
    sign = 1.0 if r.random() < 0.5 else -1.0
    arc_px = min(arc, dist * 0.15) * sign
    # Overshoot a few px past the target (capped for short hops), along the
    # unit travel direction. Zero-length travel → no overshoot (avoid /0).
    overshoot_px = min(arc, dist * 0.08)
    ux, uy = (dx / dist, dy / dist) if dist > 1e-6 else (0.0, 0.0)

    # Correlated jitter state (mean-reverting random walk).
    jx = jy = 0.0
    points: List[Point] = []
    for i in range(1, steps + 1):
        t = i / steps
        eased = t * t * (3 - 2 * t)  # smoothstep
        x = x0 + dx * eased
        y = y0 + dy * eased
        # (eased - eased^2) peaks at 0.25 (t=0.5); *4 normalises the peak to 1.
        y += arc_px * (eased - eased * eased) * 4.0
        # Overshoot-and-correct along the travel axis (0 at both ends).
        ov = overshoot_px * (t**4 * (1.0 - t)) / _OVERSHOOT_PEAK
        x += ux * ov
        y += uy * ov
        if i < steps:
            # Mean-reverting random walk: pull toward 0 (×0.6) then add a small
            # increment, so jitter is autocorrelated rather than white noise.
            jx = jx * 0.6 + r.uniform(-jitter, jitter) * 0.5
            jy = jy * 0.6 + r.uniform(-jitter, jitter) * 0.5
            x += jx
            y += jy
        points.append((x, y))
    if points:
        points[-1] = (x1, y1)
    return points


def point_in_box(box: dict, *, rng: "random.Random | None" = None) -> Point:
    """A randomised click point biased toward the centre of ``box``.

    Avoids always clicking the exact centre (a tell) while staying well inside
    the element (0.32–0.68 of each dimension) so the click never lands on a
    border or an adjacent tile.
    """
    r = rng if rng is not None else random.Random()
    x = float(box["x"]) + float(box["width"]) * r.uniform(0.32, 0.68)
    y = float(box["y"]) + float(box["height"]) * r.uniform(0.32, 0.68)
    return (x, y)


async def _sleep(seconds: float) -> None:
    if seconds > 0:
        await asyncio.sleep(seconds)


async def human_point_click(
    page: Any,
    target: Point,
    *,
    cursor: "Point | None" = None,
    jitter_ms: float = 90.0,
    rng: "random.Random | None" = None,
) -> Optional[Point]:
    """Move to an exact ``target`` point along an eased path and press.

    The click primitive shared by :func:`human_click` (which first picks a
    point inside a box) and coordinate solvers (area/bbox) that already know the
    exact pixel to click. Returns the final cursor point, or ``None`` when the
    page has no usable mouse (so the caller can fall back to ``locator.click``).
    Never raises.
    """
    r = rng if rng is not None else random.Random()
    mouse = getattr(page, "mouse", None)
    move = getattr(mouse, "move", None) if mouse is not None else None
    if move is None:
        return None

    tx, ty = float(target[0]), float(target[1])
    if cursor is None:
        # Approach from a plausible off-target origin rather than teleporting
        # the first move to the element.
        cursor = (tx - r.uniform(60.0, 160.0), ty - r.uniform(40.0, 120.0))

    step_max = max(0.004, jitter_ms / 1000.0)
    step_min = min(0.004, step_max)
    try:
        path = ease_path(cursor, (tx, ty), rng=r)
        n = len(path)
        for i, (x, y) in enumerate(path):
            await move(x, y)
            # Variable per-step dwell: a real pointer is fast mid-flight and
            # slow at the ends (acceleration / deceleration), so make the dwell
            # bell-shaped — shortest in the middle, longest approaching the
            # target — instead of a uniform sample every step.
            frac = i / (n - 1) if n > 1 else 0.5
            bell = 1.0 - 4.0 * (frac - 0.5) ** 2  # 1 at middle, 0 at ends
            dwell = step_max - (step_max - step_min) * bell
            await _sleep(dwell * r.uniform(0.7, 1.3))
        await _sleep(r.uniform(0.05, 0.16))  # pre-click dwell

        down = getattr(mouse, "down", None)
        up = getattr(mouse, "up", None)
        click = getattr(mouse, "click", None)
        if down is not None and up is not None:
            await down()
            await _sleep(r.uniform(0.03, 0.09))  # button press duration
            await up()
        elif click is not None:
            await click(tx, ty)
        else:
            return None
    except Exception:  # noqa: BLE001 - degrade to the locator.click() fallback
        return None
    return (tx, ty)


async def human_tap(
    page: Any,
    target: Point,
    *,
    rng: "random.Random | None" = None,
) -> Optional[Point]:
    """Tap a point via the **touchscreen** (trusted touchstart/touchend).

    A mobile fingerprint (Android UA, ``sec-ch-ua-mobile: ?1``, touch capable)
    paired with *mouse* events is a modality contradiction: mobile hCaptcha
    collects TOUCH motion, and a synthetic ``mousedown``/``click`` on a phone
    context is exactly the kind of inconsistency enterprise risk models flag.
    Playwright's ``touchscreen.tap`` emits trusted touch events through CDP, so
    this is the mobile counterpart of :func:`human_point_click`. A short
    pre-tap dwell models human reaction time. Returns the tapped point, or
    ``None`` when the page exposes no touchscreen (caller falls back to a
    mouse click). Never raises.
    """
    r = rng if rng is not None else random.Random()
    ts = getattr(page, "touchscreen", None)
    tap = getattr(ts, "tap", None) if ts is not None else None
    if tap is None:
        return None
    tx, ty = float(target[0]), float(target[1])
    try:
        await _sleep(r.uniform(0.05, 0.20))  # reaction time before the tap
        await tap(tx, ty)
    except Exception:  # noqa: BLE001 - degrade to the mouse-click fallback
        return None
    return (tx, ty)


async def human_tap_box(
    page: Any,
    box: dict,
    *,
    rng: "random.Random | None" = None,
) -> Optional[Point]:
    """Tap a random point inside ``box`` via the touchscreen (mobile clicks)."""
    r = rng if rng is not None else random.Random()
    return await human_tap(page, point_in_box(box, rng=r), rng=r)


async def human_touch_scroll(
    page: Any,
    *,
    seconds: float,
    rng: "random.Random | None" = None,
) -> None:
    """Passive motion for a mobile context: a few scroll nudges with dwell.

    A real phone user doesn't trace a cursor; they scroll and pause. Emitting
    ``mouse.move`` on a touch context (as the desktop wander does) is itself a
    contradiction, so mobile solves fill the passive window with wheel-driven
    scrolls (which Playwright delivers to a mobile-emulated page) plus reading
    dwell instead. Best-effort; never raises.
    """
    r = rng if rng is not None else random.Random()
    wheel = getattr(getattr(page, "mouse", None), "wheel", None)
    if wheel is None:
        return
    loop = asyncio.get_event_loop()
    deadline = loop.time() + max(0.0, seconds)
    try:
        while loop.time() < deadline:
            await wheel(0, r.randint(80, 320))
            await _sleep(r.uniform(0.25, 0.7))
            if r.random() < 0.3:  # occasional scroll-back up
                await wheel(0, -r.randint(40, 160))
                await _sleep(r.uniform(0.2, 0.5))
    except Exception:  # noqa: BLE001 - passive motion is best-effort
        return


async def human_click(
    page: Any,
    box: dict,
    *,
    cursor: "Point | None" = None,
    jitter_ms: float = 90.0,
    rng: "random.Random | None" = None,
) -> Optional[Point]:
    """Move to a random point inside ``box`` along an eased path and press.

    Returns the final cursor point (so the caller can chain the next move) or
    ``None`` when the page has no usable mouse — signalling the caller to fall
    back to ``locator.click()``. Never raises: a fake/partial mouse degrades to
    ``None``.
    """
    r = rng if rng is not None else random.Random()
    target = point_in_box(box, rng=r)
    return await human_point_click(
        page, target, cursor=cursor, jitter_ms=jitter_ms, rng=r
    )


async def human_drag(
    page: Any,
    start: Point,
    end: Point,
    *,
    steps: int = 28,
    jitter: float = 1.4,
    arc: float = 8.0,
    jitter_ms: float = 60.0,
    rng: "random.Random | None" = None,
) -> bool:
    """Press at ``start``, drag along an eased/jittered path, release at ``end``.

    The drag counterpart of :func:`human_click`: slider-puzzle and
    drag-and-drop challenges score the *pointer dynamics* of the drag itself
    (grab dwell, per-step velocity, a small arc, a settle-then-release), so a
    raw ``move → down → linear moves → up`` loop with zero dwell is a bot tell
    exactly like a teleport click is. Returns ``True`` on success, ``False``
    when the page has no usable press-capable mouse (so the caller can fall
    back to its raw stepped move). Never raises.
    """
    r = rng if rng is not None else random.Random()
    mouse = getattr(page, "mouse", None)
    move = getattr(mouse, "move", None) if mouse is not None else None
    down = getattr(mouse, "down", None) if mouse is not None else None
    up = getattr(mouse, "up", None) if mouse is not None else None
    if move is None or down is None or up is None:
        return False

    sx, sy = float(start[0]), float(start[1])
    ex, ey = float(end[0]), float(end[1])
    step_max = max(0.004, jitter_ms / 1000.0)
    try:
        await move(sx, sy)
        await _sleep(r.uniform(0.05, 0.15))  # settle before grabbing the handle
        await down()
        await _sleep(r.uniform(0.05, 0.12))  # grab dwell before the drag starts
        for x, y in ease_path(
            (sx, sy), (ex, ey), steps=steps, jitter=jitter, arc=arc, rng=r
        ):
            await move(x, y)
            await _sleep(r.uniform(0.004, step_max))
        await _sleep(r.uniform(0.04, 0.10))  # settle at the target before release
        await up()
        return True
    except Exception:  # noqa: BLE001 - degrade to the caller's raw drag fallback
        return False


async def human_wander(
    page: Any,
    *,
    seconds: float,
    viewport: "Tuple[int, int] | None" = None,
    cursor: "Point | None" = None,
    jitter_ms: float = 90.0,
    rng: "random.Random | None" = None,
) -> Optional[Point]:
    """Wander the pointer between random targets for roughly ``seconds``.

    hCaptcha's *passive* scoring (invisible widgets, checkbox pre-scoring) reads
    a continuous ``motionData`` timeline: entering the page and moving around
    over a few seconds, not a single 0.5s burst then stillness. A lone short
    move leaves an almost-empty motion buffer — one of the strongest
    "invisible → challenge every time" tells. This traces several eased
    sub-movements to random on-viewport points with human dwell between them,
    filling the buffer with realistic motion during the passive window.

    Best-effort: returns the final cursor point, or ``None`` when the page has
    no usable mouse. Never raises.
    """
    r = rng if rng is not None else random.Random()
    mouse = getattr(page, "mouse", None)
    move = getattr(mouse, "move", None) if mouse is not None else None
    if move is None:
        return None

    vw, vh = viewport or (1280, 800)
    # Keep targets a little inside the viewport edges (a human rarely parks the
    # pointer exactly on the border).
    def _target() -> Point:
        return (r.uniform(vw * 0.08, vw * 0.92), r.uniform(vh * 0.12, vh * 0.88))

    if cursor is None:
        cursor = _target()
        try:
            await move(cursor[0], cursor[1])
        except Exception:  # noqa: BLE001
            return None

    loop = asyncio.get_event_loop()
    deadline = loop.time() + max(0.0, seconds)
    step_max = max(0.004, jitter_ms / 1000.0)
    try:
        while loop.time() < deadline:
            dest = _target()
            for x, y in ease_path(cursor, dest, steps=r.randint(12, 22), rng=r):
                await move(x, y)
                await _sleep(r.uniform(0.004, step_max))
            cursor = dest
            # Pause between sub-movements (reading / hesitating), the bulk of a
            # real passive timeline's wall-time.
            await _sleep(r.uniform(0.15, 0.55))
    except Exception:  # noqa: BLE001 - passive motion is best-effort
        return cursor
    return cursor
