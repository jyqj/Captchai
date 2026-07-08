"""Tests for the consumption/metering layer (ledger, budget, accounting)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    _ = sys.path.insert(0, str(PROJECT_ROOT))

from src.consumption.accounting import SuccessAccounting
from src.consumption.budget import BudgetGuard
from src.consumption.ledger import CostLedger, SolveRecord, estimate_cost


def _rec(
    task_id: str,
    *,
    cost: float = 0.0,
    client_key: str | None = None,
    sitekey: str = "sk-default",
    outcome: str = "ready",
    model: str | None = None,
) -> SolveRecord:
    return SolveRecord(
        task_id=task_id,
        sitekey=sitekey,
        task_type="RecaptchaV2TaskProxyless",
        est_cost_usd=cost,
        client_key=client_key,
        outcome=outcome,
        model=model,
    )


# ---------------------------------------------------------------------------
# CostLedger
# ---------------------------------------------------------------------------


def test_ledger_record_sets_created_at_and_totals() -> None:
    async def run() -> None:
        ledger = CostLedger()
        rec = _rec("t1", cost=0.5)
        assert rec.created_at == 0.0
        await ledger.record(rec)
        assert rec.created_at > 0

        assert await ledger.total_cost_usd() == 0.5

    asyncio.run(run())


def test_ledger_total_cost_filters_by_client_key() -> None:
    async def run() -> None:
        ledger = CostLedger()
        await ledger.record(_rec("t1", cost=1.0, client_key="alice"))
        await ledger.record(_rec("t2", cost=2.0, client_key="bob"))
        await ledger.record(_rec("t3", cost=4.0, client_key="alice"))

        assert await ledger.total_cost_usd() == 7.0
        assert await ledger.total_cost_usd("alice") == 5.0
        assert await ledger.total_cost_usd("bob") == 2.0
        assert await ledger.total_cost_usd("nobody") == 0.0

    asyncio.run(run())


def test_ledger_records_filtering_and_limit() -> None:
    async def run() -> None:
        ledger = CostLedger()
        await ledger.record(_rec("t1", client_key="alice", sitekey="sk-a"))
        await ledger.record(_rec("t2", client_key="alice", sitekey="sk-b"))
        await ledger.record(_rec("t3", client_key="bob", sitekey="sk-a"))

        assert len(await ledger.records()) == 3
        alice = await ledger.records(client_key="alice")
        assert [r.task_id for r in alice] == ["t1", "t2"]
        sk_a = await ledger.records(sitekey="sk-a")
        assert [r.task_id for r in sk_a] == ["t1", "t3"]
        last_one = await ledger.records(limit=1)
        assert [r.task_id for r in last_one] == ["t3"]

    asyncio.run(run())


def test_ledger_summary_cost_per_success() -> None:
    async def run() -> None:
        ledger = CostLedger()
        await ledger.record(_rec("t1", cost=1.0, outcome="ready", model="gpt-4o"))
        await ledger.record(_rec("t2", cost=2.0, outcome="failed", model="gpt-4o"))
        await ledger.record(_rec("t3", cost=3.0, outcome="ready", model="local"))
        await ledger.record(_rec("t4", cost=0.0, outcome="timeout"))

        summary = await ledger.summary()
        assert summary["count"] == 4
        assert summary["cost_usd"] == 6.0
        assert summary["by_outcome"] == {"ready": 2, "failed": 1, "timeout": 1}
        assert summary["by_model"]["gpt-4o"] == {"count": 2, "cost_usd": 3.0}
        assert summary["by_model"]["local"] == {"count": 1, "cost_usd": 3.0}
        assert summary["by_model"]["unknown"] == {"count": 1, "cost_usd": 0.0}
        # 6.0 total cost / 2 "ready" outcomes
        assert summary["cost_per_success"] == 3.0

    asyncio.run(run())


def test_ledger_summary_no_successes() -> None:
    async def run() -> None:
        ledger = CostLedger()
        await ledger.record(_rec("t1", cost=1.0, outcome="failed"))
        summary = await ledger.summary()
        assert summary["cost_per_success"] == 0.0

    asyncio.run(run())


def test_ledger_max_records_ring_behavior() -> None:
    async def run() -> None:
        ledger = CostLedger(max_records=3)
        for i in range(5):
            await ledger.record(_rec(f"t{i}", cost=1.0))

        records = await ledger.records()
        assert [r.task_id for r in records] == ["t2", "t3", "t4"]
        # Evicted records no longer count toward totals.
        assert await ledger.total_cost_usd() == 3.0
        assert (await ledger.summary())["count"] == 3

    asyncio.run(run())


# ---------------------------------------------------------------------------
# estimate_cost
# ---------------------------------------------------------------------------


def test_estimate_cost_local_is_free_cloud_is_not() -> None:
    assert estimate_cost("local", 10_000, 10_000) == 0.0
    assert estimate_cost("ollama/qwen2-vl", 10_000, 10_000) == 0.0
    assert estimate_cost("gpt-4o", 1_000, 1_000) > 0.0


def test_estimate_cost_math_and_override() -> None:
    # gpt-4o defaults: 0.0025 in / 0.01 out per 1k tokens
    assert estimate_cost("gpt-4o", 1000, 1000) == 0.0025 + 0.01
    # custom price table overrides defaults
    table = {"mymodel": (1.0, 2.0)}
    assert estimate_cost("mymodel", 500, 500, table) == 0.5 + 1.0
    # unknown cloud-looking model still costs something
    assert estimate_cost("some-future-cloud-model", 1000, 1000) > 0.0


# ---------------------------------------------------------------------------
# BudgetGuard
# ---------------------------------------------------------------------------


def test_budget_no_caps_always_allows() -> None:
    async def run() -> None:
        ledger = CostLedger()
        await ledger.record(_rec("t1", cost=1_000_000.0))
        guard = BudgetGuard(ledger)
        decision = await guard.check("anyone", 999.0, model="gpt-4o")
        assert decision.allowed

    asyncio.run(run())


def test_budget_global_cap_allows_under_denies_over() -> None:
    async def run() -> None:
        ledger = CostLedger()
        await ledger.record(_rec("t1", cost=8.0, client_key="alice"))
        guard = BudgetGuard(ledger, global_cap_usd=10.0)

        under = await guard.check("alice", 1.0, model="gpt-4o")
        assert under.allowed

        over = await guard.check("alice", 3.0, model="gpt-4o")
        assert not over.allowed
        assert "global" in over.reason
        assert over.downgrade_to == "local"

    asyncio.run(run())


def test_budget_per_client_cap() -> None:
    async def run() -> None:
        ledger = CostLedger()
        await ledger.record(_rec("t1", cost=5.0, client_key="alice"))
        await ledger.record(_rec("t2", cost=1.0, client_key="bob"))
        guard = BudgetGuard(ledger, per_client_cap_usd=6.0)

        # alice already at 5.0: 2.0 more would breach her 6.0 cap.
        denied = await guard.check("alice", 2.0, model="gpt-4o")
        assert not denied.allowed
        assert denied.downgrade_to == "local"

        # bob has room.
        allowed = await guard.check("bob", 2.0, model="gpt-4o")
        assert allowed.allowed

        # anonymous traffic is not subject to per-client caps.
        anon = await guard.check(None, 2.0, model="gpt-4o")
        assert anon.allowed

    asyncio.run(run())


def test_budget_free_requests_always_allowed() -> None:
    async def run() -> None:
        ledger = CostLedger()
        await ledger.record(_rec("t1", cost=100.0))
        guard = BudgetGuard(ledger, global_cap_usd=10.0)
        decision = await guard.check("alice", 0.0, model="local")
        assert decision.allowed

    asyncio.run(run())


# ---------------------------------------------------------------------------
# SuccessAccounting
# ---------------------------------------------------------------------------


def test_accounting_optimistic_default() -> None:
    async def run() -> None:
        acc = SuccessAccounting()
        assert await acc.success_rate("never-seen") == 1.0
        stats = await acc.stats("never-seen")
        assert stats == {"attempts": 0, "successes": 0, "rate": 1.0}

    asyncio.run(run())


def test_accounting_updates_with_outcomes() -> None:
    async def run() -> None:
        acc = SuccessAccounting()
        await acc.record("sk", "ready")
        await acc.record("sk", "failed")
        await acc.record("sk", "timeout")
        await acc.record("sk", "ready")

        assert await acc.success_rate("sk") == 0.5
        stats = await acc.stats("sk")
        assert stats["attempts"] == 4
        assert stats["successes"] == 2
        assert stats["rate"] == 0.5

    asyncio.run(run())


def test_accounting_dimensions_are_independent() -> None:
    async def run() -> None:
        acc = SuccessAccounting()
        await acc.record("sk", "failed", proxy_kind="residential", model="gpt-4o")
        await acc.record("sk", "ready", proxy_kind="datacenter", model="gpt-4o")

        rate_res = await acc.success_rate("sk", proxy_kind="residential", model="gpt-4o")
        rate_dc = await acc.success_rate("sk", proxy_kind="datacenter", model="gpt-4o")
        assert rate_res == 0.0
        assert rate_dc == 1.0
        # bare-sitekey bucket has no data -> optimistic
        assert await acc.success_rate("sk") == 1.0
        # but stats() aggregates across all buckets for the sitekey
        stats = await acc.stats("sk")
        assert stats["attempts"] == 2
        assert stats["successes"] == 1

    asyncio.run(run())


def test_accounting_respects_window() -> None:
    async def run() -> None:
        acc = SuccessAccounting(window=4)
        # 4 failures fill the window...
        for _ in range(4):
            await acc.record("sk", "failed")
        assert await acc.success_rate("sk") == 0.0
        # ...then 4 successes push them all out.
        for _ in range(4):
            await acc.record("sk", "ready")
        assert await acc.success_rate("sk") == 1.0
        stats = await acc.stats("sk")
        assert stats["attempts"] == 4  # bounded by window

    asyncio.run(run())


# ---------------------------------------------------------------------------
# BaseBrowserSolver._record helper
# ---------------------------------------------------------------------------


def test_record_helper_produces_solve_record() -> None:
    async def run() -> None:
        from types import SimpleNamespace

        from src.services.browser_solver import BaseBrowserSolver

        ledger = CostLedger()
        services = SimpleNamespace(
            ledger=ledger,
            accounting=SuccessAccounting(),
            session_pool=None,
            proxy_pool=None,
        )
        config = SimpleNamespace(
            human_mouse_enabled=False,
            human_mouse_jitter_ms=0,
        )
        solver = BaseBrowserSolver(config, services=services)

        params = {
            "type": "RecaptchaV2TaskProxyless",
            "_taskId": "task-xyz",
            "_clientKey": "client-abc",
            "_proxyKind": "proxyless",
            "_sessionId": "sess-1",
        }
        started = __import__("time").monotonic()
        await solver._record(
            params,
            sitekey="sk-record",
            client_key="client-abc",
            outcome="ready",
            started=started,
            task_type="RecaptchaV2TaskProxyless",
            challenge_shape="audio",
        )

        records = await ledger.records()
        assert len(records) == 1
        rec = records[0]
        assert rec.task_id == "task-xyz"
        assert rec.sitekey == "sk-record"
        assert rec.task_type == "RecaptchaV2TaskProxyless"
        assert rec.outcome == "ready"
        assert rec.client_key == "client-abc"
        assert rec.proxy_kind == "proxyless"
        assert rec.session_id == "sess-1"
        assert rec.challenge_shape == "audio"
        assert rec.proxy_bytes == 0
        assert rec.wall_ms >= 0
        assert rec.created_at > 0

    asyncio.run(run())


def test_recaptcha_v2_records_to_ledger() -> None:
    async def run() -> None:
        from types import SimpleNamespace

        from src.services.recaptcha_v2 import RecaptchaV2Solver

        ledger = CostLedger()
        services = SimpleNamespace(
            ledger=ledger,
            accounting=SuccessAccounting(),
            session_pool=None,
            proxy_pool=None,
        )
        config = SimpleNamespace(
            human_mouse_enabled=False,
            human_mouse_jitter_ms=0,
            captcha_retries=1,
            browser_timeout=10,
        )

        class FakeManager:
            async def new_context(self, params):
                class FakeCtx:
                    async def close(self) -> None:
                        pass

                return FakeCtx(), params.get("userAgent") or "UA"

        solver = RecaptchaV2Solver(config, manager=FakeManager(), services=services)

        async def _fake_solve_once(website_url, website_key, is_invisible, params):
            return "token-" + "x" * 30, "UA"

        solver._solve_once = _fake_solve_once  # type: ignore[attr-defined]

        params = {
            "type": "RecaptchaV2TaskProxyless",
            "websiteURL": "https://example.com",
            "websiteKey": "sk-v2",
            "_clientKey": "client-v2",
            "_taskId": "task-v2",
        }
        result = await solver.solve(params)
        assert result["gRecaptchaResponse"].startswith("token-")

        records = await ledger.records()
        assert len(records) == 1
        rec = records[0]
        assert rec.outcome == "ready"
        assert rec.sitekey == "sk-v2"
        assert rec.task_type == "RecaptchaV2TaskProxyless"
        assert rec.client_key == "client-v2"
        assert rec.task_id == "task-v2"
        assert rec.challenge_shape == "audio"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# RedisCostLedger (persisted spend / records across restarts)
# ---------------------------------------------------------------------------


def _redis_helper():
    """Return (aioredis, url) or skip the test if redis isn't available."""
    pytest = __import__("pytest")
    aioredis = pytest.importorskip("redis.asyncio")
    return aioredis, "redis://localhost:6379/0"


