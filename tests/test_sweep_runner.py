"""Tests for the runner with the AI part on, with a scripted provider."""

import asyncio

import pytest

from sweepbot import broker
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


def test_adopted_position_is_sold_after_the_model_says_cut(tmp_path) -> None:
    """Live 2026-10-23: a restart lost a stuck ADATMN position.

    The config names the position. The wallet holds less than the config
    quantity, so the bot adopts the wallet quantity. It sends an exit at
    fair value and asks the model at once (no 5-minute wait). The model
    says cut, so the bot sells and books the trade.
    """
    # given
    cfg = config.load_config(None)
    cfg["premium"]["min_samples"] = 12
    cfg["bot"]["out_dir"] = str(tmp_path)
    cfg["adopt"]["positions"] = [{"market": "BTCTMN", "qty": 0.00004,
                                  "entry_price": 21_900_000_000.0,
                                  "entry_utc": "1970-01-01T00:00:00Z"}]
    store = store_mod.Store(":memory:")
    fake = provider.FakeProvider(
        plans=[schema.Plan(slots=[], reasoning="No bids.")] * 20,
        hold_or_cut=[schema.HoldOrCut(decision="cut", reason="the user wants it sold")],
        trade_logs=[schema.TradeLog(text="AI log text.", lesson="Adopt with care.")])
    holder = {}
    supervisor = supervisor_mod.Supervisor(
        cfg, fake, store, equity_fn=lambda now: holder["runner"].bot.equity(now))
    runner = runner_mod.Runner(cfg, mode="paper", store=store, start_ts=0.0,
                               supervisor=supervisor, with_twin=False)
    holder["runner"] = runner
    runner.broker.balance["BTC"] = 0.00003
    records = sweep_records(sweep_at=10_000.0, end_s=120.0)

    # when
    adopted = asyncio.run(runner.adopt_positions(0.0))
    asyncio.run(drive(runner, records, until=100.0))

    # then
    assert adopted == [("BTCTMN", 0.00003)]
    jobs = [job for job, _ in fake.calls]
    assert "hold_or_cut" in jobs
    context = dict(fake.calls)["hold_or_cut"]
    assert context["adopted"] is True
    trades = store.rows("trades")
    assert len(trades) == 1 and trades[0]["exit_reason"] == "cut"
    assert trades[0]["entry_price"] == 21_900_000_000.0


def test_no_adoption_when_the_wallet_has_no_coins(tmp_path) -> None:
    """After the coins are sold, a restart must not adopt them again."""
    # given
    cfg = config.load_config(None)
    cfg["bot"]["out_dir"] = str(tmp_path)
    cfg["adopt"]["positions"] = [{"market": "BTCTMN", "qty": 0.00004,
                                  "entry_price": 21_900_000_000.0,
                                  "entry_utc": "1970-01-01T00:00:00Z"}]
    runner = runner_mod.Runner(cfg, mode="paper", store=store_mod.Store(":memory:"),
                               start_ts=0.0, with_twin=False)

    # when
    adopted = asyncio.run(runner.adopt_positions(0.0))

    # then
    assert adopted == []
    assert "BTCTMN" not in runner.bot.slots or not runner.bot.slots["BTCTMN"].busy()


def test_adopted_position_books_its_part_of_the_entry_fee(tmp_path) -> None:
    """The config gives the buy fee of the full position: 1,000 toman for
    0.00004 BTC. The wallet holds 0.00003 BTC, so the bot books 750.
    """
    # given
    cfg = config.load_config(None)
    cfg["bot"]["out_dir"] = str(tmp_path)
    cfg["adopt"]["positions"] = [{"market": "BTCTMN", "qty": 0.00004,
                                  "entry_price": 21_900_000_000.0, "entry_fee": 1000.0,
                                  "entry_utc": "1970-01-01T00:00:00Z"}]
    runner = runner_mod.Runner(cfg, mode="paper", store=store_mod.Store(":memory:"),
                               start_ts=0.0, with_twin=False)
    runner.broker.balance["BTC"] = 0.00003

    # when
    asyncio.run(runner.adopt_positions(0.0))

    # then
    assert runner.bot.slots["BTCTMN"].entry_fees == pytest.approx(750.0)


