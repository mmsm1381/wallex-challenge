"""Start of the bot: buy the USDT that the USDT markets need.

The challenge account starts with toman only. Bids on USDT markets need a
USDT balance, so the bot first buys USDT for `funding.usdt_target_share` of
the start equity on USDTTMN. `endgame.UsdtConverter` sells all USDT again
before the last USDTTMN close.

`UsdtFunder.step` runs on each executor tick, but works at most every
`check_s`:

1. It reads the free USDT balance of the broker. The USDT that the account
   already holds counts, so a restart does not buy a second time. When the
   balance reaches the target, it cancels a rest of its order and is done.
2. It sends a USDTTMN buy limit above the best ask by the slippage. The
   limit buys from the visible asks at once and never above the limit.
3. An order that is still open after `retry_s` is cancelled. The next round
   reads the balance again.
4. It is done when the USDT value is within `tolerance` of the target, or
   when the stop time (the end-of-challenge flatten time) has come.

Until the funder is done, the executor sends no bids on USDT markets.
The USDT holding carries currency risk (RESEARCH.md section 2).
"""

import logging

from sweepbot import broker
from sweepbot import exits
from sweepbot import rules as rules_mod

log = logging.getLogger(__name__)

MARKET = "USDTTMN"


class UsdtFunder:
    """Buys USDT on USDTTMN up to a toman target, once, at the start."""

    def __init__(self, *, target_tmn: float, market, broker_: broker.Broker,
                 rule: rules_mod.MarketRule, slippage_pct: float, store, mode: str,
                 stop_at: float | None = None, tolerance: float = 0.05,
                 check_s: float = 5.0, retry_s: float = 30.0) -> None:
        """Stores the parts.

        Args:
            target_tmn: The toman value of USDT to hold.
            market: The market state. It gives the USDTTMN book.
            broker_: The broker of the bot.
            rule: The order rules of USDTTMN.
            slippage_pct: How far above the best ask the limit may buy.
            store: The SQLite store, for the log rows.
            mode: The mode label of the log rows.
            stop_at: The time from which the funder never buys, or None.
            tolerance: The part of the target that may be missing at the end.
            check_s: The smallest time between two rounds.
            retry_s: The time after which an open order is replaced.
        """
        self.target_tmn = target_tmn
        self._market = market
        self._broker = broker_
        self._rule = rule
        self._slippage_pct = slippage_pct
        self._store = store
        self._mode = mode
        self._stop_at = stop_at
        self._tolerance = tolerance
        self._check_s = check_s
        self._retry_s = retry_s
        self._next_check = 0.0
        self.order_id: str | None = None
        self._placed_at = 0.0
        self.done = target_tmn <= 0
        # For the panel: the first order time, the end time, the last USDT
        # balance and its toman value at the end.
        self.started_at: float | None = None
        self.done_at: float | None = None
        self.usdt: float | None = None
        self.usdt_tmn: float | None = None
        self.note = "waiting"

    def status(self) -> dict:
        """Returns the state of the purchase for the panel."""
        return {"target_tmn": self.target_tmn, "done": self.done,
                "started_at": self.started_at, "done_at": self.done_at,
                "usdt": self.usdt, "usdt_tmn": self.usdt_tmn,
                "order_id": self.order_id, "note": self.note}

    def _finish(self, now: float, note: str) -> None:
        """Marks the purchase done."""
        self.done = True
        self.done_at = now
        self.note = note

    def _log(self, now: float, text: str, **fields) -> None:
        """Writes one log row and one log line."""
        log.warning("[%s] %s", self._mode, text)
        self._store.add_log(ts=now, kind="risk", text=f"[{self._mode}] {text}",
                            fields={"mode": self._mode, "job": "usdt_funding", **fields})

    async def step(self, now: float) -> None:
        """Runs one round of the purchase when it is due."""
        if self.done or now < self._next_check:
            return
        self._next_check = now + self._check_s
        if self._stop_at is not None and now >= self._stop_at:
            self._finish(now, "stopped: end of the challenge is near")
            self._log(now, "USDT purchase stopped: end of the challenge is near")
            return
        book = self._market.book(MARKET)
        best_ask = book.best_ask() if book is not None else None
        if best_ask is None:
            return
        try:
            usdt = (await self._broker.balances()).get("USDT", 0.0)
        except RuntimeError as error:
            self._log(now, f"USDT purchase waits: balances failed ({error})")
            return
        self.usdt, self.usdt_tmn = usdt, usdt * best_ask
        missing_tmn = self.target_tmn - usdt * best_ask
        if missing_tmn <= self._tolerance * self.target_tmn:
            if self.order_id is not None:
                await self._broker.cancel(self.order_id)
                self.order_id = None
            self._finish(now, "bought")
            self._log(now, f"USDT purchase done: {usdt:.8g} USDT, about "
                           f"{usdt * best_ask:,.0f} toman", usdt=usdt)
            return
        if self.order_id is not None:
            if now - self._placed_at < self._retry_s:
                return
            await self._broker.cancel(self.order_id)
            self._log(now, f"USDT purchase {self.order_id} still open after "
                           f"{self._retry_s:.0f} s: cancelled, next round reads the balance")
            self.order_id = None
            return
        price = exits.round_price(best_ask * (1 + self._slippage_pct / 100),
                                  self._rule.price_tick, up=True)
        qty = self._rule.round_qty(missing_tmn / best_ask)
        if qty < self._rule.min_qty or qty * price < self._rule.min_notional:
            self._finish(now, "bought")
            self._log(now, f"USDT purchase done: the rest {missing_tmn:,.0f} toman is "
                           "below the minimum order", usdt=usdt)
            return
        self.order_id = await self._broker.place_limit(MARKET, broker.BUY, price, qty)
        self._placed_at = now
        if self.started_at is None:
            self.started_at = now
        self.note = "buying"
        self._log(now, f"USDT purchase: buy {qty:.8g} USDT at {price:.10g} or better"
                       f" (best ask {best_ask:.10g})", order_id=self.order_id,
                  qty=qty, price=price)
