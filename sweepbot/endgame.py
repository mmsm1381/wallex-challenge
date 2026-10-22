"""End of the challenge: sell all USDT for toman (DESIGN section 7).

The final equity counts in toman. USDTTMN closes every night at 20:00 UTC,
so the USDT must be sold before the last close. `RiskGuard.usdt_convert_at`
gives the time: 30 minutes before that close. The coins are sold before it
(`RiskGuard.flatten_at`), so the coins of USDT markets are USDT by then.

`UsdtConverter.step` runs on each executor tick, but works at most every
`check_s`:

1. It reads the free USDT balance of the broker.
2. It sends a USDTTMN sell limit below the best bid by the cut slippage.
   The limit sells into the visible bids at once and never below the limit.
3. An order that is still open after `retry_s` is cancelled. The next round
   reads the balance again and sends a new order at the new best bid.
4. It stops when the rest is below the minimum order value of USDTTMN.
"""

import logging

from sweepbot import broker
from sweepbot import exits
from sweepbot import rules as rules_mod

log = logging.getLogger(__name__)

MARKET = "USDTTMN"


class UsdtConverter:
    """Sells the USDT balance on USDTTMN from the convert time on."""

    def __init__(self, *, convert_at: float | None, market, broker_: broker.Broker,
                 rule: rules_mod.MarketRule, slippage_pct: float, store, mode: str,
                 check_s: float = 5.0, retry_s: float = 30.0) -> None:
        """Stores the parts.

        Args:
            convert_at: The time to start, or None to never start.
            market: The market state. It gives the USDTTMN book.
            broker_: The broker of the bot.
            rule: The order rules of USDTTMN.
            slippage_pct: How far below the best bid the limit may sell.
            store: The SQLite store, for the log rows.
            mode: The mode label of the log rows.
            check_s: The smallest time between two rounds.
            retry_s: The time after which an open order is replaced.
        """
        self.convert_at = convert_at
        self._market = market
        self._broker = broker_
        self._rule = rule
        self._slippage_pct = slippage_pct
        self._store = store
        self._mode = mode
        self._check_s = check_s
        self._retry_s = retry_s
        self._next_check = 0.0
        self.order_id: str | None = None
        self._placed_at = 0.0
        self.done = False

    def _log(self, now: float, text: str, **fields) -> None:
        """Writes one log row and one log line."""
        log.warning("[%s] %s", self._mode, text)
        self._store.add_log(ts=now, kind="risk", text=f"[{self._mode}] {text}",
                            fields={"mode": self._mode, "job": "usdt_convert", **fields})

    async def step(self, now: float) -> None:
        """Runs one round of the conversion when it is due."""
        if self.done or self.convert_at is None or now < self.convert_at:
            return
        if now < self._next_check:
            return
        self._next_check = now + self._check_s
        if self.order_id is not None:
            if now - self._placed_at < self._retry_s:
                return
            await self._broker.cancel(self.order_id)
            self._log(now, f"USDT sale {self.order_id} still open after "
                           f"{self._retry_s:.0f} s: cancelled, next round sends a new one")
            self.order_id = None
            return
        usdt = (await self._broker.balances()).get("USDT", 0.0)
        book = self._market.book(MARKET)
        best_bid = book.best_bid() if book is not None else None
        if best_bid is None:
            self._log(now, "USDT sale waits: no USDTTMN bid")
            return
        price = exits.cut_price(best_bid, self._slippage_pct, self._rule.price_tick)
        qty = self._rule.round_qty(max(usdt, 0.0))
        if qty < self._rule.min_qty or qty * price < self._rule.min_notional:
            self.done = True
            self._log(now, f"USDT sale done: {usdt:.8g} USDT left, below the minimum order",
                      usdt_left=usdt)
            return
        self.order_id = await self._broker.place_limit(MARKET, broker.SELL, price, qty)
        self._placed_at = now
        self._log(now, f"USDT sale: sell {qty:.8g} USDT at {price:.10g} or better"
                       f" (best bid {best_bid:.10g})", order_id=self.order_id,
                  qty=qty, price=price)
