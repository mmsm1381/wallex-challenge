"""Paper strategies that test the Wallex lag edge against real order books.

The shadow test does not send orders. Each strategy decides what a real bot
would do, and then uses the next real Wallex book and trades to decide if
that order would fill. Two entry styles are tested:

* `TakerStrategy` buys when the best ask is below fair value by more than
  the fees and a buffer. The fill happens only if the cheap ask is still on
  the book after the strategy latency. This measures the speed race.
* `RestingBidStrategy` keeps a bid below fair value. The bid fills only when
  a seller trades through it. This does not need to win a speed race.

Both strategies use the same exit: a resting sell at fair value, a stop
below the entry, and a time limit. After a stop, a strategy does not buy the
same market again until a cooldown ends. Neither strategy buys when the
Wallex spread is wider than `max_entry_spread_pct`. On such a book, a stop
or a taker exit sells far below the price that the strategy paid. Fees come from the challenge account. The
engine in `lagcatcher.engine` calls `on_tick`, `on_trade`, and `on_timer`,
and writes the returned events to disk.

Fill rules are conservative on purpose. A trade at exactly our price is not
a fill, because our place in the queue is unknown.
"""

import dataclasses
import math

TMN = "TMN"
USDT = "USDT"

Levels = list[tuple[float, float]]


def quote_of(market: str) -> str:
    """Returns the quote currency of a Wallex market such as "BTCUSDT"."""
    return USDT if market.endswith(USDT) else TMN


@dataclasses.dataclass
class Book:
    """One Wallex order book.

    Attributes:
        bids: (price, quantity) levels, best (highest) price first.
        asks: (price, quantity) levels, best (lowest) price first.
        ts: Local receive time of the last update, in seconds.
    """

    bids: Levels
    asks: Levels
    ts: float

    def best_bid(self) -> float | None:
        """Returns the highest bid price, or None for an empty side."""
        return self.bids[0][0] if self.bids else None

    def best_ask(self) -> float | None:
        """Returns the lowest ask price, or None for an empty side."""
        return self.asks[0][0] if self.asks else None


@dataclasses.dataclass(frozen=True)
class ExitRules:
    """Exit settings that both strategies share.

    Attributes:
        max_hold_s: The time limit of a position.
        stop_pct: The stop distance below the entry price, in percent.
        requote_bps: The fair-value move that moves the exit target.
        latency_s: The time a new or moved exit order needs to go live.
        stop_confirm_s: The time the best bid must stay at or below the
            stop before the stop sells. 0 sells on the first low bid.
        cooldown_s: The time after a stop in which the strategy does not
            buy the same market again.
    """

    max_hold_s: float
    stop_pct: float
    requote_bps: float
    latency_s: float
    stop_confirm_s: float = 0.0
    cooldown_s: float = 0.0


class TokenBucket:
    """Order budget that refills at a fixed rate.

    Wallex accepts 20 order requests per 10 seconds. A strategy that wants
    more actions than its budget must skip them, as a real bot must.
    """

    def __init__(self, capacity: int, per_s: float) -> None:
        """Creates a full bucket of `capacity` actions per `per_s` seconds."""
        self._capacity = float(capacity)
        self._rate = capacity / per_s
        self._tokens = float(capacity)
        self._last: float | None = None

    def take(self, now: float) -> bool:
        """Uses one action if the budget allows it."""
        if self._last is not None:
            refill = (now - self._last) * self._rate
            self._tokens = min(self._capacity, self._tokens + refill)
        self._last = now
        if self._tokens < 1:
            return False
        self._tokens -= 1
        return True


def buy_vwap(asks: Levels, qty: float, limit: float) -> tuple[float, float]:
    """Returns (filled quantity, average price) of a buy limit order.

    The order takes the asks at or below `limit`, best price first, until it
    has `qty`. The result shows how much of the order the book can fill.
    """
    filled = cost = 0.0
    for price, size in asks:
        if price > limit or filled >= qty:
            break
        take = min(size, qty - filled)
        filled += take
        cost += take * price
    return filled, (cost / filled if filled else 0.0)


def sell_vwap(bids: Levels, qty: float) -> float:
    """Returns the average price of a market sell of `qty` into `bids`.

    When the visible bids are smaller than `qty`, the rest sells at the
    lowest visible bid. Our size is small, so this case is rare.
    """
    filled = cost = 0.0
    for price, size in bids:
        if filled >= qty:
            break
        take = min(size, qty - filled)
        filled += take
        cost += take * price
    if filled < qty and bids:
        cost += (qty - filled) * bids[-1][0]
        filled = qty
    return cost / filled if filled else 0.0


