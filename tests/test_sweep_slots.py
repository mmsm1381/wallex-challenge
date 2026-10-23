"""Tests for the slot state machine: one test per state change of DESIGN section 5."""

import asyncio

import pytest

from lagcatcher import shadow
from sweepbot import broker
from sweepbot import config
from sweepbot import exits
from sweepbot import slots

FV = 1000.0


class Harness:
    """A slot on BTCTMN with a paper broker and a large order budget."""

    def __init__(self) -> None:
        self.cfg = config.load_config(None)
        self.paper = broker.PaperBroker(self.cfg)
        self.slot = slots.Slot("BTCTMN", self.cfg)
        self.budget = slots.OrderBudget(per_10s=1000, reserve=5)
        self.fill_ids = iter(range(1, 1000))

    def view(self, **changes) -> slots.SlotView:
        """Returns a view with FV 1000, a 990/1010 book and a 1-coin bid."""
        values = {"fv": FV, "best_bid": 990.0, "best_ask": 1010.0, "want": True,
                  "depth_pct": 1.3, "qty_for": lambda price: 1.0}
        values.update(changes)
        return slots.SlotView(**values)

    def step(self, now: float, **changes) -> None:
        """Runs one slot step at `now`."""
        asyncio.run(self.slot.step(now, self.view(**changes), self.paper, self.budget))

    def pump(self, now: float) -> list:
        """Moves the paper broker to `now` and gives its events to the slot."""
        self.paper.advance(now)
        events = asyncio.run(self.paper.poll())
        for event in events:
            self.slot.on_event(event, next(self.fill_ids) if event.kind == "fill" else None)
        return events

    def sweep(self, now: float, price: float) -> None:
        """A seller trades at `price`."""
        self.paper.on_trade("BTCTMN", price, 5.0, True, now)
        self.pump(now)

    def buyer(self, now: float, price: float) -> None:
        """A buyer trades at `price`."""
        self.paper.on_trade("BTCTMN", price, 5.0, False, now)
        self.pump(now)

    def live_bid(self) -> None:
        """Brings the slot to BID_LIVE at t=1."""
        self.step(0.0)
        self.pump(1.0)

    def in_exit(self) -> None:
        """Brings the slot to EXIT_LIVE with 1 coin bought at 987."""
        self.live_bid()
        self.sweep(2.0, 980.0)
        self.step(2.1)
        self.pump(3.0)


def test_idle_slot_sends_bid_at_depth_below_fair_value() -> None:
    """Idle -> BidPending: bid = FV x (1 - 1.3%)."""
    # given
    harness = Harness()

    # when
    harness.step(0.0)

    # then
    assert harness.slot.state == slots.BID_PENDING
    assert harness.slot.bid_price == pytest.approx(987.0)
    assert harness.slot.bid_qty == 1.0


@pytest.mark.parametrize("changes", [
    {"want": False}, {"block": "global_drop"}, {"entry_block": "slots full"},
    {"fv": None}, {"best_ask": 980.0},
])
def test_idle_slot_does_not_bid_when_something_blocks(changes: dict) -> None:
    """No bid without the plan, under a filter or a risk limit, before warm-up,
    or when the bid would cross the ask."""
    # given
    harness = Harness()

    # when
    harness.step(0.0, **changes)

    # then
    assert harness.slot.state == slots.IDLE
    assert harness.slot.bid_id is None


def test_pending_bid_goes_live_when_the_exchange_confirms() -> None:
    """BidPending -> BidLive on the live event."""
    # given
    harness = Harness()
    harness.step(0.0)

    # when
    harness.pump(0.5)

    # then
    assert harness.slot.state == slots.BID_LIVE


def test_live_bid_is_requoted_when_fair_value_moves_more_than_0_2_pct() -> None:
    """BidLive -> Cancelling -> Idle -> BidPending at the new price."""
    # given
    harness = Harness()
    harness.live_bid()

    # when
    harness.step(1.1, fv=1003.0)
    state_after_cancel = harness.slot.state
    harness.pump(1.6)
    state_after_confirm = harness.slot.state
    harness.step(1.7, fv=1003.0)

    # then
    assert state_after_cancel == slots.CANCELLING
    assert state_after_confirm == slots.IDLE
    assert harness.slot.state == slots.BID_PENDING
    assert harness.slot.bid_price == pytest.approx(1003.0 * 0.987)


