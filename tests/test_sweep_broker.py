"""Tests for the paper broker: latency, fill rules, part fills and cancel."""

import asyncio

import pytest

from lagcatcher import shadow
from sweepbot import broker
from sweepbot import config


def make_broker() -> broker.PaperBroker:
    """Builds a paper broker with a latency of 0.5 s and the real fees."""
    return broker.PaperBroker(config.load_config(None),
                              balances={"TMN": 5_000_000.0})


def book(bids: list, asks: list, ts: float = 0.0) -> shadow.Book:
    """Builds a book from (price, qty) levels."""
    return shadow.Book(bids=bids, asks=asks, ts=ts)


def kinds(events: list) -> list[str]:
    """Returns the kinds of a list of order events."""
    return [event.kind for event in events]


def place(paper: broker.PaperBroker, *args) -> str:
    """Places an order on the paper broker and returns its id."""
    return asyncio.run(paper.place_limit(*args))


def poll(paper: broker.PaperBroker) -> list:
    """Returns the new events of the paper broker."""
    return asyncio.run(paper.poll())


def test_bid_goes_live_after_the_latency() -> None:
    """A new order is pending for paper.latency_s, then live."""
    # given
    paper = make_broker()
    paper.on_book("BTCTMN", book([(100.0, 1.0)], [(101.0, 1.0)]), 0.0)
    order_id = place(paper, "BTCTMN", "buy", 98.0, 1.0)

    # when
    paper.advance(0.4)
    early = poll(paper)
    paper.advance(0.5)
    late = poll(paper)

    # then
    assert early == []
    assert kinds(late) == ["live"]
    assert late[0].order_id == order_id


def test_seller_trade_strictly_below_the_bid_fills_it_in_full() -> None:
    """A sweep that trades below our price proves that our bid filled first."""
    # given
    paper = make_broker()
    paper.on_book("BTCTMN", book([(100.0, 1.0)], [(101.0, 1.0)]), 0.0)
    order_id = place(paper, "BTCTMN", "buy", 98.0, 2.0)
    paper.advance(1.0)
    poll(paper)

    # when
    paper.on_trade("BTCTMN", 97.9, 0.1, True, 2.0)
    events = poll(paper)

    # then
    assert kinds(events) == ["fill"]
    fill = events[0]
    assert (fill.order_id, fill.price, fill.qty, fill.maker) == (order_id, 98.0, 2.0, True)
    assert fill.fee == pytest.approx(0.0025 * 98.0 * 2.0)
    assert paper.balance["BTC"] == 2.0


@pytest.mark.parametrize("price, buyer_maker", [(98.0, True), (97.0, False)])
def test_trade_at_the_bid_or_buyer_initiated_is_no_fill(price: float, buyer_maker: bool) -> None:
    """A trade at exactly our price, or a buyer taking an ask, does not fill a bid."""
    # given
    paper = make_broker()
    place(paper, "BTCTMN", "buy", 98.0, 1.0)
    paper.advance(1.0)
    poll(paper)

    # when
    paper.on_trade("BTCTMN", price, 1.0, buyer_maker, 2.0)

    # then
    assert poll(paper) == []


def test_buyer_trade_strictly_above_the_sell_fills_it() -> None:
    """The exit at fair value fills when a buyer pays more than it."""
    # given
    paper = make_broker()
    place(paper, "BTCTMN", "sell", 100.0, 1.0)
    paper.advance(1.0)
    poll(paper)

    # when
    paper.on_trade("BTCTMN", 100.5, 0.01, False, 2.0)
    events = poll(paper)

    # then
    assert kinds(events) == ["fill"]
    assert (events[0].price, events[0].qty, events[0].maker) == (100.0, 1.0, True)


def test_ask_falling_to_the_bid_fills_part_of_it() -> None:
    """A book cross fills only the quantity of the asks at or below the bid."""
    # given
    paper = make_broker()
    place(paper, "BTCTMN", "buy", 98.0, 2.0)
    paper.advance(1.0)
    poll(paper)

    # when
    paper.on_book("BTCTMN", book([(97.0, 1.0)], [(98.0, 0.5), (99.0, 5.0)]), 2.0)
    events = poll(paper)
    orders = asyncio.run(paper.open_orders())

    # then
    assert kinds(events) == ["fill"]
    assert (events[0].price, events[0].qty, events[0].maker) == (98.0, 0.5, True)
    assert orders[0].filled == 0.5 and orders[0].status == "open"


def test_crossing_order_trades_as_taker_and_rests_the_rest() -> None:
    """A sell below the best bid trades at once as a taker (the cut exit)."""
    # given
    paper = make_broker()
    paper.on_book("BTCTMN", book([(100.0, 0.4), (99.0, 0.4)], [(101.0, 1.0)]), 0.0)
    place(paper, "BTCTMN", "sell", 99.0, 1.0)

    # when
    paper.advance(0.5)
    events = poll(paper)

    # then
    assert kinds(events) == ["live", "fill"]
    fill = events[1]
    assert fill.qty == pytest.approx(0.8)
    assert fill.price == pytest.approx(99.5)
    assert fill.maker is False
    assert fill.fee == pytest.approx(0.003 * 99.5 * 0.8)


def test_cancel_takes_effect_after_the_latency_and_can_still_fill() -> None:
    """Until the cancel arrives, a sweep can still fill the bid."""
    # given
    paper = make_broker()
    order_id = place(paper, "BTCTMN", "buy", 98.0, 1.0)
    paper.advance(1.0)
    poll(paper)
    asyncio.run(paper.cancel(order_id))

    # when
    paper.on_trade("BTCTMN", 97.0, 1.0, True, 1.2)
    events = poll(paper)

    # then
    assert kinds(events) == ["fill"]


def test_cancel_removes_the_order_after_the_latency() -> None:
    """A confirmed cancel gives a cancelled event and no more fills."""
    # given
    paper = make_broker()
    order_id = place(paper, "BTCTMN", "buy", 98.0, 1.0)
    paper.advance(1.0)
    poll(paper)
    asyncio.run(paper.cancel(order_id))

    # when
    paper.advance(1.5)
    paper.on_trade("BTCTMN", 97.0, 1.0, True, 1.6)
    events = poll(paper)

    # then
    assert kinds(events) == ["cancelled"]
    assert asyncio.run(paper.open_orders()) == []


def test_bad_order_is_rejected() -> None:
    """An order with a zero quantity gives a rejected event."""
    # given
    paper = make_broker()

    # when
    place(paper, "BTCTMN", "buy", 98.0, 0.0)

    # then
    assert kinds(poll(paper)) == ["rejected"]


def test_paper_order_ids_differ_between_two_runs() -> None:
    """A restarted bot writes to the same store. Its paper ids must be new.

    Before, each run started again at "twin-1", and the store refused the
    order with "UNIQUE constraint failed: orders.id".
    """
    # given
    cfg = config.load_config(None)
    first = broker.PaperBroker(cfg, name="twin")
    second = broker.PaperBroker(cfg, name="twin")

    # when
    first_id = asyncio.run(first.place_limit("BTCTMN", broker.BUY, 100.0, 1.0))
    second_id = asyncio.run(second.place_limit("BTCTMN", broker.BUY, 100.0, 1.0))

    # then
    assert first_id != second_id
    assert first_id.startswith("twin-")