@dataclasses.dataclass
class _Position:
    """One open paper position and its exit order."""

    market: str
    quote: str
    entry_ts: float
    entry_price: float
    qty: float
    entry_fee: float
    entry_maker: bool
    live_target: float | None = None
    pending_target: float | None = None
    pending_due: float = 0.0
    stop_since: float | None = None


class _PaperStrategy:
    """Shared position, exit, fee, and PnL logic of the paper strategies."""

    def __init__(
        self,
        *,
        name: str,
        notional_tmn: float,
        fees: dict[str, tuple[float, float]],
        exit_rules: ExitRules,
        bucket: TokenBucket,
        min_fill_fraction: float,
        max_entry_spread_pct: float = math.inf,
    ) -> None:
        """Stores the settings.

        Args:
            name: The strategy name in the events.
            notional_tmn: The size of one trade in toman.
            fees: (maker, taker) fee fractions per quote currency.
            exit_rules: The exit settings.
            bucket: The order budget of this strategy.
            min_fill_fraction: The smallest part of the size that counts
                as a fill.
            max_entry_spread_pct: The widest Wallex spread, in percent, at
                which the strategy buys.
        """
        self.name = name
        self.positions: dict[str, _Position] = {}
        self.rate_limited = 0
        self._notional_tmn = notional_tmn
        self._fees = fees
        self._exit_rules = exit_rules
        self._bucket = bucket
        self._min_fill_fraction = min_fill_fraction
        self._cooldown_until: dict[str, float] = {}
        self._max_entry_spread = max_entry_spread_pct / 100

    def _no_entry(self, market: str, now: float, book: Book | None) -> bool:
        """Returns True when the strategy must not buy `market` now.

        The reasons are a running cooldown and a spread above the limit.
        A book without a bid also blocks entries, as no exit is possible.
        """
        if now < self._cooldown_until.get(market, 0.0):
            return True
        bid = book.best_bid() if book else None
        ask = book.best_ask() if book else None
        return bid is None or ask is None or ask / bid - 1 > self._max_entry_spread

    def _notional_quote(self, quote: str, usdttmn: float | None) -> float | None:
        """Returns the trade size in the quote currency, or None."""
        if quote == TMN:
            return self._notional_tmn
        return self._notional_tmn / usdttmn if usdttmn else None

    def _open(self, *, market: str, now: float, price: float, qty: float,
              maker: bool, extra: dict) -> dict:
        """Opens a position and returns its fill event."""
        quote = quote_of(market)
        maker_fee, taker_fee = self._fees[quote]
        fee = (maker_fee if maker else taker_fee) * qty * price
        self.positions[market] = _Position(
            market=market, quote=quote, entry_ts=now, entry_price=price,
            qty=qty, entry_fee=fee, entry_maker=maker)
        return {"type": "fill", "strategy": self.name, "market": market,
                "ts": now, "price": price, "qty": qty, "maker": maker, **extra}

    def _close(self, *, market: str, now: float, price: float, maker: bool,
               reason: str, usdttmn: float | None) -> dict:
        """Closes a position and returns its exit event with the net PnL.

        A USDT market reports `net_tmn` at the USDTTMN mid of the exit.
        `net_tmn` is None when that rate is not known.
        """
        position = self.positions.pop(market)
        if reason == "stop":
            self._cooldown_until[market] = now + self._exit_rules.cooldown_s
        maker_fee, taker_fee = self._fees[position.quote]
        exit_fee = (maker_fee if maker else taker_fee) * position.qty * price
        net_quote = (position.qty * (price - position.entry_price)
                     - position.entry_fee - exit_fee)
        entry_value = position.qty * position.entry_price
        rate = 1.0 if position.quote == TMN else usdttmn
        return {
            "type": "exit", "strategy": self.name, "market": market, "ts": now,
            "reason": reason, "maker": maker, "price": price,
            "entry_price": position.entry_price, "entry_ts": position.entry_ts,
            "entry_maker": position.entry_maker, "qty": position.qty,
            "hold_s": now - position.entry_ts, "net_quote": net_quote,
            "net_pct": 100 * net_quote / entry_value,
            "net_tmn": net_quote * rate if rate else None,
        }

    def _manage_exit(self, market: str, now: float, book: Book | None,
                     fv: float | None, usdttmn: float | None) -> list[dict]:
        """Runs the exit rules of one position in their order of priority.

        The order is: a new target goes live, the book crosses the target,
        the stop, the time limit, and last a target move after fair value.
        """
        position = self.positions[market]
        rules = self._exit_rules
        bids = book.bids if book else []
        best_bid = bids[0][0] if bids else None
        close = {"market": market, "now": now, "usdttmn": usdttmn}
        if position.pending_target is not None and now >= position.pending_due:
            target = position.pending_target
            position.pending_target = None
            # A sell limit at or below the best bid trades at once as a taker.
            if best_bid is not None and best_bid >= target:
                return [self._close(price=sell_vwap(bids, position.qty),
                                    maker=False, reason="target_marketable",
                                    **close)]
            position.live_target = target
        if (position.live_target is not None and best_bid is not None
                and best_bid >= position.live_target):
            return [self._close(price=position.live_target, maker=True,
                                reason="target", **close)]
        stop_price = position.entry_price * (1 - rules.stop_pct / 100)
        if self._stop_hit(position, now, best_bid, fv, stop_price):
            return [self._close(price=sell_vwap(bids, position.qty),
                                maker=False, reason="stop", **close)]
        if best_bid is not None and now - position.entry_ts >= rules.max_hold_s:
            return [self._close(price=sell_vwap(bids, position.qty),
                                maker=False, reason="time", **close)]
        if fv is not None:
            self._move_target(position, now, fv)
        return []

    def _stop_hit(self, position: _Position, now: float, best_bid: float | None,
                  fv: float | None, stop_price: float) -> bool:
        """Returns True when the stop must sell the position.

        Fair value at or below the stop price sells at once. A best bid at
        or below the stop price sells only when it stays there for
        `stop_confirm_s`. Wallex bots often remove their bids for 0.1 to 2 s,
        and such a low bid is not a real price move.
        """
        if best_bid is None:
            return False
        if fv is not None and fv <= stop_price:
            return True
        if best_bid > stop_price:
            position.stop_since = None
            return False
        if position.stop_since is None:
            position.stop_since = now
        return now - position.stop_since >= self._exit_rules.stop_confirm_s

    def _move_target(self, position: _Position, now: float, fv: float) -> None:
        """Sends a new exit target when fair value moved enough."""
        current = position.pending_target
        if current is None:
            current = position.live_target
        moved_bps = math.inf if current is None else abs(fv / current - 1) * 1e4
        if moved_bps <= self._exit_rules.requote_bps:
            return
        if not self._bucket.take(now):
            self.rate_limited += 1
            return
        position.pending_target = fv
        position.pending_due = now + self._exit_rules.latency_s

    def _exit_on_trade(self, market: str, now: float, price: float,
                       buyer_maker: bool, usdttmn: float | None) -> list[dict]:
        """Fills the exit target when a buyer trades above it."""
        position = self.positions[market]
        target = position.live_target
        if target is None or buyer_maker or price <= target:
            return []
        return [self._close(market=market, now=now, price=target, maker=True,
                            reason="target", usdttmn=usdttmn)]