def test_adopted_position_keeps_the_time_limit_of_the_buy(tmp_path) -> None:
    """The buy was 20 minutes before the restart. The 15-minute limit counts
    from the buy, so the restart does not move it: the bot sells at once.
    """
    # given
    cfg = config.load_config(None)
    cfg["premium"]["min_samples"] = 12
    cfg["bot"]["out_dir"] = str(tmp_path)
    cfg["adopt"]["positions"] = [{"market": "BTCTMN", "qty": 0.00003,
                                  "entry_price": 21_900_000_000.0,
                                  "entry_utc": "1969-12-31T23:40:00Z"}]
    store = store_mod.Store(":memory:")
    runner = runner_mod.Runner(cfg, mode="paper", store=store, start_ts=0.0,
                               with_twin=False)
    runner.broker.balance["BTC"] = 0.00003
    records = sweep_records(sweep_at=10_000.0, end_s=30.0)

    # when
    asyncio.run(runner.adopt_positions(0.0))
    asyncio.run(drive(runner, records, until=20.0))

    # then
    trades = store.rows("trades")
    assert len(trades) == 1 and trades[0]["exit_reason"] == "time"


def test_start_keeps_coins_below_the_smallest_order_as_dust(tmp_path) -> None:
    """Live 2026-10-23: 0.08 ADA stayed after the sale (step 0.1).

    At the start, the bot keeps a free coin below the smallest order of its
    toman market as dust. A larger balance is not the bot's to sell.
    """
    # given
    cfg = config.load_config(None)
    cfg["bot"]["out_dir"] = str(tmp_path)
    runner = runner_mod.Runner(cfg, mode="paper", store=store_mod.Store(":memory:"),
                               start_ts=0.0, with_twin=False)
    runner.broker.balance.update({"ADA": 0.08, "DOGE": 50.0})

    # when
    asyncio.run(runner.seed_dust())

    # then
    assert runner.bot.dust == {"ADA": pytest.approx(0.08)}


def test_dust_goes_to_the_toman_market_and_follows_the_closed_trades(tmp_path) -> None:
    """The rest of a closed trade adds to the dust. A sale of dust lowers it.

    Only the toman market of the coin gets the dust, so two slots of one
    coin do not sell the same dust.
    """
    # given
    cfg = config.load_config(None)
    cfg["bot"]["out_dir"] = str(tmp_path)
    runner = runner_mod.Runner(cfg, mode="paper", store=store_mod.Store(":memory:"),
                               start_ts=0.0, with_twin=False)
    bot = runner.bot

    # when
    bot.keep_dust("ADAUSDT", 0.08)
    after_rest = bot.dust_for("ADATMN"), bot.dust_for("ADAUSDT")
    bot.keep_dust("ADATMN", -0.1)

    # then
    assert after_rest == (pytest.approx(0.08), 0.0)
    assert bot.dust_for("ADATMN") == 0.0


def test_rejected_exit_with_dust_drops_the_dust(tmp_path) -> None:
    """Wallex rejects an exit that asks for more coins than the wallet has.

    The dust can be wrong, so the bot drops it. The next exit sells only
    the position, and a reject loop cannot start.
    """
    # given
    cfg = config.load_config(None)
    cfg["premium"]["min_samples"] = 12
    cfg["bot"]["out_dir"] = str(tmp_path)
    cfg["adopt"]["positions"] = [{"market": "BTCTMN", "qty": 0.00003,
                                  "entry_price": 21_900_000_000.0,
                                  "entry_utc": "1970-01-01T00:00:00Z"}]
    runner = runner_mod.Runner(cfg, mode="paper", store=store_mod.Store(":memory:"),
                               start_ts=0.0, with_twin=False)
    runner.broker.balance["BTC"] = 0.00003
    asyncio.run(runner.adopt_positions(0.0))
    runner.bot.dust["BTC"] = 0.000000005
    asyncio.run(drive(runner, sweep_records(sweep_at=10_000.0, end_s=120.0), until=100.0))
    slot = runner.bot.slots["BTCTMN"]
    exit_qty = slot.exit_qty

    # when
    runner.broker._events.append(broker.OrderEvent("rejected", slot.exit_id, 100.5,
                                                    reason="HTTP 422"))
    asyncio.run(runner.bot.step(100.5))

    # then
    assert exit_qty == pytest.approx(0.000030005)
    assert runner.bot.dust == {}
