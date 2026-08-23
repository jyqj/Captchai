"""Shared pytest helpers for the test suite.

Redis-backed tests (ledger, accounting, proxy pool, task store) need both the
``redis`` package *and* a reachable Redis server. :func:`redis_helper` skips
the calling test when either is missing — ``pytest.importorskip`` for the
package, then a best-effort ping for the server — so the suite degrades to a
clean skip instead of a hard failure on machines without Redis.

Import from test modules as::

    from tests.conftest import redis_helper, flush_prefix
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

REDIS_URL = "redis://localhost:6379/0"


def redis_helper():
    """Return (aioredis module, url) or skip the calling test.

    Skips when the ``redis`` package isn't installed OR no real Redis server
    answers a ping at ``REDIS_URL`` — these tests need a live server.
    """
    aioredis = pytest.importorskip("redis.asyncio")
    import redis as sync_redis

    try:
        client = sync_redis.from_url(REDIS_URL, decode_responses=True)
        client.ping()
        client.close()
    except Exception:
        pytest.skip(f"no reachable Redis server at {REDIS_URL}")
    return aioredis, REDIS_URL


def flush_prefix(prefix: str) -> None:
    """Synchronously flush test keys so tests don't see stale data."""
    import redis as sync_redis

    r = sync_redis.from_url(REDIS_URL, decode_responses=True)
    for key in r.scan_iter(f"{prefix}*"):
        r.delete(key)
    r.close()