def _flush_prefix(prefix: str) -> None:
    """Synchronously flush test keys so tests don't see stale data."""
    import redis as sync_redis

    r = sync_redis.from_url("redis://localhost:6379/0", decode_responses=True)
    for key in r.scan_iter(f"{prefix}:*"):
        r.delete(key)
    r.close()


def test_redis_ledger_persists_total_and_records() -> None:
    aioredis, url = _redis_helper()
    prefix = "test:ledger:persist"

    async def run() -> None:
        from src.consumption.ledger import RedisCostLedger

        _flush_prefix(prefix)
        ledger = RedisCostLedger(url, key_prefix=prefix)

        await ledger.record(_rec("t1", cost=0.5, client_key="client-a"))
        await ledger.record(_rec("t2", cost=1.5, client_key="client-b"))

        # Global total survives in Redis (O(1) GET, not a scan).
        assert await ledger.total_cost_usd() == 2.0
        # Per-client totals are tracked separately.
        assert await ledger.total_cost_usd("client-a") == 0.5
        assert await ledger.total_cost_usd("client-b") == 1.5

        # Records are retrievable and round-trip through JSON.
        records = await ledger.records()
        assert len(records) == 2
        # LPUSH means newest first.
        assert records[0].task_id == "t2"
        assert records[1].task_id == "t1"
        assert all(r.created_at > 0 for r in records)

        # Simulate a "restart": drop the in-process object and build a new one
        # against the same Redis. Spend and history must survive.
        await ledger.close()
        ledger2 = RedisCostLedger(url, key_prefix=prefix)
        assert await ledger2.total_cost_usd() == 2.0
        assert await ledger2.total_cost_usd("client-a") == 0.5
        records2 = await ledger2.records()
        assert len(records2) == 2
        await ledger2.close()

    asyncio.run(run())


