"""Compose per-tile screenshots into a single row-major grid image.

Sending N tile images to a multimodal model costs ~N× the image tokens and
N× the per-image decode latency. A hCaptcha 3×3 grid is 9 images per classify
call — and with self-consistency voting that becomes 9×``samples`` images.
Composing the tiles into ONE montage collapses that to a single image while
preserving the tile order the classifier prompt already assumes ("indexed
left-to-right, top-to-bottom starting at 0"), so index *i* in the model's
answer still maps back to tile *i* unchanged.

Pure function, no browser. Returns ``None`` whenever composition isn't
possible (fewer than two tiles, a decode failure, an implausible declared
size, or Pillow missing) so the caller falls back to the per-tile path with
zero behavioural change.

PIL decode / paste / PNG encode are CPU-bound and would stall the event loop
under concurrent solves, so async callers use :func:`acompose_grid`, which
runs the same work in a worker thread.
"""

from __future__ import annotations

import asyncio
import io
import math
from typing import List, Optional, Tuple

# Decompression guards. ``Image.open`` parses only the header, so these are
# checked against the *declared* ``size`` before any pixel access (``convert`` /
# ``paste`` force a full decode). They bound how much work a hostile or merely
# huge caller-supplied tile can make the CPU thread do.
_MAX_TILE_DIMENSION = 2048
_MAX_TILE_PIXELS = 4_000_000
_MAX_CANVAS_DIMENSION = 8192
_MAX_CANVAS_PIXELS = 16_000_000


def _size_within(
    width: int, height: int, max_dimension: int, max_pixels: int
) -> bool:
    """True when a declared ``(width, height)`` is decodable within budget."""
    try:
        width = int(width)
        height = int(height)
    except (TypeError, ValueError):
        return False
    return (
        0 < width <= max_dimension
        and 0 < height <= max_dimension
        and width * height <= max_pixels
    )


def compose_grid(
    images: List[bytes],
    *,
    cols: Optional[int] = None,
    gap: int = 6,
    background: Tuple[int, int, int] = (30, 30, 30),
) -> Optional[Tuple[bytes, int, int]]:
    """Row-major montage of ``images`` → ``(png_bytes, rows, cols)`` or ``None``.

    Tiles are laid out left-to-right, top-to-bottom into a grid of ``cols``
    columns (``ceil(sqrt(n))`` when not given), separated by a ``gap``-pixel
    margin filled with ``background`` so the model can tell tiles apart.
    Uneven tile sizes are centred within a uniform cell (max tile dimensions).

    Each tile's header is opened lazily and its declared size validated BEFORE
    the full decode; the assembled canvas is bounded too, so neither a single
    oversized tile nor a grid of merely large ones can exhaust the process.
    """
    if not images or len(images) < 2:
        return None
    try:
        from PIL import Image  # noqa: WPS433 - optional-at-runtime import
    except Exception:  # pragma: no cover - Pillow is a hard dep in requirements
        return None

    tiles = []
    for raw in images:
        if not raw:
            return None
        try:
            # ``open`` is lazy (header only): no pixel data is decoded yet.
            image = Image.open(io.BytesIO(raw))
        except Exception:
            return None
        width, height = image.size
        if not _size_within(
            width, height, _MAX_TILE_DIMENSION, _MAX_TILE_PIXELS
        ):
            return None
        try:
            # ``convert`` is the first pixel access → the full decode. The
            # header check above bounds what that decode can cost.
            with image:
                tiles.append(image.convert("RGB"))
        except Exception:
            return None

    n = len(tiles)
    if not cols or cols < 1:
        cols = int(math.ceil(math.sqrt(n)))
    rows = int(math.ceil(n / cols))

    cell_w = max(t.width for t in tiles)
    cell_h = max(t.height for t in tiles)
    total_w = cols * cell_w + (cols + 1) * gap
    total_h = rows * cell_h + (rows + 1) * gap
    if not _size_within(
        total_w, total_h, _MAX_CANVAS_DIMENSION, _MAX_CANVAS_PIXELS
    ):
        return None

    canvas = Image.new("RGB", (total_w, total_h), background)
    for i, tile in enumerate(tiles):
        r, c = divmod(i, cols)
        x = gap + c * (cell_w + gap) + (cell_w - tile.width) // 2
        y = gap + r * (cell_h + gap) + (cell_h - tile.height) // 2
        canvas.paste(tile, (x, y))

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue(), rows, cols


async def acompose_grid(
    images: List[bytes],
    *,
    cols: Optional[int] = None,
    gap: int = 6,
    background: Tuple[int, int, int] = (30, 30, 30),
) -> Optional[Tuple[bytes, int, int]]:
    """Async wrapper around :func:`compose_grid` (worker-thread decode).

    The decode / paste / PNG encode are CPU-bound, so running them on the event
    loop would stall every concurrent solve. Same contract: montage tuple or
    ``None`` when composition isn't possible.
    """
    return await asyncio.to_thread(
        compose_grid, images, cols=cols, gap=gap, background=background
    )
