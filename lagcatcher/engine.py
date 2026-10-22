"""The shadow engine: feeds in, fair values and paper trades out.

`Engine` owns the live state: the Wallex books, the global quotes, and the
premium per market. Each feed update goes to `on_book`, `on_trades`, or
`on_quote`. The engine then computes the fair value of the affected markets
and gives the update to the gap-episode tracker and to each paper strategy.
Three loops run next to the feeds:

* a 100 ms timer for latency-based fills, exit orders, and time stops,
* a premium sampler (every `premium.sample_s`),
* a status line (every `run.status_every_s`) that also flushes the files.

Fair value of a market = median global mid x (USDTTMN mid for a toman
market) x exp(rolling premium). The engine sends no real orders.
"""

import asyncio
import collections
import dataclasses
import logging
import math
import shutil
import signal
import time

from lagcatcher import config
from lagcatcher import episodes
from lagcatcher import fairvalue
from lagcatcher import feeds
from lagcatcher import recorder
from lagcatcher import shadow

log = logging.getLogger(__name__)


def base_of(market: str) -> str:
    """Returns the base coin of a Wallex market, such as "BTC" for "BTCTMN"."""
    quote = shadow.quote_of(market)
    return market[:-len(quote)]


# Raw recording comes back only this far above its limit, so that it does
# not switch on and off at the limit.
RESUME_MARGIN_MB = 100.0


def disk_action(*, free_mb: float, min_record_mb: float, min_run_mb: float,
                raw_off: bool = False) -> str:
    """Returns what the engine must do for the free disk space.

    The test can run on a host that other programs use. A full disk would
    stop those programs too, so the engine first stops its raw files and
    then stops itself. When space comes back, raw recording starts again.

    Returns:
        "ok", "stop_raw", "resume_raw", or "stop".
    """
    if free_mb < min_run_mb:
        return "stop"
    if free_mb < min_record_mb:
        return "stop_raw"
    if raw_off:
        return "resume_raw" if free_mb >= min_record_mb + RESUME_MARGIN_MB else "stop_raw"
    return "ok"


def build_strategies(cfg: dict) -> list:
    """Builds one taker strategy per latency and one resting-bid strategy."""
    fees = {"TMN": config.fee_rates("TMN", cfg),
            "USDT": config.fee_rates("USDT", cfg)}
    exit_cfg = cfg["exit"]
    paper = cfg["paper"]
    budget = cfg["limits"]["orders_per_10s"]
    rules = shadow.ExitRules(
        max_hold_s=exit_cfg["max_hold_s"], stop_pct=exit_cfg["stop_pct"],
        requote_bps=exit_cfg["requote_bps"], latency_s=cfg["resting"]["latency_s"],
        stop_confirm_s=exit_cfg["stop_confirm_s"], cooldown_s=exit_cfg["cooldown_s"])
    strategies: list = [
        shadow.TakerStrategy(
            name=f"taker_{latency:g}s", latency_s=latency,
            notional_tmn=paper["notional_tmn"],
            min_gap_pct=cfg["taker"]["min_gap_pct"],
            buffer_pct=cfg["taker"]["buffer_pct"], fees=fees,
            # A bot with this latency also places its exit with it.
            exit_rules=dataclasses.replace(rules, latency_s=latency),
            bucket=shadow.TokenBucket(capacity=budget, per_s=10.0),
            min_fill_fraction=paper["min_fill_fraction"],
            max_entry_spread_pct=paper["max_entry_spread_pct"])
        for latency in cfg["taker"]["latencies_s"]
    ]
    strategies.append(shadow.RestingBidStrategy(
        name="resting", latency_s=cfg["resting"]["latency_s"],
        notional_tmn=paper["notional_tmn"],
        buffer_pct=cfg["resting"]["buffer_pct"],
        requote_bps=cfg["resting"]["requote_bps"], fees=fees, exit_rules=rules,
        bucket=shadow.TokenBucket(capacity=budget, per_s=10.0),
        min_fill_fraction=paper["min_fill_fraction"],
        max_entry_spread_pct=paper["max_entry_spread_pct"]))
    return strategies