def test_live_bid_stays_when_fair_value_moves_0_1_pct() -> None:
    """A deep bid does not follow small moves. This saves the order budget."""
    # given
    harness = Harness()
    harness.live_bid()

    # when
    harness.step(1.1, fv=1001.0)

    # then
    assert harness.slot.state == slots.BID_LIVE


@pytest.mark.parametrize("changes", [{"block": "fx_shock"}, {"want": False}])
def test_live_bid_is_cancelled_on_a_filter_or_plan_change(changes: dict) -> None:
    """BidLive -> Cancelling -> Idle when a filter trips or the plan drops the slot."""
    # given
    harness = Harness()
    harness.live_bid()

    # when
    harness.step(1.1, **changes)
    cancelling = harness.slot.state
    harness.pump(1.6)

    # then
    assert cancelling == slots.CANCELLING
    assert harness.slot.state == slots.IDLE


def test_bid_fill_opens_position_and_exit_goes_at_fair_value() -> None:
    """BidLive -> InPosition on the sweep -> ExitLive with a sell at FV."""
    # given
    harness = Harness()
    harness.live_bid()

    # when
    harness.sweep(2.0, 980.0)
    in_position = harness.slot.state
    harness.step(2.1)

    # then
    assert in_position == slots.IN_POSITION
    assert harness.slot.state == slots.EXIT_LIVE
    assert harness.slot.exit_price == FV
    assert harness.slot.exit_kind == exits.TARGET
    assert harness.slot.entry_price() == pytest.approx(987.0)


def test_part_fill_cancels_the_rest_and_exits_the_filled_coins() -> None:
    """A book cross fills 0.4 of 1.0. The rest of the bid is cancelled first."""
    # given
    harness = Harness()
    harness.live_bid()
    harness.paper.on_book("BTCTMN", shadow.Book(bids=[(980.0, 1.0)],
                                                asks=[(987.0, 0.4), (995.0, 3.0)], ts=2.0), 2.0)
    harness.pump(2.0)

    # when
    harness.step(2.1)
    exit_before_cancel = harness.slot.exit_id
    harness.pump(2.7)
    harness.step(2.8)

    # then
    assert exit_before_cancel is None
    assert harness.slot.state == slots.EXIT_LIVE
    assert harness.slot.qty == pytest.approx(0.4)
    orders = asyncio.run(harness.paper.open_orders())
    assert [(o.side, o.qty) for o in orders] == [("sell", pytest.approx(0.4))]


def test_full_fill_with_fee_in_coin_goes_to_exit_without_cancel() -> None:
    """Live 2026-10-23: ADATMN filled 7.9, the fee was 0.01975 ADA.

    The fill event carries 7.88025 ADA. The bid is filled in full, so the
    slot must send the exit at once. A cancel of a filled order gives no
    event, so a wait for it would keep the slot in IN_POSITION.
    """
    # given
    harness = Harness()
    harness.live_bid()
    bid_id = harness.slot.bid_id
    event = broker.OrderEvent("fill", bid_id, 2.0, price=987.0, qty=0.9975,
                              fee=0.0, fee_qty=0.0025)

    # when
    harness.slot.on_event(event, 1)
    harness.step(2.1)

    # then
    assert harness.slot.bid_id is None
    assert harness.slot.state == slots.EXIT_LIVE
    assert harness.slot.qty == pytest.approx(0.9975)


def test_exit_also_sells_the_dust_of_earlier_trades() -> None:
    """A fee in the coin leaves less than one quantity step after a sale.

    The executor gives that rest to the next exit as `dust_qty`. The exit
    sells 1.0 + 0.5. The trade closes with a rest of -0.5: the dust is used.
    """
    # given
    harness = Harness()
    harness.live_bid()
    harness.sweep(2.0, 980.0)

    # when
    harness.step(2.1, dust_qty=0.5)
    harness.pump(3.0)
    orders = asyncio.run(harness.paper.open_orders())
    exit_qty = harness.slot.exit_qty
    harness.buyer(4.0, 1005.0)

    # then
    assert [(o.side, o.qty) for o in orders] == [("sell", pytest.approx(1.5))]
    assert exit_qty == pytest.approx(1.5)
    assert harness.slot.closed[0].leftover_qty == pytest.approx(-0.5)


