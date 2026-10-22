"""Runner of the sweep bot: wiring and the time loop of each mode.

`Runner` builds the shared parts (market state, filters, store) and the
executors (the bot and the no-AI twin). `Runner.tick` runs one 100 ms
step. Two drivers call it:

* `replay` reads lagcatcher raw files and runs the ticks on replay time.
* `run_live` reads the live feeds and runs the ticks on the clock. It also
  runs the AI loop: the supervisor timer, hold-or-cut, trade logs, news,
  and the daily report and review.

The executor never waits for the model. All model calls run as separate
asyncio tasks.
"""

import asyncio
import json
import logging
import os
import pathlib
import signal
import time
from collections.abc import Iterable

from lagcatcher import feeds
from sweepbot import broker
from sweepbot import endgame
from sweepbot import executor
from sweepbot import filters as filters_mod
from sweepbot import market as market_mod
from sweepbot import report
from sweepbot import risk as risk_mod
from sweepbot import rules as rules_mod
from sweepbot import store as store_mod
from sweepbot import twin as twin_mod
from sweepbot.ai import validator

log = logging.getLogger(__name__)

# The smallest time between two plan calls, in seconds.
MIN_PLAN_GAP_S = 60.0


class Runner:
    """The parts of one bot run."""

    def __init__(self, cfg: dict, *, mode: str, store: store_mod.Store,
                 start_ts: float, supervisor=None, with_twin: bool = True,
                 record_raw: bool = False, live_broker=None) -> None:
        """Builds the parts.

        Args:
            cfg: The config dict from `sweepbot.config.load_config`.
            mode: "live", "paper" or "replay".
            store: The SQLite store.
            start_ts: The start time of the run.
            supervisor: An `ai.supervisor.Supervisor`, or None for the
                default plan only.
            with_twin: True runs the no-AI twin next to the bot.
            record_raw: True writes the lagcatcher raw files.
            live_broker: The `WallexBroker` of live mode.
        """
        if mode not in ("live", "paper", "replay"):
            raise ValueError(f"unknown mode {mode!r}")
        if (mode == "live") != (live_broker is not None):
            raise ValueError("live mode needs a live broker, and only live mode")
        self.cfg = cfg
        self.mode = mode
        self.store = store
        self.supervisor = supervisor
        self.market = market_mod.Market(cfg, record_raw=record_raw)
        self.filters = filters_mod.Filters(cfg)
        if live_broker is not None:
            self.broker = live_broker
        else:
            self.broker = broker.PaperBroker(cfg, name=mode)
            self.market.add_listener(on_book=self.broker.on_book,
                                     on_trade=self.broker.on_trade)
        self._tasks: set[asyncio.Task] = set()
        self._last_plan_call = -MIN_PLAN_GAP_S
        if supervisor is None:
            plan = validator.default_plan(cfg)
            plan_id = store.add_plan(ts=start_ts, provider="config", model="default",
                                     raw_json=plan.model_dump_json(),
                                     validated_json=plan.model_dump_json(),
                                     accepted=True, notes="default plan, AI off")
            plan_source = lambda now: (plan, plan_id)
        else:
            plan_source = supervisor.active_plan
        bot_risk = risk_mod.RiskGuard(cfg, start_ts)
        rule_set = rules_mod.load_rules(cfg["bot"]["market_rules"])
        converter = endgame.UsdtConverter(
            convert_at=bot_risk.usdt_convert_at(), market=self.market,
            broker_=self.broker,
            rule=rule_set.get(endgame.MARKET, rules_mod.MarketRule()),
            slippage_pct=cfg["slots"]["cut_slippage_pct"], store=store, mode=mode)
        self.bot = executor.Executor(
            cfg, market=self.market, broker_=self.broker, store=store, mode=mode,
            risk=bot_risk, filters=self.filters, converter=converter,
            plan_source=plan_source,
            on_trade=self._on_trade if supervisor else None,
            on_hold_or_cut=self._on_hold_or_cut if supervisor else None)
        self.twin = (twin_mod.build_twin(cfg, market=self.market, store=store,
                                         filters=self.filters, start_ts=start_ts)
                     if with_twin else None)
        self.executors = [self.bot] + ([self.twin] if self.twin else [])

    # Tick ------------------------------------------------------------------

    def feed_filters(self, now: float) -> None:
        """Gives the filters their samples and stores the filter events."""
        for coin in self.market.engine.coins:
            mid = self.market.global_mid(coin, now)
            if mid is not None:
                self.filters.on_global_mid(coin, mid, now)
        rate, _ = self.market.usdt_rate(now)
        if rate:
            self.filters.on_usdt_rate(rate, now)
        for event in self.filters.update(now):
            self.store.add_filter_event(ts=event["ts"], filter_name=event["filter"],
                                        key=event["key"], kind=event["kind"])
            if event["kind"] == "start" and self.supervisor is not None:
                self.supervisor.alert(f"{event['filter']} {event['key']} "
                                      f"{event.get('move_pct', 0):+.2f}%", now)

    async def tick(self, now: float) -> None:
        """Runs one step of the filters and the executors at `now`."""
        for executor_ in self.executors:
            executor_.broker.advance(now)
        self.feed_filters(now)
        for executor_ in self.executors:
            await executor_.step(now)

    # AI hooks --------------------------------------------------------------

    def _spawn(self, coroutine) -> None:
        """Runs a model job as a task, so that the executor does not wait."""
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_trade(self, trade: dict) -> None:
        """Asks the model for the decision log text of a closed trade."""
        now = trade["ts"]
        self._spawn(self.supervisor.trade_log(trade, now))
        if trade["net_pct"] < 0:
            self.supervisor.alert(f"loss on {trade['market']} {trade['net_pct']:+.2f}%", now)

    def _on_hold_or_cut(self, context: dict) -> None:
        """Asks the model whether to hold or cut a slow position."""

        async def ask() -> None:
            decision, reason = await self.supervisor.hold_or_cut(context, context["now"])
            self.bot.apply_hold_or_cut(context["market"], decision, reason, time.time())

        self._spawn(ask())

    async def ai_step(self, now: float) -> None:
        """Starts the model jobs that are due. Called about once per second."""
        if self.supervisor is None:
            return
        # Alerts can come in bursts. One plan call per minute is enough.
        if self.supervisor.due(now) and now - self._last_plan_call >= MIN_PLAN_GAP_S:
            self._last_plan_call = now
            self._spawn(self.supervisor.run_plan(now, self.bot.snapshot(now)))
        self._spawn(self.supervisor.refresh_news(now))

    def daily(self, day: str, now: float) -> str:
        """Writes the report of a finished Tehran day and starts the review."""
        out_dir = pathlib.Path(self.cfg["bot"]["out_dir"]) / "reports"
        path = report.write_daily_report(self.store, day, str(out_dir))
        if self.supervisor is not None:
            start, end = report.day_bounds(day)
            trades = self.store.rows("trades", "ts >= ? AND ts < ?", (start, end))
            summary = {mode: report.mode_summary([t for t in trades if t["mode"] == mode])
                       for mode in sorted({t["mode"] for t in trades})}
            self._spawn(self.supervisor.daily_review(
                {"day": day, "summary": summary, "report_markdown": open(path).read()}, now))
        return path

    # Panel -----------------------------------------------------------------

    def live_state(self, now: float) -> dict:
        """Returns the live state that the panel shows (DESIGN section 15).

        The panel runs as its own process. It reads this state from
        `state.json` and the history from SQLite.
        """
        bot = self.bot
        risk = bot.risk
        equity = bot.equity(now)
        close = risk.usdt_convert_at()
        slots_out = []
        for market, slot in sorted(bot.slots.items()):
            view = bot.views.get(market)
            plan_slot = bot._slot_plan.get(market)
            selling = slot.exit_id is not None
            order_id = slot.exit_id or slot.bid_id
            slots_out.append({
                "market": market, "state": slot.state,
                "depth_pct": (plan_slot.depth_pct if plan_slot else
                              (view.depth_pct if view and view.want else None)),
                "fv": view.fv if view else None,
                "order_side": ("sell" if selling else "buy") if order_id else None,
                "order_price": (slot.exit_price if selling else slot.bid_price) if order_id else None,
                "qty": slot.qty if slot.has_position() else (slot.bid_qty if order_id else None),
                "entry_price": slot.entry_price(),
                "spread_pct": self.market.spread_pct(market),
                "age_s": now - (slot.entry_ts or slot.order_ts) if (slot.entry_ts or slot.order_ts) and slot.busy() else None,
                "block": (view.block or view.entry_block or view.cut) if view else None,
            })
        stats = self.market.engine.stats
        rate, source = self.market.usdt_rate(now)
        plan = bot.plan
        plan_out = None
        if plan is not None:
            ai_plan = self.supervisor is not None and bot.plan_id is not None and plan.reasoning != "default plan"
            plan_out = {"id": bot.plan_id, "source": "ai" if ai_plan else "default",
                        "reasoning": plan.reasoning, "fx_risk": plan.fx_risk, "pause": plan.pause,
                        "valid_until": None,
                        "slots": [s.model_dump() for s in plan.slots]}
        usdt_exposure = bot._usdt_exposure_tmn(now)
        return {
            "ts": now, "mode": self.mode,
            "control": {"paused": bot.paused, "flatten": bot.force_flatten},
            "halted": risk.halted(now),
            "end": {"end_ts": risk.end_ts, "flatten_at": risk.flatten_at(),
                    "usdt_convert_at": close,
                    "usdttmn_close": close + 1800.0 if close is not None else None},
            "kpi": {"equity": equity, "start_equity": risk.start_equity, "peak": max(risk.peak, equity),
                    "drawdown_pct": 100 * (equity / max(risk.peak, equity) - 1),
                    "open_slots": sum(s.busy() for s in bot.slots.values()),
                    "max_slots": self.cfg["limits"]["max_slots"],
                    "usdt_exposure_tmn": usdt_exposure,
                    "usdt_share_pct": 100 * usdt_exposure / equity if equity else None,
                    "usdt_limit_pct": 100 * self.cfg["limits"]["max_usdt_share"],
                    "orders_per_10s": self.cfg["limits"]["orders_per_10s"],
                    "budget_refused": bot.budget.refused},
            "slots": slots_out,
            "health": {"wallex_age_s": now - stats["wallex"]["last"] if stats["wallex"]["last"] else None,
                       "venues": [{"name": name, "age_s": now - item["last"] if item["last"] else None}
                                  for name, item in stats.items() if name != "wallex"],
                       "usdt_rate": rate, "usdt_source": source,
                       "filters_active": [f"{name}:{key}" for name, key in self.filters.active()]},
            "plan": plan_out,
            "ai": ({"enabled": True, **self.supervisor.status()} if self.supervisor
                   else {"enabled": False, "failures": 0, "running": False, "news": None,
                         "budget_usd": self.cfg["ai"]["budget_usd"]}),
            "limits": self.cfg["limits"],
        }

    def write_state(self, path: str, now: float) -> None:
        """Writes `live_state` to `path` in one step, so a reader never sees half a file."""
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as state_file:
            json.dump(self.live_state(now), state_file, default=str)
        os.replace(tmp, path)

    def apply_control(self, path: str, now: float) -> None:
        """Reads the control file of the panel and applies it to the bot.

        A missing or broken file changes nothing.
        """
        try:
            with open(path, encoding="utf-8") as control_file:
                control = json.load(control_file)
        except (OSError, ValueError):
            return
        self.bot.set_control(paused=bool(control.get("paused")),
                             flatten=bool(control.get("flatten")), now=now)

    async def shutdown(self) -> None:
        """Cancels the open orders and waits for the model tasks."""
        for executor_ in self.executors:
            for order in await executor_.broker.open_orders():
                await executor_.broker.cancel(order.id)
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=5.0)
        self.market.close()


