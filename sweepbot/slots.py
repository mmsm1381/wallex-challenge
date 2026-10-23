"""Bid manager of the sweep bot: one state machine per slot (DESIGN section 5).

A slot is one market of the plan. `Slot.step` runs on every executor tick
and sends orders through the broker. `Slot.on_event` takes the order events
of the broker and changes the state:

    IDLE --bid sent--> BID_PENDING --live--> BID_LIVE
    BID_LIVE --fair value moved more than the requote limit--> CANCELLING
        --cancel confirmed--> IDLE, and the next step sends the new bid
    BID_PENDING or BID_LIVE --filter, stale feed, plan removed slot,
        pause or halt--> CANCELLING --cancel confirmed--> IDLE
    BID_PENDING, BID_LIVE or CANCELLING --fill--> IN_POSITION
    IN_POSITION --exit sent--> EXIT_LIVE
    EXIT_LIVE --fair value moved, cut or time limit--> EXIT_CANCELLING
        --cancel confirmed--> IN_POSITION, and the next step sends the
        new exit
    EXIT_LIVE --exit filled--> CLOSED --cooldown ends--> IDLE

A part fill of the bid cancels the rest of the bid. The exit then sells
all filled coins. The cooldown is 60 s after a closed trade and 5 minutes
after a loss. A rejected bid also starts a cooldown (`reject_cooldown_s`).
Without it, a bid that Wallex always rejects (for example for missing
funds) is sent again on every tick and uses up the order budget.
"""

import dataclasses

from sweepbot import broker
from sweepbot import exits

IDLE = "idle"
BID_PENDING = "bid_pending"
BID_LIVE = "bid_live"
CANCELLING = "cancelling"
IN_POSITION = "in_position"
EXIT_LIVE = "exit_live"
EXIT_CANCELLING = "exit_cancelling"
CLOSED = "closed"

# A position below this part of the bought quantity counts as sold.
_DUST = 1e-9


class OrderBudget:
    """Token bucket for order requests, with a reserve for safety cancels.

    Normal requests (new bids, requotes, exits) may use the bucket only
    down to the reserve. Safety cancels (filter, stale feed, pause) may use
    the reserve too. DESIGN section 5: 15 per 10 s plus a reserve of 5.
    """

    def __init__(self, per_10s: int, reserve: int) -> None:
        """Creates a full bucket of `per_10s` + `reserve` requests."""
        self._capacity = float(per_10s + reserve)
        self._rate = per_10s / 10.0
        self._reserve = float(reserve)
        self._tokens = self._capacity
        self._last: float | None = None
        self.refused = 0

    def take(self, now: float, safety: bool = False) -> bool:
        """Uses one request when the budget allows it."""
        if self._last is not None:
            self._tokens = min(self._capacity,
                               self._tokens + (now - self._last) * self._rate)
        self._last = now
        floor = 1.0 if safety else 1.0 + self._reserve
        if self._tokens < floor:
            self.refused += 1
            return False
        self._tokens -= 1
        return True


def bid_price(fv: float, depth_pct: float, tick: float = 0.0) -> float:
    """Returns the bid price: FV x (1 - depth / 100), rounded down."""
    return exits.round_price(fv * (1 - depth_pct / 100), tick, up=False)


def moved_more_than(current: float, desired: float, limit_pct: float) -> bool:
    """Returns True when `desired` differs from `current` by more than the limit."""
    return abs(desired / current - 1) * 100 > limit_pct


@dataclasses.dataclass
class SlotView:
    """What one slot needs to know at one executor tick.

    Attributes:
        fv: The fair value, or None before warm-up.
        best_bid: The best Wallex bid, or None.
        best_ask: The best Wallex ask, or None.
        want: True when the plan has the slot and the bot may trade.
        block: A reason to cancel the bid now (filter, stale feed), or None.
        entry_block: A reason not to send a new bid (risk, wide spread),
            or None. A live bid stays.
        depth_pct: The depth of the bid in the plan.
        qty_for: A function of the bid price that returns the bid
            quantity, or None when the size is not known.
        cut: The reason to cut the position now, or None.
        dust_qty: Coins of earlier trades below one quantity step. The
            exit sells them with the position.
    """

    fv: float | None
    best_bid: float | None
    best_ask: float | None
    want: bool
    block: str | None = None
    entry_block: str | None = None
    depth_pct: float = 1.3
    qty_for: object = None
    cut: str | None = None
    dust_qty: float = 0.0