class TakerStrategy(_PaperStrategy):
    """Buys the ask when it is below fair value by more than the hurdle.

    The hurdle is max(min_gap_pct, taker fee + maker fee + buffer_pct). The
    order is a limit at the signal ask. After `latency_s` the strategy checks
    the real book. The order fills at the average price of the asks at or
    below the limit if they hold at least `min_fill_fraction` of the size.
    Otherwise the strategy records a miss. One instance runs per latency, so
    the report shows how the edge changes with speed.
    """

    def __init__(self, *, name: str, latency_s: float, notional_tmn: float,
                 min_gap_pct: float, buffer_pct: float,
                 fees: dict[str, tuple[float, float]], exit_rules: ExitRules,
                 bucket: TokenBucket, min_fill_fraction: float,
                 max_entry_spread_pct: float = math.inf) -> None:
        """Stores the settings. Percent values use percent units."""
        super().__init__(name=name, notional_tmn=notional_tmn, fees=fees,
                         exit_rules=exit_rules, bucket=bucket,
                         min_fill_fraction=min_fill_fraction,
                         max_entry_spread_pct=max_entry_spread_pct)
        self._latency = latency_s
        self._min_gap = min_gap_pct / 100
        self._buffer = buffer_pct / 100
        self._pending: dict[str, dict] = {}

    def on_tick(self, *, market: str, now: float, book: Book | None,
                fv: float | None, usdttmn: float | None) -> list[dict]:
        """Reacts to a new book or fair value of one market."""
        if market in self.positions:
            return self._manage_exit(market, now, book, fv, usdttmn)
        if market in self._pending:
            if now >= self._pending[market]["due"]:
                return self._try_fill(market, now, book)
            return []
        best_ask = book.best_ask() if book else None
        if fv is None or best_ask is None or self._no_entry(market, now, book):
            return []
        maker_fee, taker_fee = self._fees[quote_of(market)]
        hurdle = max(self._min_gap, taker_fee + maker_fee + self._buffer)
        gap = 1 - best_ask / fv
        if gap < hurdle:
            return []
        notional = self._notional_quote(quote_of(market), usdttmn)
        if notional is None:
            return []
        if not self._bucket.take(now):
            self.rate_limited += 1
            return []
        self._pending[market] = {"limit": best_ask, "due": now + self._latency,
                                 "qty": notional / best_ask, "signal_ts": now,
                                 "gap": gap}
        return [{"type": "signal", "strategy": self.name, "market": market,
                 "ts": now, "ask": best_ask, "fv": fv, "gap_pct": 100 * gap,
                 "hurdle_pct": 100 * hurdle}]

    def on_trade(self, *, market: str, now: float, price: float, qty: float,
                 buyer_maker: bool, usdttmn: float | None) -> list[dict]:
        """Checks the exit target of an open position against a trade."""
        if market not in self.positions:
            return []
        return self._exit_on_trade(market, now, price, buyer_maker, usdttmn)

    def on_timer(self, *, now: float, books: dict[str, Book],
                 fvs: dict[str, float | None],
                 usdttmn: float | None) -> list[dict]:
        """Runs the exits, then fills the orders whose latency passed.

        A position that opens in this call gets its exit order in the next
        call, as a real bot sends that order after it sees the fill.
        """
        events = []
        for market in list(self.positions):
            events += self._manage_exit(market, now, books.get(market),
                                        fvs.get(market), usdttmn)
        due = [m for m, order in self._pending.items() if now >= order["due"]]
        for market in due:
            events += self._try_fill(market, now, books.get(market))
        return events

    def _try_fill(self, market: str, now: float, book: Book | None) -> list[dict]:
        """Fills a pending order against the current book, or records a miss."""
        order = self._pending.pop(market)
        asks = book.asks if book else []
        filled, vwap = buy_vwap(asks, order["qty"], order["limit"])
        details = {"signal_ts": order["signal_ts"], "limit": order["limit"],
                   "gap_pct": 100 * order["gap"]}
        if filled >= self._min_fill_fraction * order["qty"]:
            return [self._open(market=market, now=now, price=vwap, qty=filled,
                               maker=False, extra=details)]
        best_ask = book.best_ask() if book else None
        moved = best_ask is None or best_ask > order["limit"]
        return [{"type": "miss", "strategy": self.name, "market": market,
                 "ts": now, "reason": "ask_moved" if moved else "thin_depth",
                 "best_ask": best_ask, **details}]