async def replay(runner: Runner, records: Iterable[tuple[float, str, dict]], *,
                 start_ts: float | None = None, end_ts: float | None = None) -> None:
    """Runs recorded raw data through the runner on replay time.

    Before each record, the replay runs every premium sample and every
    executor tick that is due at or before the record time. Records before
    `start_ts` warm up the books, the quotes and the premium, but the
    executors do not trade on them.

    Args:
        runner: The runner. Its supervisor must be None.
        records: (ts, kind, record) tuples from `lagcatcher.replay.load_records`.
        start_ts: The first tick time, or None for the first record.
        end_ts: The replay stops at this time, or None for the last record.
    """
    market = runner.market
    timer_s = runner.cfg["bot"]["timer_s"]
    premium_s = runner.cfg["premium"]["sample_s"]
    next_timer = next_premium = None
    for ts, kind, record in records:
        if end_ts is not None and ts >= end_ts:
            break
        if next_timer is None:
            next_timer = ts if start_ts is None else max(ts, start_ts)
            next_premium = ts + premium_s
        while next_premium <= ts:
            market.sample_premiums(next_premium)
            next_premium += premium_s
        while next_timer <= ts:
            await runner.tick(next_timer)
            next_timer += timer_s
        if kind == "book":
            market.on_book(record["m"], record["s"],
                           [tuple(level) for level in record["lv"]], ts)
        elif kind == "trade":
            market.on_trades(record["m"], [(record["p"], record["q"], record["bm"],
                                            record.get("tts"))], ts)
        else:
            market.on_quote(record["v"], record["c"], record["b"], record["a"], ts)
    if next_timer is not None:
        await runner.tick(next_timer)


