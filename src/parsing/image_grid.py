"""Compose per-tile screenshots into a single row-major grid image.

Sending N tile images to a multimodal model costs ~N× the image tokens and
N× the per-image decode latency. A hCaptcha 3×3 grid is 9 images per classify
call — and with self-consistency voting that becomes 9×``samples`` images.
Composing the tiles into ONE montage collapses that to a single image while
preserving the tile order the classifier prompt already assumes ("indexed
left-to-right, top-to-bottom starting at 0"), so index *i* in the model's
answer still maps back to tile *i* unchanged.

Pure function, no browser. Returns ``None`` whenever composition isn't
possible (fewer than two tiles, a decode failure, or Pillow missing) so the
caller falls back to the per-tile path with zero behavioural change.
"""

from __future__ import annotations

import io
import math
from typing import List, Optional, Tuple


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
            tiles.append(Image.open(io.BytesIO(raw)).convert("RGB"))
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

    canvas = Image.new("RGB", (total_w, total_h), background)
    for i, tile in enumerate(tiles):
        r, c = divmod(i, cols)
        x = gap + c * (cell_w + gap) + (cell_w - tile.width) // 2
        y = gap + r * (cell_h + gap) + (cell_h - tile.height) // 2
        canvas.paste(tile, (x, y))

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue(), rows, cols