def test_redis_ledger_summary_aggregates() -> None:
    aioredis, url = _redis_helper()
    prefix = "test:ledger:summary"

    async def run() -> None:
        from src.consumption.ledger import RedisCostLedger

        _flush_prefix(prefix)
        ledger = RedisCostLedger(url, key_prefix=prefix)

        await ledger.record(_rec("t1", cost=0.5, client_key="c1", outcome="ready", model="cloud"))
        await ledger.record(_rec("t2", cost=1.0, client_key="c1", outcome="failed", model="cloud"))
        await ledger.record(_rec("t3", cost=0.0, client_key="c2", outcome="ready", model="local"))

        summary = await ledger.summary()
        assert summary["count"] == 3
        assert summary["cost_usd"] == 1.5
        assert summary["by_outcome"]["ready"] == 2
        assert summary["by_outcome"]["failed"] == 1
        assert summary["by_model"]["cloud"]["count"] == 2
        assert summary["by_model"]["cloud"]["cost_usd"] == 1.5
        assert summary["by_model"]["local"]["count"] == 1
        # cost_per_success = total / successes = 1.5 / 2
        assert abs(summary["cost_per_success"] - 0.75) < 1e-9
        await ledger.close()

    asyncio.run(run())


def test_redis_ledger_records_filter_by_client_and_sitekey() -> None:
    aioredis, url = _redis_helper()
    prefix = "test:ledger:filter"

    async def run() -> None:
        from src.consumption.ledger import RedisCostLedger

        _flush_prefix(prefix)
        ledger = RedisCostLedger(url, key_prefix=prefix)

        await ledger.record(_rec("t1", cost=0.5, client_key="c1", sitekey="sk-a"))
        await ledger.record(_rec("t2", cost=1.0, client_key="c2", sitekey="sk-a"))
        await ledger.record(_rec("t3", cost=0.0, client_key="c1", sitekey="sk-b"))

        c1_records = await ledger.records(client_key="c1")
        assert {r.task_id for r in c1_records} == {"t1", "t3"}
        ska_records = await ledger.records(sitekey="sk-a")
        assert {r.task_id for r in ska_records} == {"t1", "t2"}
        c1_ska = await ledger.records(client_key="c1", sitekey="sk-a")
        assert [r.task_id for r in c1_ska] == ["t1"]
        await ledger.close()

    asyncio.run(run())


def test_build_ledger_selects_redis_when_configured() -> None:
    aioredis, url = _redis_helper()
    prefix = "test:ledger:build"

    async def run() -> None:
        from types import SimpleNamespace

        from src.consumption.ledger import (
            CostLedger,
            RedisCostLedger,
            build_ledger,
        )

        # No redis_url → in-memory CostLedger.
        cfg_mem = SimpleNamespace(redis_url=None)
        assert isinstance(build_ledger(cfg_mem), CostLedger)

        # redis_url set → RedisCostLedger.
        cfg_redis = SimpleNamespace(redis_url=url)
        ledger = build_ledger(cfg_redis)
        assert isinstance(ledger, RedisCostLedger)
        await ledger.close()

    asyncio.run(run())