def test_exit_is_requoted_when_fair_value_moves() -> None:
    """ExitLive -> ExitLive at the new fair value."""
    # given
    harness = Harness()
    harness.in_exit()

    # when
    harness.step(3.1, fv=1005.0)
    harness.pump(3.6)
    harness.step(3.7, fv=1005.0)

    # then
    assert harness.slot.state == slots.EXIT_LIVE
    assert harness.slot.exit_price == 1005.0


def test_exit_fill_closes_the_trade_with_net_after_fees() -> None:
    """ExitLive -> Closed: 987 -> 1000 less 0.25% maker fee on both sides."""
    # given
    harness = Harness()
    harness.in_exit()

    # when
    harness.buyer(4.0, 1000.5)

    # then
    assert harness.slot.state == slots.CLOSED
    trade = harness.slot.closed[0]
    assert trade.net_quote == pytest.approx(13.0 - 0.0025 * 987.0 - 0.0025 * 1000.0)
    assert trade.exit_reason == exits.TARGET
    assert trade.entry_fill_ids == [1] and trade.exit_fill_ids == [2]


def test_cut_replaces_the_exit_with_a_taker_sell() -> None:
    """ExitLive -> ExitCancelling -> InPosition -> ExitLive (cut) -> Closed."""
    # given
    harness = Harness()
    harness.in_exit()
    harness.paper.on_book("BTCTMN", shadow.Book(bids=[(985.0, 5.0)], asks=[(995.0, 5.0)],
                                                ts=3.0), 3.0)

    # when
    harness.step(3.1, cut=exits.TIME, best_bid=985.0)
    harness.pump(3.6)
    harness.step(3.7, cut=exits.TIME, best_bid=985.0)
    harness.pump(4.2)

    # then
    assert harness.slot.state == slots.CLOSED
    trade = harness.slot.closed[0]
    assert trade.exit_reason == exits.TIME
    assert trade.exit_price == 985.0
    assert trade.net_quote < 0


def test_cooldown_after_win_is_60_s_and_after_loss_is_5_minutes() -> None:
    """Closed -> Idle when the cooldown ends."""
    # given
    win = Harness()
    win.in_exit()
    win.buyer(4.0, 1000.5)

    # when
    win.step(63.9)
    before = win.slot.state
    win.step(64.0)

    # then
    assert before == slots.CLOSED
    assert win.slot.state == slots.BID_PENDING
    loss_cooldown = win.cfg["slots"]["loss_cooldown_s"]
    assert loss_cooldown == 300.0


def test_order_budget_keeps_a_reserve_for_safety_cancels() -> None:
    """Normal requests stop at the reserve. Safety cancels may use it."""
    # given
    budget = slots.OrderBudget(per_10s=2, reserve=1)

    # when
    normal = [budget.take(0.0), budget.take(0.0), budget.take(0.0)]
    safety = budget.take(0.0, safety=True)

    # then
    assert normal == [True, True, False]
    assert safety is True
    assert budget.take(0.0, safety=True) is False


def test_rejected_bid_waits_for_the_reject_cooldown_before_next_bid() -> None:
    """Wallex rejects the bid (for example: not enough USDT in the account).

    Scenario:
        - The slot sends a bid at t=0. The broker rejects it at t=0.
        - Without a wait, the slot sends the same bid on every 100 ms tick,
          and the rejects use up the order budget of the other slots.
    Expected: no new bid at t=1. A new bid after the reject cooldown.
    """
    # given
    harness = Harness()
    harness.step(0.0)
    rejected = broker.OrderEvent("rejected", harness.slot.bid_id, 0.0, reason="HTTP 400")
    harness.slot.on_event(rejected)

    # when
    harness.step(1.0)
    state_after_1s = harness.slot.state
    harness.step(harness.cfg["slots"]["reject_cooldown_s"] + 1.0)

    # then
    assert state_after_1s == slots.IDLE
    assert harness.slot.state == slots.BID_PENDING
