"""Tests for the shared real-outcome module (candidate 3 of the review).

``record_real_outcome`` is the one fan-out both the HTTP report route and the
solver's inline siteverify path delegate to. These unit tests pin its contract:
it drives all four sinks (proxy health, per-sitekey real bucket, accounting,
session reputation), gates each on the identity fields, and swallows a failing
sink so outcome feedback never propagates an error.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    _ = sys.path.insert(0, str(PROJECT_ROOT))

from src.services.egress import SolveIdentity  # noqa: E402
from src.services.outcome import record_real_outcome  # noqa: E402


class _ProxyPool:
    def __init__(self):
        self.report_calls = []
        self.sitekey_real_calls = []

    async def report(self, proxy_id, *, success):
        self.report_calls.append((proxy_id, success))

    async def report_sitekey_real(self, proxy_id, sitekey, *, success):
        self.sitekey_real_calls.append((proxy_id, sitekey, success))


class _Accounting:
    def __init__(self):
        self.calls = []

    async def record_real_outcome(self, sitekey, *, success, proxy_kind=None, model=None):
        self.calls.append((sitekey, success, proxy_kind, model))


class _SessionPool:
    def __init__(self):
        self.calls = []

    async def report_outcome(self, session_id, *, success):
        self.calls.append((session_id, success))


def _services(**over):
    base = dict(
        proxy_pool=_ProxyPool(),
        accounting=_Accounting(),
        session_pool=_SessionPool(),
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_full_fan_out_to_all_four_sinks():
    services = _services()
    identity = SolveIdentity(
        proxy_kind="pool_proxy", proxy_id="px-1", session_id="sess-1"
    )
    asyncio.run(
        record_real_outcome(
            services, identity, "sk-1", success=True, model="gpt-4o"
        )
    )
    assert services.proxy_pool.report_calls == [("px-1", True)]
    assert services.proxy_pool.sitekey_real_calls == [("px-1", "sk-1", True)]
    assert services.accounting.calls == [("sk-1", True, "pool_proxy", "gpt-4o")]
    assert services.session_pool.calls == [("sess-1", True)]


def test_no_proxy_id_skips_proxy_sinks_but_still_accounts():
    services = _services()
    identity = SolveIdentity(proxy_kind="proxyless", proxy_id=None, session_id=None)
    asyncio.run(
        record_real_outcome(services, identity, "sk-2", success=False)
    )
    assert services.proxy_pool.report_calls == []
    assert services.proxy_pool.sitekey_real_calls == []
    # Accounting is recorded even with no proxy / session.
    assert services.accounting.calls == [("sk-2", False, "proxyless", None)]
    assert services.session_pool.calls == []


def test_empty_sitekey_skips_sitekey_bucket_only():
    services = _services()
    identity = SolveIdentity(proxy_id="px-9", session_id="s-9")
    asyncio.run(record_real_outcome(services, identity, "", success=True))
    # Proxy health still updates, but the per-sitekey real bucket is skipped.
    assert services.proxy_pool.report_calls == [("px-9", True)]
    assert services.proxy_pool.sitekey_real_calls == []


def test_a_failing_sink_never_propagates():
    class _BoomProxy:
        async def report(self, *a, **k):
            raise RuntimeError("proxy backend down")

        async def report_sitekey_real(self, *a, **k):
            raise RuntimeError("proxy backend down")

    services = _services(proxy_pool=_BoomProxy())
    identity = SolveIdentity(proxy_id="px-err", session_id="s-err")
    # Must not raise even though the proxy sink blows up — and the other
    # sinks still run.
    asyncio.run(record_real_outcome(services, identity, "sk", success=True))
    assert services.accounting.calls == [("sk", True, None, None)]
    assert services.session_pool.calls == [("s-err", True)]


def test_accepts_a_solve_record_as_identity():
    """A stored SolveRecord duck-types as the outcome identity (route path)."""
    from src.consumption.ledger import SolveRecord

    services = _services()
    rec = SolveRecord(
        task_id="t",
        sitekey="sk",
        task_type="X",
        proxy_id="px",
        proxy_kind="datacenter",
        session_id="s",
    )
    asyncio.run(record_real_outcome(services, rec, rec.sitekey, success=False, model="m"))
    assert services.proxy_pool.report_calls == [("px", False)]
    assert services.accounting.calls == [("sk", False, "datacenter", "m")]
    assert services.session_pool.calls == [("s", False)]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
