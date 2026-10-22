"""Brokers of the sweep bot: the `Broker` protocol and the paper broker.

The executor talks only to the `Broker` protocol, so the same code runs in
live, paper and replay mode (DESIGN section 11). A broker reports order
changes as `OrderEvent`s from `poll`: an order goes live, fills (in full
or in part), is cancelled, or is rejected.

`PaperBroker` simulates the Wallex matching with the conservative rules of
`lagcatcher.shadow`:

* A new order and a cancel take effect after `paper.latency_s`. Until a
  cancel takes effect, the order can still fill.
* An order that crosses the book when it goes live trades at once as a
  taker against the visible levels. The rest of the order rests.
* A resting buy fills in full when a seller trades strictly below its
  price. A trade at exactly our price is not a fill, because our place in
  the queue is unknown. A resting sell fills in full when a buyer trades
  strictly above its price.
* A resting buy also fills when the best ask falls to its price, up to the
  quantity of the asks at or below it. A resting sell fills the same way
  against the bids.

Fees use the challenge account rates (maker or taker) in the quote
currency. `WallexBroker` comes in milestone 2, after the API documentation
is read (see `docs/IMPLEMENTATION_PLAN.md`).
"""

import dataclasses
import itertools
import time
from collections.abc import Callable
from typing import Protocol

from lagcatcher import shadow
from sweepbot import config

BUY = "buy"
SELL = "sell"

PENDING = "pending"
OPEN = "open"
FILLED = "filled"
CANCELLED = "cancelled"
REJECTED = "rejected"

# Below this part of the order size, the rest of an order counts as done.
_DUST = 1e-9


@dataclasses.dataclass
class Order:
    """One limit order and its state.

    Attributes:
        id: The broker order id.
        market: The Wallex market, such as "BTCTMN".
        side: BUY or SELL.
        price: The limit price in the quote currency.
        qty: The order quantity in the base coin.
        ts: The time the order was sent.
        filled: The filled quantity.
        status: PENDING, OPEN, FILLED, CANCELLED or REJECTED.
    """

    id: str
    market: str
    side: str
    price: float
    qty: float
    ts: float
    filled: float = 0.0
    status: str = PENDING
    live_at: float = 0.0
    cancel_at: float | None = None

    @property
    def remaining(self) -> float:
        """Returns the quantity that is not filled yet."""
        return max(0.0, self.qty - self.filled)


@dataclasses.dataclass(frozen=True)
class OrderEvent:
    """One change of an order.

    Attributes:
        kind: "live", "fill", "cancelled" or "rejected".
        order_id: The broker order id.
        ts: The time of the change.
        price: The fill price (fills only).
        qty: The fill quantity (fills only).
        fee: The fee in the quote currency (fills only).
        maker: True when the fill was a maker fill.
        reason: A short reason (rejects only).
    """

    kind: str
    order_id: str
    ts: float
    price: float = 0.0
    qty: float = 0.0
    fee: float = 0.0
    maker: bool = False
    reason: str = ""


class Broker(Protocol):
    """What the executor needs from an exchange, live or simulated."""

    async def place_limit(self, market: str, side: str, price: float,
                          qty: float) -> str:
        """Sends a limit order and returns its id."""

    async def cancel(self, order_id: str) -> None:
        """Asks the exchange to cancel an order."""

    async def open_orders(self) -> list[Order]:
        """Returns the orders that are pending or open."""

    async def balances(self) -> dict[str, float]:
        """Returns the free balance per currency."""

    async def balances_detail(self) -> dict[str, dict[str, float]]:
        """Returns {currency: {"total", "locked"}} for the panel."""

    async def poll(self) -> list[OrderEvent]:
        """Returns the order changes since the last call."""


def _taker_fill(levels: shadow.Levels, qty: float,
                crosses: Callable[[float], bool]) -> tuple[float, float]:
    """Returns (quantity, average price) of a taker order against `levels`.

    The order takes the levels for which `crosses(price)` is True, best
    level first, until it has `qty`.
    """
    filled = cost = 0.0
    for price, size in levels:
        if filled >= qty or not crosses(price):
            break
        take = min(size, qty - filled)
        filled += take
        cost += take * price
    return filled, (cost / filled if filled else 0.0)


# One number per paper broker in this process.
_RUNS = itertools.count(1)


