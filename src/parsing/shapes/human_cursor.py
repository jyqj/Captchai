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

    Uses a smoothstep ease on the straight-line interpolation (so the pointer
    accelerates then decelerates like a hand movement), adds a perpendicular-ish
    arc that peaks mid-path, and perturbs the intermediate points with small
    random jitter. The final point is forced to exactly ``end`` so the click
    lands on the intended pixel regardless of jitter.
    """
    r = rng if rng is not None else random.Random()
    x0, y0 = start
    x1, y1 = end
    dist = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
    # Cap the arc height relative to the travel distance so short hops don't
    # loop wildly; randomise the sign so the bow isn't always the same way.
    sign = 1.0 if r.random() < 0.5 else -1.0
    arc_px = min(arc, dist * 0.15) * sign

    points: List[Point] = []
    for i in range(1, steps + 1):
        t = i / steps
        eased = t * t * (3 - 2 * t)  # smoothstep
        x = x0 + (x1 - x0) * eased
        y = y0 + (y1 - y0) * eased
        # (eased - eased^2) peaks at 0.25 (t=0.5); *4 normalises the peak to 1.
        y += arc_px * (eased - eased * eased) * 4.0
        if i < steps:
            x += r.uniform(-jitter, jitter)
            y += r.uniform(-jitter, jitter)
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
    try:
        for x, y in ease_path(cursor, (tx, ty), rng=r):
            await move(x, y)
            await _sleep(r.uniform(0.004, step_max))
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