class Engine:
    """Live state and wiring of one shadow-test session."""

    def __init__(self, cfg: dict, strategies: list | None = None) -> None:
        """Builds the state, the strategies, and the recorder from `cfg`.

        Args:
            cfg: The config dict from `config.load_config`.
            strategies: Paper strategies to use instead of the configured
                ones. The replay uses this to test many settings at once.
        """
        self.cfg = cfg
        markets = cfg["markets"]
        self.trade_markets = ([f"{coin}USDT" for coin in markets["usdt"]]
                              + [f"{coin}TMN" for coin in markets["tmn"]])
        self.wallex_markets = self.trade_markets + ["USDTTMN"]
        self.coins = sorted({base_of(m) for m in self.trade_markets})
        self.tmn_markets = [m for m in self.trade_markets
                            if shadow.quote_of(m) == shadow.TMN]
        self.markets_by_coin = collections.defaultdict(list)
        for market in self.trade_markets:
            self.markets_by_coin[base_of(market)].append(market)
        self.books: dict[str, shadow.Book] = {}
        self.side_ts: dict[tuple[str, str], float] = {}
        venues = cfg["venues"]
        self.quotes = fairvalue.Quotes(max_quote_age_s=venues["max_quote_age_s"],
                                       min_venues=venues["min_venues"])
        premium = cfg["premium"]
        self.premiums = {m: fairvalue.Premium(premium["window_s"],
                                              premium["min_samples"])
                         for m in self.trade_markets}
        self.strategies = strategies if strategies is not None else build_strategies(cfg)
        self.thresholds = cfg["episodes"]["thresholds_pct"]
        self.episodes = episodes.Episodes(self.thresholds)
        self.recorder = recorder.Recorder(cfg["run"]["out_dir"],
                                          cfg["run"]["record_levels"])
        self.stats = {name: feeds.new_stats()
                      for name in ["wallex"] + list(venues["enabled"])}
        self.counts = {s.name: collections.Counter() for s in self.strategies}
        self.net_tmn = {s.name: 0.0 for s in self.strategies}
        self.max_gap: dict[str, float] = {}
        self.stop = asyncio.Event()

    def wallex_live(self, now: float) -> bool:
        """Returns True when the Wallex feed sent a message recently.

        All Wallex markets share the feed connection, so a silent feed means
        that every book in memory can be old. Old books with live global
        prices show false gaps, so the engine does not use them.
        """
        stale_s = self.cfg["run"]["wallex_stale_s"]
        return now - self.stats["wallex"]["last"] <= stale_s

    def usdttmn_mid(self) -> float | None:
        """Returns the Wallex USDTTMN mid, or None without a full book."""
        book = self.books.get("USDTTMN")
        if book is None or book.best_bid() is None or book.best_ask() is None:
            return None
        return (book.best_bid() + book.best_ask()) / 2

    def raw_fair_value(self, market: str, now: float) -> float | None:
        """Returns the fair value before the premium, or None."""
        mid = self.quotes.global_mid(coin=base_of(market), now=now)
        if mid is None or shadow.quote_of(market) == shadow.USDT:
            return mid
        usdttmn = self.usdttmn_mid()
        return mid * usdttmn if usdttmn else None

    def fair_value(self, market: str, now: float) -> float | None:
        """Returns the fair value with the premium, or None before warm-up."""
        raw = self.raw_fair_value(market, now)
        premium = self.premiums[market].value(now)
        if raw is None or premium is None:
            return None
        return raw * math.exp(premium)

    def on_book(self, market: str, side: str, levels: list, now: float,
                seed_since: float | None) -> None:
        """Stores a book side and evaluates the markets it changes.

        A REST seed (seed_since is not None) does not replace a side that a
        websocket push updated after the seed request started.
        """
        if seed_since is not None and self.side_ts.get((market, side), 0.0) > seed_since:
            return
        book = self.books.setdefault(market, shadow.Book(bids=[], asks=[], ts=now))
        if side == "bid":
            book.bids = levels
        else:
            book.asks = levels
        book.ts = now
        self.side_ts[(market, side)] = now
        self.recorder.book(now, market, side, levels)
        if market == "USDTTMN":
            for tmn_market in self.tmn_markets:
                self._evaluate(tmn_market, now)
        elif market in self.premiums:
            self._evaluate(market, now)

    def on_trades(self, market: str, trades: list, now: float) -> None:
        """Records Wallex trades and gives them to the strategies."""
        self.recorder.trades(now, market, trades)
        if market not in self.premiums:
            return
        usdttmn = self.usdttmn_mid()
        for price, qty, buyer_maker, _ in trades:
            for strategy in self.strategies:
                self._emit(strategy.on_trade(
                    market=market, now=now, price=price, qty=qty,
                    buyer_maker=buyer_maker, usdttmn=usdttmn))

    def on_quote(self, venue: str, coin: str, bid: float, ask: float,
                 now: float) -> None:
        """Stores a global quote and evaluates the markets of that coin."""
        self.quotes.update(venue=venue, coin=coin, bid=bid, ask=ask, ts=now)
        self.recorder.quote(now, venue, coin, bid, ask)
        for market in self.markets_by_coin.get(coin, []):
            self._evaluate(market, now)

    def _evaluate(self, market: str, now: float) -> None:
        """Updates the gap episodes and the strategies of one market."""
        book = self.books.get(market) if self.wallex_live(now) else None
        fv = self.fair_value(market, now)
        usdttmn = self.usdttmn_mid()
        best_ask = book.best_ask() if book else None
        gap = None
        depth = None
        if fv is not None and best_ask is not None:
            gap = 1 - best_ask / fv
            self.max_gap[market] = max(gap, self.max_gap.get(market, -1.0))
            to_tmn = 1.0 if shadow.quote_of(market) == shadow.TMN else (usdttmn or 0.0)
            depth = {threshold: to_tmn * sum(
                         price * qty for price, qty in book.asks
                         if price <= fv * (1 - threshold / 100))
                     for threshold in self.thresholds}
        for closed in self.episodes.update(market=market, now=now, gap=gap,
                                           depth_tmn=depth):
            self.recorder.episode(closed)
        for strategy in self.strategies:
            self._emit(strategy.on_tick(market=market, now=now, book=book,
                                        fv=fv, usdttmn=usdttmn))

    def _emit(self, events: list[dict]) -> None:
        """Records strategy events and updates the counters."""
        for event in events:
            event["ts"] = round(event["ts"], 3)
            self.recorder.event(event)
            name = event["strategy"]
            self.counts[name][event["type"]] += 1
            if event["type"] == "exit" and event.get("net_tmn") is not None:
                self.net_tmn[name] += event["net_tmn"]
            if event["type"] in ("fill", "exit"):
                log.info("%s %s %s at %.8g%s", name, event["type"],
                         event["market"], event["price"],
                         f" net {event['net_pct']:+.3f}%" if event["type"] == "exit" else "")

    def tick_timers(self, now: float) -> None:
        """Runs the strategy timers once: latency fills, exits, time stops."""
        usdttmn = self.usdttmn_mid()
        fvs = {m: self.fair_value(m, now) for m in self.trade_markets}
        books = self.books if self.wallex_live(now) else {}
        for strategy in self.strategies:
            self._emit(strategy.on_timer(now=now, books=books, fvs=fvs,
                                         usdttmn=usdttmn))

    def sample_premiums(self, now: float) -> None:
        """Adds one sample of log(Wallex mid / raw fair value) per market.

        A market with a spread above `premium.max_spread_pct` gets no sample.
        """
        max_spread = self.cfg["premium"]["max_spread_pct"] / 100
        for market in self.trade_markets:
            raw = self.raw_fair_value(market, now)
            book = self.books.get(market)
            if raw is None or book is None:
                continue
            if book.best_bid() is None or book.best_ask() is None:
                continue
            if book.best_ask() / book.best_bid() - 1 > max_spread:
                continue
            mid = (book.best_bid() + book.best_ask()) / 2
            self.premiums[market].add(now, math.log(mid / raw))

    async def _timer_loop(self) -> None:
        """Runs the strategy timers every 100 ms."""
        while not self.stop.is_set():
            await asyncio.sleep(0.1)
            self.tick_timers(time.time())

    async def _premium_loop(self) -> None:
        """Samples the premium every `premium.sample_s` seconds."""
        every = self.cfg["premium"]["sample_s"]
        while not self.stop.is_set():
            await asyncio.sleep(every)
            self.sample_premiums(time.time())

    def status_line(self, now: float, every: float, previous: dict) -> str:
        """Returns one status line and updates `previous` message counts."""
        feed_parts = []
        for name, stats in self.stats.items():
            rate = (stats["msgs"] - previous.get(name, 0)) / every
            previous[name] = stats["msgs"]
            stale = "" if now - stats["last"] < 30 else " STALE"
            version = f" {stats['endpoint']}" if stats.get("endpoint") else ""
            feed_parts.append(f"{name}{version} {rate:.0f}/s{stale}")
        ready = sum(1 for m in self.trade_markets if self.fair_value(m, now) is not None)
        strategy_parts = []
        for strategy in self.strategies:
            counts = self.counts[strategy.name]
            strategy_parts.append(
                f"{strategy.name}: sig {counts['signal']} fill {counts['fill']} "
                f"miss {counts['miss']} exit {counts['exit']} "
                f"open {len(strategy.positions)} "
                f"net {self.net_tmn[strategy.name]:+.0f} "
                f"rate-limited {strategy.rate_limited}")
        top = sorted(self.max_gap.items(), key=lambda item: item[1], reverse=True)[:3]
        self.max_gap = {}
        gaps = " ".join(f"{market} {100 * gap:+.2f}%" for market, gap in top)
        return (f"books {len(self.books)}/{len(self.wallex_markets)} "
                f"fv_ready {ready}/{len(self.trade_markets)} | max gap {gaps or '-'} | "
                + ", ".join(feed_parts) + " | " + " | ".join(strategy_parts))

    def _check_disk(self) -> None:
        """Applies `disk_action` to the free space of the output folder."""
        run_cfg = self.cfg["run"]
        free_mb = shutil.disk_usage(run_cfg["out_dir"]).free / 2**20
        action = disk_action(free_mb=free_mb,
                             min_record_mb=run_cfg["min_free_mb_record"],
                             min_run_mb=run_cfg["min_free_mb_run"],
                             raw_off=not self.recorder.raw_enabled)
        if action == "stop":
            log.error("only %.0f MB free on disk: stopping the run", free_mb)
            self.stop.set()
        elif action == "stop_raw" and self.recorder.raw_enabled:
            log.warning("only %.0f MB free on disk: raw recording is off", free_mb)
            self.recorder.raw_enabled = False
        elif action == "resume_raw":
            log.warning("%.0f MB free on disk again: raw recording is on", free_mb)
            self.recorder.raw_enabled = True

    async def _status_loop(self) -> None:
        """Logs a status line and flushes the files."""
        every = self.cfg["run"]["status_every_s"]
        previous: dict = {}
        status_path = f"{self.cfg['run']['out_dir']}/status.log"
        while not self.stop.is_set():
            await asyncio.sleep(every)
            self._check_disk()
            line = self.status_line(time.time(), every, previous)
            log.info(line)
            with open(status_path, "a", encoding="utf-8") as status_file:
                status_file.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {line}\n")
            self.recorder.flush()

    async def run(self, duration_s: float | None = None) -> None:
        """Runs the feeds and the loops until a signal or `duration_s`."""
        loop = asyncio.get_running_loop()
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signal_number, self.stop.set)
        if duration_s:
            loop.call_later(duration_s, self.stop.set)
        tasks = [asyncio.create_task(feeds.run_wallex(
            markets=self.wallex_markets, on_book=self.on_book,
            on_trades=self.on_trades, stats=self.stats["wallex"], stop=self.stop))]
        for venue in self.cfg["venues"]["enabled"]:
            runner = feeds.QUOTE_RUNNERS[venue]
            tasks.append(asyncio.create_task(runner(
                coins=self.coins, on_quote=self.on_quote,
                stats=self.stats[venue], stop=self.stop)))
        tasks += [asyncio.create_task(self._timer_loop()),
                  asyncio.create_task(self._premium_loop()),
                  asyncio.create_task(self._status_loop())]
        log.info("started: %d Wallex markets, %d coins, venues %s",
                 len(self.wallex_markets), len(self.coins),
                 ",".join(self.cfg["venues"]["enabled"]))
        await self.stop.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.recorder.flush()
        self.recorder.close()
        log.info("stopped")