@dataclasses.dataclass
class ClosedTrade:
    """One finished round trip of a slot."""

    market: str
    entry_ts: float
    exit_ts: float
    qty: float
    entry_price: float
    exit_price: float
    entry_fees: float
    exit_fees: float
    net_quote: float
    net_pct: float
    exit_reason: str
    entry_fill_ids: list
    exit_fill_ids: list
    # The coins left after the exit. Below zero: the exit also sold dust.
    leftover_qty: float = 0.0


class Slot:
    """State machine of one market."""

    def __init__(self, market: str, cfg: dict, tick: float = 0.0,
                 min_qty: float = 0.0) -> None:
        """Creates an idle slot.

        Args:
            market: The Wallex market, such as "BTCTMN".
            cfg: The config dict from `sweepbot.config.load_config`.
            tick: The price tick of the market (0: no rounding).
            min_qty: The smallest order quantity. A rest below it cannot be
                sold, so the trade counts as closed.
        """
        self.market = market
        self.tick = tick
        self.min_qty = min_qty
        slots = cfg["slots"]
        self._requote_pct = slots["requote_pct"]
        self._cooldown_s = slots["cooldown_s"]
        self._loss_cooldown_s = slots["loss_cooldown_s"]
        self._reject_cooldown_s = slots["reject_cooldown_s"]
        self._cut_slippage_pct = slots["cut_slippage_pct"]
        self.state = IDLE
        self.order_ts: float | None = None
        self.cooldown_until = 0.0
        self.closed: list[ClosedTrade] = []
        self.log: list[str] = []
        self._reset_trade()

    def _reset_trade(self) -> None:
        """Clears the orders and the position of a finished trade."""
        self.bid_id: str | None = None
        self.bid_price: float | None = None
        self.bid_qty = 0.0
        self.bid_filled = 0.0
        self.after_cancel: str | None = None
        self.exit_id: str | None = None
        self.exit_price: float | None = None
        self.exit_kind: str | None = None
        self.exit_qty = 0.0
        self.qty = 0.0
        self.bought = 0.0
        self.entry_cost = 0.0
        self.entry_fees = 0.0
        self.entry_ts: float | None = None
        self.exit_value = 0.0
        self.exit_sold = 0.0
        self.exit_fees = 0.0
        self.entry_fill_ids: list = []
        self.exit_fill_ids: list = []
        self.hold_or_cut_asked = False
        self.cut_requested = False
        self.adopted = False

    # Queries ---------------------------------------------------------------

    def order_ids(self) -> list[str]:
        """Returns the ids of the orders that the slot owns."""
        return [order_id for order_id in (self.bid_id, self.exit_id) if order_id]

    def has_position(self) -> bool:
        """Returns True while the slot holds a quantity that it can sell."""
        return self.qty > max(_DUST * max(self.bought, 1e-30), self.min_qty * (1 - 1e-9))

    def entry_price(self) -> float | None:
        """Returns the average entry price, or None."""
        return self.entry_cost / self.bought if self.bought else None

    def busy(self) -> bool:
        """Returns True when the slot has an order or a position."""
        return self.state not in (IDLE, CLOSED)

    def adopt(self, qty: float, entry_price: float, entry_ts: float,
              entry_fee: float = 0.0) -> None:
        """Takes over coins that a restart lost, as an open position.

        The next step sends the exit.

        Args:
            qty: The coins to sell.
            entry_price: The buy price.
            entry_ts: The time of the buy. The time limit counts from it, so
                a restart does not move the limit.
            entry_fee: The buy fee of `qty` in the quote currency.
        """
        self._reset_trade()
        self.qty = qty
        self.bought = qty
        self.entry_cost = entry_price * qty
        self.entry_fees = entry_fee
        self.entry_ts = entry_ts
        self.adopted = True
        self.state = IN_POSITION
        self.log.append(f"adopt {entry_price:.10g} x {qty:.10g}")

    # Tick ------------------------------------------------------------------

    async def step(self, now: float, view: SlotView, broker_: broker.Broker,
                   budget: OrderBudget) -> None:
        """Sends the orders that the state and the view need."""
        if self.state == CLOSED and now >= self.cooldown_until:
            self.state = IDLE
        if self.state == IDLE:
            await self._maybe_bid(now, view, broker_, budget)
        elif self.state in (BID_PENDING, BID_LIVE):
            await self._manage_bid(now, view, broker_, budget)
        elif self.state == IN_POSITION:
            await self._place_exit(now, view, broker_, budget)
        elif self.state == EXIT_LIVE:
            await self._manage_exit(now, view, broker_, budget)

    async def _maybe_bid(self, now: float, view: SlotView,
                         broker_: broker.Broker, budget: OrderBudget) -> None:
        """Sends a new bid when the plan wants one and nothing blocks it."""
        if (now < self.cooldown_until or not view.want or view.block
                or view.entry_block or view.fv is None):
            return
        price = bid_price(view.fv, view.depth_pct, self.tick)
        # A bid at or above the ask would buy as a taker. The edge is the
        # resting bid, so the slot waits.
        if view.best_ask is not None and price >= view.best_ask:
            return
        qty = view.qty_for(price) if view.qty_for else None
        if not qty or qty <= 0:
            return
        if not budget.take(now):
            return
        self.bid_id = await broker_.place_limit(self.market, broker.BUY, price, qty)
        self.order_ts = now
        self.bid_price = price
        self.bid_qty = qty
        self.bid_filled = 0.0
        self.state = BID_PENDING
        self.log.append(f"bid {price:.10g} x {qty:.10g}")

    async def _manage_bid(self, now: float, view: SlotView,
                          broker_: broker.Broker, budget: OrderBudget) -> None:
        """Cancels the bid for safety, or moves it after fair value."""
        if not view.want or view.block:
            if budget.take(now, safety=True):
                await broker_.cancel(self.bid_id)
                self.after_cancel = IDLE
                self.state = CANCELLING
                self.log.append(f"cancel bid: {view.block or 'not wanted'}")
            return
        if view.fv is None:
            return
        desired = bid_price(view.fv, view.depth_pct, self.tick)
        if not moved_more_than(self.bid_price, desired, self._requote_pct):
            return
        if budget.take(now):
            await broker_.cancel(self.bid_id)
            self.after_cancel = "requote"
            self.state = CANCELLING
            self.log.append(f"requote bid {self.bid_price:.10g} -> {desired:.10g}")

    async def _place_exit(self, now: float, view: SlotView,
                          broker_: broker.Broker, budget: OrderBudget) -> None:
        """Sends the exit for the coins held, after the bid is done."""
        if self.bid_id is not None:
            # The rest of a part-filled bid must go first.
            if self.after_cancel is None and budget.take(now, safety=True):
                await broker_.cancel(self.bid_id)
                self.after_cancel = IN_POSITION
            return
        if not self.has_position():
            return
        if view.cut:
            if view.best_bid is None or not budget.take(now, safety=True):
                return
            price = exits.cut_price(view.best_bid, self._cut_slippage_pct, self.tick)
            kind = view.cut
        else:
            if view.fv is None or not budget.take(now):
                return
            price = exits.exit_target(view.fv, self.tick)
            kind = exits.TARGET
        # The broker rounds the quantity down to the step. The rest stays as
        # dust for the next exit.
        qty = self.qty + view.dust_qty
        self.exit_id = await broker_.place_limit(self.market, broker.SELL, price, qty)
        self.order_ts = now
        self.exit_price = price
        self.exit_kind = kind
        self.exit_qty = qty
        self.state = EXIT_LIVE
        self.log.append(f"exit {kind} {price:.10g} x {qty:.10g}")

    async def _manage_exit(self, now: float, view: SlotView,
                           broker_: broker.Broker, budget: OrderBudget) -> None:
        """Moves the exit after fair value, or replaces it with a cut."""
        if self.exit_kind != exits.TARGET:
            return
        if view.cut:
            if budget.take(now, safety=True):
                await broker_.cancel(self.exit_id)
                self.state = EXIT_CANCELLING
                self.log.append(f"cancel exit for {view.cut}")
            return
        if view.fv is None:
            return
        desired = exits.exit_target(view.fv, self.tick)
        if moved_more_than(self.exit_price, desired, self._requote_pct) and budget.take(now):
            await broker_.cancel(self.exit_id)
            self.state = EXIT_CANCELLING
            self.log.append(f"requote exit {self.exit_price:.10g} -> {desired:.10g}")

    # Events ----------------------------------------------------------------

    def on_event(self, event: broker.OrderEvent, fill_id: object = None) -> None:
        """Applies one broker event of an order of this slot.

        Args:
            event: The order event.
            fill_id: The store id of a fill event, for the trade record.
        """
        if event.order_id == self.bid_id:
            self._on_bid_event(event, fill_id)
        elif event.order_id == self.exit_id:
            self._on_exit_event(event, fill_id)

    def _on_bid_event(self, event: broker.OrderEvent, fill_id: object) -> None:
        """Applies an event of the bid."""
        if event.kind == "live":
            if self.state == BID_PENDING:
                self.state = BID_LIVE
        elif event.kind == "fill":
            if self.entry_ts is None:
                self.entry_ts = event.ts
            self.qty += event.qty
            self.bought += event.qty
            # The order filled the coins held plus a fee in the coin.
            self.bid_filled += event.qty + event.fee_qty
            self.entry_cost += event.price * event.qty
            self.entry_fees += event.fee
            self.entry_fill_ids.append(fill_id)
            if self.bid_filled >= self.bid_qty * (1 - _DUST):
                self.bid_id = None
                self.after_cancel = None
            elif self.state != CANCELLING:
                self.after_cancel = None
            self.state = IN_POSITION
            self.log.append(f"bid fill {event.price:.10g} x {event.qty:.10g}")
        elif event.kind in ("cancelled", "rejected"):
            self.bid_id = None
            after = self.after_cancel
            self.after_cancel = None
            if self.has_position():
                self.state = IN_POSITION
            else:
                self.state = IDLE
                if event.kind == "rejected":
                    self.cooldown_until = event.ts + self._reject_cooldown_s
                self.log.append(f"bid {event.kind}"
                                + (" for requote" if after == "requote" else "")
                                + (f": {event.reason}" if event.reason else ""))

    def _on_exit_event(self, event: broker.OrderEvent, fill_id: object) -> None:
        """Applies an event of the exit."""
        if event.kind == "fill":
            self.qty -= event.qty
            self.exit_sold += event.qty
            self.exit_value += event.price * event.qty
            self.exit_fees += event.fee
            self.exit_fill_ids.append(fill_id)
            self.log.append(f"exit fill {event.price:.10g} x {event.qty:.10g}")
            if not self.has_position():
                self._close(event.ts)
        elif event.kind in ("cancelled", "rejected"):
            self.exit_id = None
            if self.has_position():
                self.state = IN_POSITION
            elif self.bought:
                self._close(event.ts)

    def _close(self, now: float) -> None:
        """Books the closed trade and starts the cooldown."""
        net = self.exit_value - self.entry_cost - self.entry_fees - self.exit_fees
        trade = ClosedTrade(
            market=self.market, entry_ts=self.entry_ts, exit_ts=now,
            qty=self.bought, entry_price=self.entry_cost / self.bought,
            exit_price=self.exit_value / self.exit_sold if self.exit_sold else 0.0,
            entry_fees=self.entry_fees, exit_fees=self.exit_fees,
            net_quote=net, net_pct=100 * net / self.entry_cost,
            exit_reason=self.exit_kind or exits.TARGET,
            entry_fill_ids=list(self.entry_fill_ids),
            exit_fill_ids=list(self.exit_fill_ids),
            leftover_qty=self.qty)
        self.closed.append(trade)
        self.cooldown_until = now + (self._loss_cooldown_s if net < 0 else self._cooldown_s)
        self._reset_trade()
        self.state = CLOSED