class PaperBroker:
    """Simulated broker on live or recorded Wallex books and trades."""

    def __init__(self, cfg: dict, *, name: str = "paper",
                 balances: dict[str, float] | None = None) -> None:
        """Creates an empty broker.

        Args:
            cfg: The config dict from `sweepbot.config.load_config`.
            name: The prefix of the order ids, such as "paper" or "twin".
            balances: The start balance per currency. The paper broker does
                not block an order for missing funds. The risk guard limits
                the funds in use.
        """
        self._latency = cfg["paper"]["latency_s"]
        self._fees = {quote: config.fee_rates(quote, cfg)
                      for quote in (shadow.TMN, shadow.USDT)}
        # The run part keeps the ids unique in the store across restarts.
        self._name = f"{name}-{int(time.time())}{next(_RUNS)}"
        self._ids = itertools.count(1)
        self._orders: dict[str, Order] = {}
        self._books: dict[str, shadow.Book] = {}
        self._events: list[OrderEvent] = []
        self._now = 0.0
        self.balance = dict(balances or {})

    # Market data -----------------------------------------------------------

    def on_book(self, market: str, book: shadow.Book | None, now: float) -> None:
        """Takes a new book. Pending orders go live, and crossed orders fill."""
        if book is None:
            return
        self._books[market] = book
        self.advance(now)
        for order in self._live_orders(market):
            self._fill_on_book(order, book, now)

    def on_trade(self, market: str, price: float, qty: float,
                 buyer_maker: bool, now: float) -> None:
        """Takes one Wallex trade and fills the orders that it trades through.

        Args:
            market: The market of the trade.
            price: The trade price.
            qty: The trade quantity. A trade through our price proves that
                our order filled first and in full, so the size does not
                limit the fill.
            buyer_maker: True when a seller hit a resting bid.
            now: The receive time.
        """
        self.advance(now)
        for order in self._live_orders(market):
            if order.side == BUY and buyer_maker and price < order.price:
                self._fill(order, order.price, order.remaining, True, now)
            elif order.side == SELL and not buyer_maker and price > order.price:
                self._fill(order, order.price, order.remaining, True, now)

    def advance(self, now: float) -> None:
        """Applies the order changes that are due at `now`."""
        self._now = max(self._now, now)
        for order in list(self._orders.values()):
            if order.status == PENDING and now >= order.live_at:
                self._go_live(order, now)
            if (order.cancel_at is not None and now >= order.cancel_at
                    and order.status in (PENDING, OPEN)):
                order.status = CANCELLED
                self._events.append(OrderEvent("cancelled", order.id, now))
            if order.status in (FILLED, CANCELLED, REJECTED):
                del self._orders[order.id]

    # Broker protocol -------------------------------------------------------

    async def place_limit(self, market: str, side: str, price: float,
                          qty: float) -> str:
        """Sends a limit order. It goes live after the latency."""
        order_id = f"{self._name}-{next(self._ids)}"
        if side not in (BUY, SELL) or price <= 0 or qty <= 0:
            self._events.append(OrderEvent("rejected", order_id, self._now,
                                           reason="bad order"))
            return order_id
        self._orders[order_id] = Order(
            id=order_id, market=market, side=side, price=price, qty=qty,
            ts=self._now, live_at=self._now + self._latency)
        return order_id

    async def cancel(self, order_id: str) -> None:
        """Cancels an order after the latency. Unknown ids are ignored."""
        order = self._orders.get(order_id)
        if order is not None and order.cancel_at is None:
            order.cancel_at = self._now + self._latency

    async def open_orders(self) -> list[Order]:
        """Returns copies of the pending and open orders."""
        return [dataclasses.replace(order) for order in self._orders.values()]

    async def balances(self) -> dict[str, float]:
        """Returns the simulated balances."""
        return dict(self.balance)

    async def balances_detail(self) -> dict[str, dict[str, float]]:
        """Returns the simulated balances. The paper broker locks no funds."""
        return {asset: {"total": value, "locked": 0.0}
                for asset, value in self.balance.items()}

    async def poll(self) -> list[OrderEvent]:
        """Returns the order changes since the last call."""
        events, self._events = self._events, []
        return events

    # Internals -------------------------------------------------------------

    def _live_orders(self, market: str) -> list[Order]:
        """Returns the open orders of `market` in time order."""
        return [order for order in self._orders.values()
                if order.market == market and order.status == OPEN]

    def _go_live(self, order: Order, now: float) -> None:
        """Makes an order live. A crossing order first trades as a taker."""
        order.status = OPEN
        self._events.append(OrderEvent("live", order.id, now))
        book = self._books.get(order.market)
        if book is None:
            return
        if order.side == BUY:
            qty, price = _taker_fill(book.asks, order.remaining,
                                     lambda level: level <= order.price)
        else:
            qty, price = _taker_fill(book.bids, order.remaining,
                                     lambda level: level >= order.price)
        if qty > 0:
            self._fill(order, price, qty, False, now)

    def _fill_on_book(self, order: Order, book: shadow.Book, now: float) -> None:
        """Fills a resting order when the other side of the book reaches it."""
        if order.side == BUY:
            qty, _ = _taker_fill(book.asks, order.remaining,
                                 lambda level: level <= order.price)
        else:
            qty, _ = _taker_fill(book.bids, order.remaining,
                                 lambda level: level >= order.price)
        if qty > 0:
            self._fill(order, order.price, qty, True, now)

    def _fill(self, order: Order, price: float, qty: float, maker: bool,
              now: float) -> None:
        """Books one fill, its fee and the balances."""
        quote = shadow.quote_of(order.market)
        base = order.market[:-len(quote)]
        maker_fee, taker_fee = self._fees[quote]
        fee = (maker_fee if maker else taker_fee) * price * qty
        order.filled += qty
        if order.remaining <= _DUST * order.qty:
            order.status = FILLED
        sign = 1 if order.side == BUY else -1
        self.balance[base] = self.balance.get(base, 0.0) + sign * qty
        self.balance[quote] = (self.balance.get(quote, 0.0)
                               - sign * price * qty - fee)
        self._events.append(OrderEvent("fill", order.id, now, price=price,
                                       qty=qty, fee=fee, maker=maker))