async def run_live(runner: Runner, duration_s: float | None = None) -> None:
    """Runs the live feeds, the 100 ms ticks and the AI loop until a signal."""
    cfg = runner.cfg
    market = runner.market
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_number, stop.set)
    if duration_s:
        loop.call_later(duration_s, stop.set)
    stats = market.engine.stats
    tasks = [asyncio.create_task(feeds.run_wallex(
        markets=market.engine.wallex_markets, on_book=market.on_book,
        on_trades=market.on_trades, stats=stats["wallex"], stop=stop))]
    for venue in cfg["venues"]["enabled"]:
        tasks.append(asyncio.create_task(feeds.QUOTE_RUNNERS[venue](
            coins=market.engine.coins, on_quote=market.on_quote,
            stats=stats[venue], stop=stop)))

    async def every(period_s: float, job) -> None:
        while not stop.is_set():
            await asyncio.sleep(period_s)
            try:
                await job(time.time())
            except Exception:  # A bug in one tick must not stop the bot.
                log.exception("loop job failed")

    async def premium(now: float) -> None:
        market.sample_premiums(now)

    day = {"current": risk_mod.tehran_day(time.time())}

    async def status(now: float) -> None:
        market.engine._check_disk()
        if market.engine.stop.is_set():
            log.error("disk almost full: stopping")
            stop.set()
        lines = [executor_.status(now) for executor_ in runner.executors]
        for line in lines:
            log.info(line)
        out_dir = pathlib.Path(cfg["bot"]["out_dir"])
        with open(out_dir / "status.log", "a", encoding="utf-8") as status_file:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
            status_file.write("".join(f"{stamp} {line}\n" for line in lines))
        market.engine.recorder.flush()
        today = risk_mod.tehran_day(now)
        if today != day["current"]:
            log.info("daily report: %s", runner.daily(day["current"], now))
            day["current"] = today

    if runner.mode == "live":
        cancelled = await runner.broker.cancel_unknown(market.trade_markets)
        log.warning("LIVE mode: %d old orders cancelled", len(cancelled))
    out_dir = pathlib.Path(cfg["bot"]["out_dir"])
    state_path, control_path = str(out_dir / "state.json"), str(out_dir / "control.json")

    async def panel_io(now: float) -> None:
        runner.apply_control(control_path, now)
        runner.write_state(state_path, now)

    tasks += [asyncio.create_task(every(cfg["bot"]["timer_s"], runner.tick)),
              asyncio.create_task(every(1.0, panel_io)),
              asyncio.create_task(every(cfg["premium"]["sample_s"], premium)),
              asyncio.create_task(every(cfg["bot"]["status_every_s"], status)),
              asyncio.create_task(every(1.0, runner.ai_step))]
    log.info("sweepbot %s started: %d markets, venues %s, AI %s", runner.mode,
             len(market.trade_markets), ",".join(cfg["venues"]["enabled"]),
             "on" if runner.supervisor else "off")
    await stop.wait()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await runner.shutdown()
    log.info("sweepbot stopped")
