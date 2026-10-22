"""Tests for the USDT sale at the end of the challenge."""

import asyncio

import pytest

from lagcatcher import shadow
from sweepbot import broker
from sweepbot import config
from sweepbot import endgame
from sweepbot import rules
from sweepbot import store as store_mod

RULE = rules.MarketRule(price_tick=1.0, qty_step=0.01, min_qty=0.01, min_notional=50_000)
CONVERT_AT = 1000.0


class FakeMarket:
    """Gives one USDTTMN book."""

    def __init__(self, best_bid: float | None = 250_000.0) -> None:
        self.bids = [(best_bid, 100.0)] if best_bid else []

    def book(self, market: str):
        return shadow.Book(bids=self.bids, asks=[(251_000.0, 100.0)], ts=0.0)


def make(usdt: float, best_bid: float | None = 250_000.0):
    """Builds a converter on a paper broker that holds `usdt` USDT."""
    cfg = config.load_config(None)
    paper = broker.PaperBroker(cfg, balances={"USDT": usdt, "TMN": 0.0})
    store = store_mod.Store(":memory:")
    converter = endgame.UsdtConverter(
        convert_at=CONVERT_AT, market=FakeMarket(best_bid), broker_=paper, rule=RULE,
        slippage_pct=2.0, store=store, mode="paper")
    return converter, paper, store


def step(converter, now: float) -> None:
    """Runs one converter step."""
    asyncio.run(converter.step(now))


def test_converter_waits_until_the_convert_time() -> None:
    """Before usdt_convert_at, no order goes out."""
    # given
    converter, paper, _ = make(usdt=10.0)

    # when
    step(converter, CONVERT_AT - 1)

    # then
    assert asyncio.run(paper.open_orders()) == []


def test_converter_sells_the_usdt_below_the_best_bid() -> None:
    """10.005 USDT: sell 10.00 (step 0.01) at 245,000 (2% below 250,000)."""
    # given
    converter, paper, store = make(usdt=10.005)

    # when
    step(converter, CONVERT_AT)
    orders = asyncio.run(paper.open_orders())

    # then
    assert [(o.market, o.side, o.price, o.qty) for o in orders] == [
        ("USDTTMN", "sell", 245_000.0, pytest.approx(10.0))]
    assert "USDT sale" in store.rows("logs")[0]["text"]


def test_converter_order_fills_at_the_bid_and_then_it_is_done() -> None:
    """The limit sells into the 250,000 bid. The rest is below the minimum."""
    # given
    converter, paper, store = make(usdt=10.0)
    step(converter, CONVERT_AT)
    paper.on_book("USDTTMN", FakeMarket().book("USDTTMN"), CONVERT_AT + 0.5)

    # when
    step(converter, CONVERT_AT + 40)
    step(converter, CONVERT_AT + 50)

    # then
    assert paper.balance["USDT"] == pytest.approx(0.0)
    assert paper.balance["TMN"] == pytest.approx(10.0 * 250_000 * (1 - 0.003))
    assert converter.done is True
    assert "done" in store.rows("logs")[-1]["text"]


def test_converter_replaces_an_order_that_stays_open() -> None:
    """No bid at or above the limit: the order is cancelled after 30 s."""
    # given
    converter, paper, _ = make(usdt=10.0)
    step(converter, CONVERT_AT)
    first = converter.order_id

    # when
    step(converter, CONVERT_AT + 31)
    paper.advance(CONVERT_AT + 32)
    step(converter, CONVERT_AT + 37)

    # then
    assert converter.order_id not in (None, first)


def test_converter_stops_below_the_minimum_order_value() -> None:
    """0.1 USDT is 25,000 toman, below the 50,000 minimum. Nothing to sell."""
    # given
    converter, paper, store = make(usdt=0.1)

    # when
    step(converter, CONVERT_AT)

    # then
    assert converter.done is True
    assert asyncio.run(paper.open_orders()) == []
    assert "done" in store.rows("logs")[0]["text"]


def test_converter_waits_without_a_usdttmn_bid() -> None:
    """An empty book gives no price. The converter waits for the next round."""
    # given
    converter, paper, _ = make(usdt=10.0, best_bid=None)

    # when
    step(converter, CONVERT_AT)

    # then
    assert converter.done is False
    assert asyncio.run(paper.open_orders()) == []
