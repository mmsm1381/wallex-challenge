"""Live market state of the sweep bot: books, global quotes and fair value.

`Market` wraps `lagcatcher.engine.Engine` without its paper strategies. The
engine keeps the Wallex books, the global quotes and the rolling premium,
and computes the fair value with the rule of DESIGN section 2. `Market` adds
what the sweep bot needs on top:

* the USDT rate in toman, with a fallback to the implied rate when the
  USDTTMN book is frozen (the market closes every night from 20:00 to
  08:00 UTC),
* the spread of a book, and the Wallex feed state,
* listeners, so that the paper brokers see each book and trade.
"""

import copy
import statistics
from collections.abc import Callable

from lagcatcher import engine
from lagcatcher import shadow

TMN = shadow.TMN
USDT = shadow.USDT


def base_of(market: str) -> str:
    """Returns the base coin of a market, such as "BTC" for "BTCTMN"."""
    return engine.base_of(market)


def quote_of(market: str) -> str:
    """Returns the quote currency of a market: "TMN" or "USDT"."""
    return shadow.quote_of(market)


class Market:
    """Books, quotes and fair values of all markets of the config."""

    def __init__(self, cfg: dict, record_raw: bool = False) -> None:
        """Builds the engine state.

        Args:
            cfg: The config dict from `sweepbot.config.load_config`.
            record_raw: True writes the lagcatcher raw files to
                `bot.out_dir`, so a replay can run on the same hours later.
        """
        engine_cfg = copy.deepcopy(cfg)
        engine_cfg["run"]["out_dir"] = cfg["bot"]["out_dir"]
        self.cfg = cfg
        self.engine = engine.Engine(engine_cfg, strategies=[])
        self.engine.recorder.raw_enabled = record_raw
        self.trade_markets = self.engine.trade_markets
        self._filters = cfg["filters"]
        self._book_listeners: list[Callable] = []
        self._trade_listeners: list[Callable] = []

    def add_listener(self, *, on_book: Callable | None = None,
                     on_trade: Callable | None = None) -> None:
        """Adds callbacks for each book side and each trade.

        Args:
            on_book: Called as on_book(market, book, now) after a book update.
            on_trade: Called as on_trade(market, price, qty, buyer_maker, now).
        """
        if on_book is not None:
            self._book_listeners.append(on_book)
        if on_trade is not None:
            self._trade_listeners.append(on_trade)

    def on_book(self, market: str, side: str, levels: list, now: float,
                seed_since: float | None = None) -> None:
        """Stores a book side and tells the listeners."""
        self.engine.on_book(market, side, levels, now, seed_since)
        self.engine.stats["wallex"]["last"] = max(
            self.engine.stats["wallex"]["last"], now)
        book = self.engine.books.get(market)
        for listener in self._book_listeners:
            listener(market, book, now)

    def on_trades(self, market: str, trades: list, now: float) -> None:
        """Stores Wallex trades and tells the listeners about each trade."""
        self.engine.on_trades(market, trades, now)
        self.engine.stats["wallex"]["last"] = max(
            self.engine.stats["wallex"]["last"], now)
        for price, qty, buyer_maker, _ in trades:
            for listener in self._trade_listeners:
                listener(market, price, qty, buyer_maker, now)

    def on_quote(self, venue: str, coin: str, bid: float, ask: float,
                 now: float) -> None:
        """Stores a global best quote."""
        self.engine.on_quote(venue, coin, bid, ask, now)

    def sample_premiums(self, now: float) -> None:
        """Adds one premium sample per market (every `premium.sample_s`)."""
        self.engine.sample_premiums(now)

    def book(self, market: str) -> shadow.Book | None:
        """Returns the Wallex book of `market`, or None."""
        return self.engine.books.get(market)

    def wallex_live(self, now: float) -> bool:
        """Returns True when the Wallex feed sent a message recently."""
        return now - self.engine.stats["wallex"]["last"] <= self._filters["wallex_stale_s"]

    def global_mid(self, coin: str, now: float) -> float | None:
        """Returns the median mid of the fresh global venues, or None."""
        return self.engine.quotes.global_mid(coin=coin, now=now)

    def fair_value(self, market: str, now: float) -> float | None:
        """Returns the fair value with the premium, or None before warm-up."""
        return self.engine.fair_value(market, now)

    def mid(self, market: str) -> float | None:
        """Returns the Wallex mid of `market`, or None without a full book."""
        book = self.book(market)
        if book is None or book.best_bid() is None or book.best_ask() is None:
            return None
        return (book.best_bid() + book.best_ask()) / 2

    def spread_pct(self, market: str) -> float | None:
        """Returns the Wallex spread of `market` in percent, or None."""
        book = self.book(market)
        if book is None or book.best_bid() is None or book.best_ask() is None:
            return None
        return 100 * (book.best_ask() / book.best_bid() - 1)

    def usdttmn_frozen(self, now: float) -> bool:
        """Returns True when the USDTTMN book did not change for a long time.

        The limit is `filters.usdttmn_frozen_s`. A book that never came is
        frozen too.
        """
        last = max(self.engine.side_ts.get(("USDTTMN", "bid"), -1e18),
                   self.engine.side_ts.get(("USDTTMN", "ask"), -1e18))
        return now - last > self._filters["usdttmn_frozen_s"]

    def implied_usdt_rate(self) -> float | None:
        """Returns the median of coin TMN mid / coin USDT mid, or None.

        Only coins with both books and a spread up to `filters.max_spread_pct`
        on both count. The TMN markets keep trading while USDTTMN is closed,
        so this rate follows the toman also at night.
        """
        max_spread = self._filters["max_spread_pct"]
        ratios = []
        for market in self.trade_markets:
            if quote_of(market) != TMN:
                continue
            usdt_market = base_of(market) + USDT
            if usdt_market not in self.trade_markets:
                continue
            spreads = (self.spread_pct(market), self.spread_pct(usdt_market))
            if any(spread is None or spread > max_spread for spread in spreads):
                continue
            ratios.append(self.mid(market) / self.mid(usdt_market))
        return statistics.median(ratios) if ratios else None

    def usdt_rate(self, now: float) -> tuple[float | None, str]:
        """Returns (toman per USDT, source).

        The source is "usdttmn" for the USDTTMN mid, or "implied" when the
        USDTTMN book is frozen or missing.
        """
        mid = self.engine.usdttmn_mid()
        if mid is not None and not self.usdttmn_frozen(now):
            return mid, "usdttmn"
        implied = self.implied_usdt_rate()
        if implied is not None:
            return implied, "implied"
        return mid, "usdttmn"

    def to_tmn(self, market: str, amount_quote: float, now: float) -> float | None:
        """Converts an amount in the quote currency of `market` to toman."""
        if quote_of(market) == TMN:
            return amount_quote
        rate, _ = self.usdt_rate(now)
        return amount_quote * rate if rate else None

    def close(self) -> None:
        """Flushes and closes the raw files."""
        self.engine.recorder.flush()
        self.engine.recorder.close()
