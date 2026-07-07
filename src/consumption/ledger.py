"""In-memory cost ledger for captcha solve attempts."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

# Dollars per 1k tokens: (input, output). Local models are effectively free.
DEFAULT_PRICE_TABLE: Dict[str, Any] = {
    "gpt-4o": (0.0025, 0.01),
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4.1": (0.002, 0.008),
    "gpt-4.1-mini": (0.0004, 0.0016),
    "claude-3-5-sonnet": (0.003, 0.015),
    "claude-3-5-haiku": (0.0008, 0.004),
    "gemini-1.5-pro": (0.00125, 0.005),
    "gemini-1.5-flash": (0.000075, 0.0003),
    "local": (0.0, 0.0),
}

# Fallback for unknown cloud models (assume a cheap cloud tier).
_UNKNOWN_CLOUD_PRICE = (0.0005, 0.0015)

_LOCAL_MARKERS = ("local", "ollama", "llama", "qwen", "vllm", "lmstudio")


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    price_table: dict | None = None,
) -> float:
    """Estimate USD cost of a model call from token counts.

    ``price_table`` maps model name to ``(input_per_1k, output_per_1k)``;
    falls back to the default table, then to heuristics (local-looking
    model names cost 0, unknown cloud models get a cheap default rate).
    """
    table = price_table if price_table is not None else DEFAULT_PRICE_TABLE
    key = (model or "").lower()

    prices = table.get(key)
    if prices is None:
        # Prefix match, e.g. "gpt-4o-2024-08-06" -> "gpt-4o".
        for name, p in table.items():
            if key.startswith(str(name).lower()):
                prices = p
                break
    if prices is None:
        if any(marker in key for marker in _LOCAL_MARKERS):
            return 0.0
        prices = _UNKNOWN_CLOUD_PRICE

    in_price, out_price = float(prices[0]), float(prices[1])
    return (input_tokens / 1000.0) * in_price + (output_tokens / 1000.0) * out_price


@dataclass
class SolveRecord:
    task_id: str
    sitekey: str
    task_type: str
    proxy_id: str | None = None
    session_id: str | None = None
    model: str | None = None
    challenge_shape: str | None = None      # "grid_select" | "area_bbox" | ...
    rounds: int = 0
    vision_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    proxy_bytes: int = 0
    wall_ms: int = 0
    outcome: str = "failed"                  # "ready" | "failed" | "timeout"
    est_cost_usd: float = 0.0
    client_key: str | None = None
    created_at: float = 0.0                  # epoch seconds; auto-set if 0


class CostLedger:
    """Append-only, bounded, async-safe ledger of solve attempts."""

    def __init__(self, max_records: int = 100_000) -> None:
        self._records: Deque[SolveRecord] = deque(maxlen=max_records)
        self._lock = asyncio.Lock()

    async def record(self, rec: SolveRecord) -> None:
        if rec.created_at == 0:
            rec.created_at = time.time()
        async with self._lock:
            self._records.append(rec)

    async def total_cost_usd(self, client_key: str | None = None) -> float:
        async with self._lock:
            return sum(
                r.est_cost_usd
                for r in self._records
                if client_key is None or r.client_key == client_key
            )

    async def records(
        self,
        client_key: str | None = None,
        sitekey: str | None = None,
        limit: int | None = None,
    ) -> list[SolveRecord]:
        async with self._lock:
            matched = [
                r
                for r in self._records
                if (client_key is None or r.client_key == client_key)
                and (sitekey is None or r.sitekey == sitekey)
            ]
        if limit is not None:
            matched = matched[-limit:]
        return matched

    async def summary(self) -> dict[str, Any]:
        async with self._lock:
            snapshot = list(self._records)

        by_outcome: Dict[str, int] = {}
        by_model: Dict[str, Dict[str, float]] = {}
        total_cost = 0.0
        for r in snapshot:
            total_cost += r.est_cost_usd
            by_outcome[r.outcome] = by_outcome.get(r.outcome, 0) + 1
            model = r.model or "unknown"
            slot = by_model.setdefault(model, {"count": 0, "cost_usd": 0.0})
            slot["count"] += 1
            slot["cost_usd"] += r.est_cost_usd

        successes = by_outcome.get("ready", 0)
        cost_per_success = (total_cost / successes) if successes else 0.0

        return {
            "count": len(snapshot),
            "cost_usd": total_cost,
            "by_outcome": by_outcome,
            "by_model": by_model,
            "cost_per_success": cost_per_success,
        }
