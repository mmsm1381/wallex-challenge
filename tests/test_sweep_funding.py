"""Tests for the USDT purchase at the start of the bot."""

import asyncio

import pytest

from lagcatcher import shadow
from sweepbot import broker
from sweepbot import config
from sweepbot import funding
from sweepbot import rules
from sweepbot import store as store_mod

RULE = rules.MarketRule(price_tick=1.0, qty_step=0.01, min_qty=0.01, min_notional=50_000)
ASK = 250_000.0


class FakeMarket:
    """Gives one USDTTMN book with a 249,000 / 250,000 spread."""

    def __init__(self, best_ask: float | None = ASK) -> None:
        self.asks = [(best_ask, 1_000.0)] if best_ask else []

    def book(self, market: str):
        return shadow.Book(bids=[(249_000.0, 1_000.0)], asks=self.asks, ts=0.0)


def make(usdt: float = 0.0, best_ask: float | None = ASK, stop_at: float | None = None):
    """Builds a funder that wants 40% of 5,000,000 toman in USDT."""
    cfg = config.load_config(None)
    paper = broker.PaperBroker(cfg, balances={"USDT": usdt, "TMN": 5_000_000.0})
    store = store_mod.Store(":memory:")
    funder = funding.UsdtFunder(
        target_tmn=2_000_000.0, market=FakeMarket(best_ask), broker_=paper, rule=RULE,
        slippage_pct=0.3, store=store, mode="paper", stop_at=stop_at)
    return funder, paper, store


def step(funder, now: float) -> None:
    """Runs one funder step."""
    asyncio.run(funder.step(now))


def test_funder_buys_the_missing_usdt_above_the_best_ask() -> None:
    """No USDT yet: buy 2,000,000 / 250,000 = 8.00 USDT, limit 0.3% above the ask.

    Scenario:
        - The account holds no USDT. The target is 2,000,000 toman.
    Expected: one buy of 8.00 USDTTMN at 250,750 (rounded to the 1 toman tick).
    """
    # given
    funder, paper, store = make()

    # when
    step(funder, 0.0)
    orders = asyncio.run(paper.open_orders())

    # then
    assert [(o.market, o.side, o.price, o.qty) for o in orders] == [
        ("USDTTMN", "buy", 250_750.0, pytest.approx(8.0))]
    assert not funder.done
    assert "USDT purchase" in store.rows("logs")[0]["text"]


def test_funder_is_done_when_the_account_already_holds_the_target() -> None:
    """A restart must not buy USDT a second time.

    Scenario:
        - The account holds 7.9 USDT = 1,975,000 toman, inside the 5% tolerance.
    Expected: no order, and the funder is done.
    """
    # given
    funder, paper, _ = make(usdt=7.9)

    # when
    step(funder, 0.0)

    # then
    assert asyncio.run(paper.open_orders()) == []
    assert funder.done


def test_funder_order_fills_and_then_it_is_done() -> None:
    """The buy fills against the 250,000 ask. The next round sees the USDT."""
    # given
    funder, paper, _ = make()
    step(funder, 0.0)
    paper.on_book("USDTTMN", FakeMarket().book("USDTTMN"), 0.5)

    # when
    step(funder, 10.0)

    # then
    assert paper.balance["USDT"] == pytest.approx(8.0)
    assert funder.done


def test_funder_does_nothing_without_an_ask() -> None:
    """The USDTTMN book has no ask (the market is closed): wait, do not buy."""
    # given
    funder, paper, _ = make(best_ask=None)

    # when
    step(funder, 0.0)

    # then
    assert asyncio.run(paper.open_orders()) == []
    assert not funder.done


def test_funder_stops_at_the_stop_time() -> None:
    """From the end-of-challenge flatten time on, the funder never buys."""
    # given
    funder, paper, _ = make(stop_at=100.0)

    # when
    step(funder, 100.0)

    # then
    assert asyncio.run(paper.open_orders()) == []
    assert funder.done


def test_executor_blocks_usdt_bids_until_the_funder_is_done() -> None:
    """With a USDT target, the bot must not bid on ETHUSDT before it holds USDT.

    Scenario:
        - Paper runner with `funding.usdt_target_share` = 0.4.
        - ETHUSDT and BTCTMN have narrow books.
    Expected: the ETHUSDT view says "usdt_funding". The BTCTMN view does not.
    After the funder is done, the ETHUSDT block is gone.
    """
    # given
    from sweepbot import runner as runner_mod
    from sweepbot.ai import schema
    cfg = config.load_config(None)
    cfg["funding"]["usdt_target_share"] = 0.4
    run = runner_mod.Runner(cfg, mode="paper", store=store_mod.Store(":memory:"),
                            start_ts=0.0, with_twin=False)
    for market, bid, ask in (("ETHUSDT", 3999.0, 4001.0),
                             ("BTCTMN", 22_000_000_000.0, 22_010_000_000.0)):
        run.market.on_book(market, "bid", [(bid, 1.0)], 0.0)
        run.market.on_book(market, "ask", [(ask, 1.0)], 0.0)
    slot_plan = {m: schema.SlotPlan(market=m, depth_pct=1.3, size_tmn=950_000.0)
                 for m in ("ETHUSDT", "BTCTMN")}

    def entry_block(market: str) -> str | None:
        from sweepbot import slots
        view = run.bot._view(market, slots.Slot(market, cfg), slot_plan[market], 0.0,
                             open_count=0, usdt_exposure=0.0, equity=5_000_000.0,
                             flatten=False)
        return view.entry_block

    # when
    before = (entry_block("ETHUSDT"), entry_block("BTCTMN"))
    run.bot.funder.done = True
    after = entry_block("ETHUSDT")

    # then
    assert before == ("usdt_funding", None)
    assert after is None
