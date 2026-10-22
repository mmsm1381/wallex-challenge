"""Tests for the runner with the AI part on, with a scripted provider."""

import asyncio

from sweepbot import config
from sweepbot import runner as runner_mod
from sweepbot import store as store_mod
from sweepbot.ai import provider
from sweepbot.ai import schema
from sweepbot.ai import supervisor as supervisor_mod
from test_sweep_replay import sweep_records

BTC_PLAN = schema.Plan(slots=[schema.SlotPlan(market="BTCTMN", depth_pct=1.3,
                                              size_tmn=1_000_000.0)],
                       reasoning="BTCTMN sweeps are frequent today.")


async def drive(runner: runner_mod.Runner, records: list, until: float) -> None:
    """Feeds `records` and runs ticks and AI steps up to `until`.

    After each AI step the test yields to the event loop, so that the model
    tasks (in worker threads) can finish, as in the live loop.
    """
    market = runner.market
    records = iter(records)
    pending = next(records, None)
    now = 0.0
    while now <= until:
        while pending is not None and pending[0] <= now:
            ts, kind, record = pending
            if kind == "book":
                market.on_book(record["m"], record["s"], [tuple(x) for x in record["lv"]], ts)
            elif kind == "trade":
                market.on_trades(record["m"], [(record["p"], record["q"], record["bm"], None)], ts)
            else:
                market.on_quote(record["v"], record["c"], record["b"], record["a"], ts)
            pending = next(records, None)
        if abs(now / 5 - round(now / 5)) < 1e-9:
            market.sample_premiums(now)
        await runner.tick(now)
        if abs(now - round(now)) < 1e-9:
            await runner.ai_step(now)
            await asyncio.sleep(0.002)
        now = round(now + 0.1, 1)


def test_ai_plan_hold_or_cut_and_trade_log_flow(tmp_path) -> None:
    """The model plan picks BTCTMN. The sweep fills, no buyer comes back, and
    after 5 minutes the model says cut. The bot sells and logs the trade.
    """
    # given
    cfg = config.load_config(None)
    cfg["premium"]["min_samples"] = 12
    cfg["bot"]["out_dir"] = str(tmp_path)
    store = store_mod.Store(":memory:")
    fake = provider.FakeProvider(
        plans=[BTC_PLAN] * 20,
        hold_or_cut=[schema.HoldOrCut(decision="cut", reason="fair value fell")],
        trade_logs=[schema.TradeLog(text="AI log text.", lesson="Cut early.")])
    holder = {}
    supervisor = supervisor_mod.Supervisor(
        cfg, fake, store, equity_fn=lambda now: holder["runner"].bot.equity(now))
    runner = runner_mod.Runner(cfg, mode="paper", store=store, start_ts=0.0,
                               supervisor=supervisor, with_twin=False)
    holder["runner"] = runner
    records = sweep_records(end_s=530.0, rebound=False)

    # when
    asyncio.run(drive(runner, records, until=520.0))

    # then
    jobs = [job for job, _ in fake.calls]
    assert "propose_plan" in jobs and "hold_or_cut" in jobs and "write_trade_log" in jobs
    trades = store.rows("trades")
    assert len(trades) == 1 and trades[0]["exit_reason"] == "cut"
    texts = [row["text"] for row in store.rows("logs", "kind = ?", ("trade",))]
    assert any(text == "AI log text." for text in texts)
    assert any("BTCTMN: bought" in text for text in texts)
    plans = store.rows("plans", "provider = ?", ("fake",))
    assert plans and plans[0]["accepted"] is True