class RestingBidStrategy(_PaperStrategy):
    """Keeps a bid below fair value and waits for a seller to hit it.

    The bid price is fv * (1 - 2 * maker fee - buffer_pct / 100), so a fill
    and a maker exit at fair value pay both fees plus the buffer. A moved
    bid goes live only after `latency_s`. Until then the old bid can fill,
    as on a real exchange. The bid fills when a sell-initiated trade prints
    strictly below it, or when the best ask falls to it.
    """

    def __init__(self, *, name: str, latency_s: float, notional_tmn: float,
                 buffer_pct: float, requote_bps: float,
                 fees: dict[str, tuple[float, float]], exit_rules: ExitRules,
                 bucket: TokenBucket, min_fill_fraction: float,
                 max_entry_spread_pct: float = math.inf) -> None:
        """Stores the settings. Percent values use percent units."""
        super().__init__(name=name, notional_tmn=notional_tmn, fees=fees,
                         exit_rules=exit_rules, bucket=bucket,
                         min_fill_fraction=min_fill_fraction,
                         max_entry_spread_pct=max_entry_spread_pct)
        self._latency = latency_s
        self._buffer = buffer_pct / 100
        self._requote_bps = requote_bps
        self._bids: dict[str, dict] = {}

    def on_tick(self, *, market: str, now: float, book: Book | None,
                fv: float | None, usdttmn: float | None) -> list[dict]:
        """Moves, activates, or fills the bid of one market."""
        if market in self.positions:
            return self._manage_exit(market, now, book, fv, usdttmn)
        best_ask = book.best_ask() if book else None
        bid = self._bids.get(market)
        if bid is not None and bid["pending"] is not None and now >= bid["due"]:
            fill = self._activate(market, now, bid, book)
            if fill:
                return fill
        if (bid is not None and bid["live"] is not None
                and best_ask is not None and best_ask <= bid["live"]):
            del self._bids[market]
            return [self._open(market=market, now=now, price=bid["live"],
                               qty=bid["live_qty"], maker=True,
                               extra={"entry": "book_cross"})]
        if fv is None or self._no_entry(market, now, book):
            self._bids.pop(market, None)
            return []
        self._requote(market, now, fv, best_ask, usdttmn)
        return []

    def on_trade(self, *, market: str, now: float, price: float, qty: float,
                 buyer_maker: bool, usdttmn: float | None) -> list[dict]:
        """Fills the live bid when a seller trades through it."""
        if market in self.positions:
            return self._exit_on_trade(market, now, price, buyer_maker, usdttmn)
        bid = self._bids.get(market)
        if bid is None or bid["live"] is None:
            return []
        # buyer_maker=True means a seller took a resting bid.
        if not buyer_maker or price >= bid["live"]:
            return []
        if qty < self._min_fill_fraction * bid["live_qty"]:
            return []
        del self._bids[market]
        return [self._open(market=market, now=now, price=bid["live"],
                           qty=bid["live_qty"], maker=True,
                           extra={"entry": "sell_through"})]

    def on_timer(self, *, now: float, books: dict[str, Book],
                 fvs: dict[str, float | None],
                 usdttmn: float | None) -> list[dict]:
        """Runs the exits and the bids whose new price is due."""
        events = []
        for market in list(self.positions):
            events += self._manage_exit(market, now, books.get(market),
                                        fvs.get(market), usdttmn)
        due = [m for m, bid in self._bids.items()
               if bid["pending"] is not None and now >= bid["due"]]
        for market in due:
            events += self.on_tick(market=market, now=now,
                                   book=books.get(market), fv=fvs.get(market),
                                   usdttmn=usdttmn)
        return events

    def _activate(self, market: str, now: float, bid: dict,
                  book: Book | None) -> list[dict]:
        """Makes the pending bid live, or fills it if the ask is at or below it."""
        price = bid["pending"]
        qty = bid["pending_qty"]
        bid["pending"] = None
        if book is not None and book.best_ask() is not None and book.best_ask() <= price:
            filled, vwap = buy_vwap(book.asks, qty, price)
            if filled >= self._min_fill_fraction * qty:
                del self._bids[market]
                return [self._open(market=market, now=now, price=vwap,
                                   qty=filled, maker=False,
                                   extra={"entry": "marketable"})]
        bid["live"] = price
        bid["live_qty"] = qty
        return []

    def _requote(self, market: str, now: float, fv: float,
                 best_ask: float | None, usdttmn: float | None) -> None:
        """Sends a new bid price when fair value moved enough."""
        maker_fee, _ = self._fees[quote_of(market)]
        desired = fv * (1 - (2 * maker_fee + self._buffer))
        # A bid at or above the ask is a taker order. The taker strategy
        # tests that case, so this strategy does not send it.
        if best_ask is not None and desired >= best_ask:
            return
        bid = self._bids.get(market)
        current = None
        if bid is not None:
            current = bid["pending"] if bid["pending"] is not None else bid["live"]
        if current is not None and abs(desired / current - 1) * 1e4 <= self._requote_bps:
            return
        notional = self._notional_quote(quote_of(market), usdttmn)
        if notional is None:
            return
        if not self._bucket.take(now):
            self.rate_limited += 1
            return
        live = bid["live"] if bid is not None else None
        live_qty = bid["live_qty"] if bid is not None else 0.0
        self._bids[market] = {"live": live, "live_qty": live_qty,
                              "pending": desired, "pending_qty": notional / desired,
                              "due": now + self._latency}
