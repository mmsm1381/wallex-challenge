"""The sweep executor: the fast path of the bot (DESIGN section 3).

`Executor.step` runs every 100 ms. One step:

1. takes the order events of the broker and gives them to the slots, and
   writes orders, fills and closed trades to the store,
2. updates the equity and the risk guard,
3. reads the current plan (the AI plan or the default plan),
4. builds the view of each slot (fair value, book, filters, risk) and lets
   the slot send its orders.

The executor never waits for a model. It reads the plan through
`plan_source`, which returns at once. It asks for a hold-or-cut answer
through `on_hold_or_cut`, and the answer comes back later through
`apply_hold_or_cut`. The same class runs the bot and the no-AI twin.
"""

import logging
import statistics
from collections.abc import Callable

from sweepbot import allocation as allocation_mod
from sweepbot import broker
from sweepbot import config as config_mod
from sweepbot import exits
from sweepbot import filters as filters_mod
from sweepbot import market as market_mod
from sweepbot import risk as risk_mod
from sweepbot import rules as rules_mod
from sweepbot import slots
from sweepbot import store as store_mod
from sweepbot.ai import schema

log = logging.getLogger(__name__)

PlanSource = Callable[[float], tuple[schema.Plan, int | None]]


def _number(value: float) -> str:
    """Formats a price or a quantity for a person: 21,710,052,000 or 0.00004606."""
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    if abs(value) >= 1:
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    return f"{value:.10f}".rstrip("0").rstrip(".")


def trade_text(trade: dict) -> str:
    """Returns the rule-based decision log text of one closed trade."""
    net_tmn = trade["net_tmn"]
    money = f"{net_tmn:+,.0f} toman" if net_tmn is not None else "toman value unknown"
    return (f"{trade['market']}: bought {_number(trade['qty'])} at {_number(trade['entry_price'])}"
            f" ({trade['depth_pct']:.2f}% below fair value {_number(trade['fv_at_bid'])}),"
            f" sold at {_number(trade['exit_price'])} by {trade['exit_reason']} after"
            f" {trade['hold_s']:.0f} s. Net {trade['net_pct']:+.2f}% ({money}).")


class Executor:
    """Runs the slots of one bot (live, paper, replay or twin)."""

    def __init__(self, cfg: dict, *, market: market_mod.Market,
                 broker_: broker.Broker, store: store_mod.Store, mode: str,
                 risk: risk_mod.RiskGuard, filters: filters_mod.Filters,
                 plan_source: PlanSource,
                 on_trade: Callable[[dict], None] | None = None,
                 on_hold_or_cut: Callable[[dict], None] | None = None,
                 converter=None, funder=None, allocate: bool = False) -> None:
        """Wires the parts.

        Args:
            cfg: The config dict from `sweepbot.config.load_config`.
            market: The shared market state.
            broker_: The broker of this bot.
            store: The SQLite store.
            mode: "live", "paper", "replay" or "twin". It labels the rows.
            risk: The risk guard of this bot.
            filters: The shared filters. The runner feeds them.
            plan_source: Returns (plan, plan id) for a time.
            on_trade: Called with each closed trade dict.
            on_hold_or_cut: Called with the context of a position that has
                no exit fill after `slots.hold_or_cut_after_s`.
            converter: The `endgame.UsdtConverter` of the bot, or None.
            funder: The `funding.UsdtFunder` of the bot, or None. Until it
                is done, no new bid goes to a USDT market.
            allocate: True chooses the bid markets from all markets with
                `allocation.allocate` instead of the plan list. The bot uses
                it when no AI plan exists. The twin uses it too.
        """
        self.cfg = cfg
        self.market = market
        self.broker = broker_
        self.store = store
        self.mode = mode
        self.risk = risk
        self.filters = filters
        self.plan_source = plan_source
        self.on_trade = on_trade
        self.on_hold_or_cut = on_hold_or_cut
        self.converter = converter
        self.funder = funder
        limits = cfg["limits"]
        self.budget = slots.OrderBudget(limits["orders_per_10s"], limits["safety_reserve"])
        self.rules = rules_mod.load_rules(cfg["bot"]["market_rules"])
        self.slots: dict[str, slots.Slot] = {}
        self._owner: dict[str, slots.Slot] = {}
        self._slot_plan: dict[str, schema.SlotPlan] = {}
        self._fv_at_bid: dict[str, float] = {}
        self.realized_tmn = 0.0
        self.trades: list[dict] = []
        self.plan_id: int | None = None
        self.plan: schema.Plan | None = None
        self.halt_reason: str | None = None
        # Set by a person through the panel (DESIGN section 15).
        self.paused = False
        self.force_flatten = False
        self.allocate = allocate
        self._no_funds_logged: dict[str, float] = {}
        # The last allocation, for the panel.
        self.allocation: dict | None = None
        # The last view of each slot, for the panel.
        self.views: dict[str, slots.SlotView] = {}
        # The toman value of the whole wallet, set by the runner after each
        # wallet read. None: no read yet, or an asset without a price.
        self.wallet_tmn: float | None = None

    # Equity ----------------------------------------------------------------

    def equity(self, now: float) -> float:
        """Returns the equity in toman.

        With a wallet read, the equity is the toman value of the whole
        wallet. It includes the fees and the currency moves of the USDT that
        the bot holds. Without one (the twin, a replay, or before the first
        read), it is the model equity of `model_equity`.
        """
        if self.wallet_tmn is not None:
            return self.wallet_tmn
        return self.model_equity(now)

    def model_equity(self, now: float) -> float:
        """Returns the start equity + closed net + open positions at their mark.

        An open position is marked at fair value, or at the best bid when
        fair value is not known.
        """
        equity = self.risk.start_equity + self.realized_tmn
        for market, slot in self.slots.items():
            if not slot.has_position():
                continue
            mark = self.market.fair_value(market, now)
            if mark is None:
                book = self.market.book(market)
                mark = book.best_bid() if book and book.best_bid() else slot.entry_price()
            open_quote = (slot.qty * mark + slot.exit_value - slot.entry_cost
                          - slot.entry_fees - slot.exit_fees)
            open_tmn = self.market.to_tmn(market, open_quote, now)
            equity += open_tmn or 0.0
        return equity

    def _usdt_exposure_tmn(self, now: float) -> float:
        """Returns the toman value of the busy USDT slots at their plan size."""
        total = 0.0
        for market, slot in self.slots.items():
            if slot.busy() and market_mod.quote_of(market) == market_mod.USDT:
                plan_slot = self._slot_plan.get(market)
                total += plan_slot.size_tmn if plan_slot else 0.0
        return total

    # Plan ------------------------------------------------------------------

    def _wanted(self, plan: schema.Plan, halted: str | None,
                now: float = 0.0) -> dict[str, schema.SlotPlan]:
        """Returns the slots that the executor may bid on now."""
        if plan.pause or halted or self.paused or self.force_flatten:
            return {}
        if self.allocate:
            return self._allocated(now)
        wanted = {}
        for slot_plan in plan.slots:
            if (plan.fx_risk == "high"
                    and market_mod.quote_of(slot_plan.market) == market_mod.TMN):
                continue
            wanted[slot_plan.market] = slot_plan
        return wanted

    def ineligible(self, market: str, now: float) -> str | None:
        """Returns why `market` may not have a bid now, or None."""
        if not self.market.wallex_live(now):
            return "wallex_stale"
        coin = market_mod.base_of(market)
        if self.market.global_mid(coin, now) is None:
            return "global_stale"
        if self.market.fair_value(market, now) is None:
            return "no_fair_value"
        spread = self.market.spread_pct(market)
        if spread is None or spread > self.cfg["filters"]["max_spread_pct"]:
            return "wide_spread"
        is_tmn = market_mod.quote_of(market) == market_mod.TMN
        block = self.filters.blocks(market, coin, is_tmn)
        if block:
            return block
        if not is_tmn and self.funder is not None and not self.funder.done:
            return "usdt_funding"
        slot = self.slots.get(market)
        if slot is not None and now < slot.cooldown_until:
            return "cooldown"
        return None

    def _allocated(self, now: float) -> dict[str, schema.SlotPlan]:
        """Returns the markets of `allocation.allocate` as plan slots, and logs skips."""
        alloc = self.cfg["allocation"]
        ranking = [m for m in alloc["ranking"] if m in self.market.trade_markets]
        reasons = {m: self.ineligible(m, now) for m in ranking}
        busy = {}
        for market, slot in self.slots.items():
            if slot.busy():
                kind = (allocation_mod.BID if slot.state in (slots.BID_PENDING, slots.BID_LIVE)
                        else allocation_mod.HOLDING)
                busy[market] = (kind, slot.order_ts)
        result = allocation_mod.allocate(
            ranking=ranking, caps={market_mod.TMN: alloc["tmn_slots"],
                                   market_mod.USDT: alloc["usdt_slots"]},
            ineligible=reasons, busy=busy, now=now, switch_min_s=alloc["switch_min_s"])
        self.allocation = {"tmn_slots": alloc["tmn_slots"], "usdt_slots": alloc["usdt_slots"],
                           "selected": result.selected, "no_funds": result.no_funds,
                           "used": result.used,
                           "ineligible": {m: r for m, r in reasons.items() if r}}
        if self.mode != "twin":
            for market in result.no_funds:
                if now - self._no_funds_logged.get(market, -1e18) < alloc["log_every_s"]:
                    continue
                self._no_funds_logged[market] = now
                quote = market_mod.quote_of(market)
                used = ", ".join(result.used.get(quote, [])) or "none"
                self.store.add_log(
                    ts=now, kind="alloc",
                    text=f"[{self.mode}] {market} qualifies, but no funds: the "
                         f"{quote} places are used by {used}.",
                    fields={"mode": self.mode, "market": market, "used": result.used.get(quote)})
        size = self.cfg["plan"]["size_tmn"]
        return {m: schema.SlotPlan(market=m, depth_pct=config_mod.default_depth(self.cfg, m),
                                   size_tmn=size)
                for m in result.selected}

    # Step ------------------------------------------------------------------

    async def step(self, now: float) -> None:
        """Runs one executor tick at `now`."""
        await self._handle_events(now)
        equity = self.equity(now)
        for event in self.risk.update_equity(now, equity):
            self.store.add_log(ts=now, kind="risk", text=f"[{self.mode}] {event['text']}",
                               fields={"mode": self.mode, **event})
        halted = self.risk.halted(now)
        if halted != self.halt_reason:
            if halted:
                self.store.add_log(ts=now, kind="risk",
                                   text=f"[{self.mode}] new bids stop: {halted}",
                                   fields={"mode": self.mode, "halt": halted})
            self.halt_reason = halted
        self.plan, self.plan_id = self.plan_source(now)
        wanted = self._wanted(self.plan, halted, now)
        open_count = sum(slot.busy() for slot in self.slots.values())
        usdt_exposure = self._usdt_exposure_tmn(now)
        flatten = self.risk.must_flatten(now) or self.force_flatten
        for market in sorted(set(self.slots) | set(wanted)):
            slot = self.slots.get(market)
            if slot is None:
                rule = self.rules.get(market, rules_mod.MarketRule())
                slot = self.slots[market] = slots.Slot(market, self.cfg, rule.price_tick,
                                                       rule.min_qty)
            slot_plan = wanted.get(market)
            was_busy = slot.busy()
            view = self._view(market, slot, slot_plan, now, open_count,
                              usdt_exposure, equity, flatten)
            self.views[market] = view
            before = set(slot.order_ids())
            await slot.step(now, view, self.broker, self.budget)
            if self._register_orders(slot, before, now):
                self._fv_at_bid[market] = view.fv
            if slot.busy() and not was_busy:
                open_count += 1
                self._slot_plan[market] = slot_plan
                if market_mod.quote_of(market) == market_mod.USDT:
                    usdt_exposure += slot_plan.size_tmn
            self._maybe_ask_hold_or_cut(market, slot, now)
        for market in [m for m, s in self.slots.items() if not s.busy() and m not in wanted
                       and s.state == slots.IDLE]:
            del self.slots[market]
            self.views.pop(market, None)
        if self.funder is not None:
            await self.funder.step(now)
        if self.converter is not None:
            await self.converter.step(now)

    def _view(self, market: str, slot: slots.Slot, slot_plan: schema.SlotPlan | None,
              now: float, open_count: int, usdt_exposure: float, equity: float,
              flatten: bool) -> slots.SlotView:
        """Builds what the slot needs to know now."""
        live = self.market.wallex_live(now)
        book = self.market.book(market) if live else None
        coin = market_mod.base_of(market)
        is_tmn = market_mod.quote_of(market) == market_mod.TMN
        block = None
        if not live:
            block = "wallex_stale"
        elif self.market.global_mid(coin, now) is None:
            block = "global_stale"
        else:
            block = self.filters.blocks(market, coin, is_tmn)
        entry_block = None
        if slot_plan is not None and not slot.busy():
            spread = self.market.spread_pct(market)
            if spread is None or spread > self.cfg["filters"]["max_spread_pct"]:
                entry_block = "wide_spread"
            elif not is_tmn and self.funder is not None and not self.funder.done:
                entry_block = "usdt_funding"
            else:
                entry_block = self.risk.check_bid(
                    now=now, market=market, size_tmn=slot_plan.size_tmn,
                    depth_pct=slot_plan.depth_pct, open_count=open_count,
                    usdt_exposure_tmn=usdt_exposure, equity_tmn=equity)
        cut = None
        if slot.has_position() and slot.entry_ts is not None:
            cut = exits.cut_reason(now=now, entry_ts=slot.entry_ts,
                                   max_hold_s=self.cfg["limits"]["max_hold_s"],
                                   cut_requested=slot.cut_requested, flatten=flatten)
        qty_for = None
        if slot_plan is not None:
            size_quote = slot_plan.size_tmn
            if not is_tmn:
                rate, _ = self.market.usdt_rate(now)
                size_quote = slot_plan.size_tmn / rate if rate else None
            if size_quote:
                rule = self.rules.get(market, rules_mod.MarketRule())
                qty_for = lambda price, size=size_quote, rule=rule: rule.order_qty(size, price)
        return slots.SlotView(
            fv=self.market.fair_value(market, now),
            best_bid=book.best_bid() if book else None,
            best_ask=book.best_ask() if book else None,
            want=slot_plan is not None, block=block, entry_block=entry_block,
            depth_pct=slot_plan.depth_pct if slot_plan else 0.0,
            qty_for=qty_for, cut=cut)

    def _register_orders(self, slot: slots.Slot, before: set, now: float) -> bool:
        """Stores the orders that the slot sent in this step.

        Returns:
            True when the slot sent a new bid.
        """
        new_bid = False
        for order_id in set(slot.order_ids()) - before:
            self._owner[order_id] = slot
            if order_id == slot.bid_id:
                side, price, qty, reason = broker.BUY, slot.bid_price, slot.bid_qty, "bid"
                new_bid = True
            else:
                side, price, qty, reason = (broker.SELL, slot.exit_price, slot.qty,
                                            slot.exit_kind)
            self.store.add_order(order_id=order_id, ts=now, market=slot.market,
                                 side=side, price=price, qty=qty, status="pending",
                                 reason=reason, plan_id=self.plan_id, mode=self.mode)
        return new_bid

    async def _handle_events(self, now: float) -> None:
        """Gives the broker events to the slots and stores them."""
        for event in await self.broker.poll():
            slot = self._owner.get(event.order_id)
            if slot is None:
                continue
            fill_id = None
            if event.kind == "fill":
                fill_id = self.store.add_fill(order_id=event.order_id, ts=event.ts,
                                              price=event.price, qty=event.qty,
                                              fee=event.fee, maker=event.maker)
                self.store.set_order_status(event.order_id, "fill")
            else:
                self.store.set_order_status(event.order_id, event.kind)
            if event.kind in ("cancelled", "rejected"):
                self._owner.pop(event.order_id, None)
            slot.on_event(event, fill_id)
            for closed in slot.closed:
                self._book_trade(closed, now)
            slot.closed.clear()

    def _book_trade(self, closed: slots.ClosedTrade, now: float) -> None:
        """Stores a closed trade, its log row, and updates the equity."""
        net_tmn = self.market.to_tmn(closed.market, closed.net_quote, now)
        fv_at_bid = self._fv_at_bid.pop(closed.market, None) or closed.entry_price
        trade = {
            "ts": closed.exit_ts, "mode": self.mode, "market": closed.market,
            "entry_ts": closed.entry_ts, "qty": closed.qty,
            "entry_price": closed.entry_price, "exit_price": closed.exit_price,
            "fv_at_bid": fv_at_bid,
            "depth_pct": 100 * (1 - closed.entry_price / fv_at_bid),
            "net_quote": closed.net_quote, "net_tmn": net_tmn,
            "net_pct": closed.net_pct, "hold_s": closed.exit_ts - closed.entry_ts,
            "exit_reason": closed.exit_reason, "plan_id": self.plan_id,
        }
        trade["id"] = self.store.add_trade(
            ts=closed.exit_ts, market=closed.market, mode=self.mode,
            entry_fill_ids=closed.entry_fill_ids, exit_fill_ids=closed.exit_fill_ids,
            entry_price=closed.entry_price, exit_price=closed.exit_price,
            qty=closed.qty, net_quote=closed.net_quote, net_tmn=net_tmn,
            net_pct=closed.net_pct, hold_s=trade["hold_s"],
            exit_reason=closed.exit_reason)
        self.store.add_log(ts=closed.exit_ts, kind="trade",
                           text=f"[{self.mode}] {trade_text(trade)}", fields=trade)
        self.realized_tmn += net_tmn or 0.0
        self.trades.append(trade)
        self._slot_plan.pop(closed.market, None)
        log.info("[%s] %s", self.mode, trade_text(trade))
        if self.on_trade is not None:
            self.on_trade(trade)

    # Hold or cut -----------------------------------------------------------

    def _maybe_ask_hold_or_cut(self, market: str, slot: slots.Slot, now: float) -> None:
        """Asks once per position when it has no exit fill after the wait."""
        if (self.on_hold_or_cut is None or not slot.has_position()
                or slot.hold_or_cut_asked or slot.entry_ts is None
                or now - slot.entry_ts < self.cfg["slots"]["hold_or_cut_after_s"]):
            return
        slot.hold_or_cut_asked = True
        book = self.market.book(market)
        rate, source = self.market.usdt_rate(now)
        self.on_hold_or_cut({
            "mode": self.mode, "market": market, "now": now,
            "age_s": now - slot.entry_ts, "qty": slot.qty,
            "entry_price": slot.entry_price(), "exit_price": slot.exit_price,
            "fv_at_bid": self._fv_at_bid.get(market),
            "fv_now": self.market.fair_value(market, now),
            "global_mid": self.market.global_mid(market_mod.base_of(market), now),
            "best_bid": book.best_bid() if book else None,
            "best_ask": book.best_ask() if book else None,
            "usdt_rate": rate, "usdt_rate_source": source,
            "time_limit_s": self.cfg["limits"]["max_hold_s"],
        })

    def apply_hold_or_cut(self, market: str, decision: str, reason: str, now: float) -> None:
        """Applies a hold-or-cut answer. "cut" sells the position at once."""
        slot = self.slots.get(market)
        if slot is None or not slot.has_position():
            return
        if decision == "cut":
            slot.cut_requested = True
        self.store.add_log(ts=now, kind="ai", text=f"[{self.mode}] {market}: {decision}. {reason}",
                           fields={"mode": self.mode, "market": market,
                                   "decision": decision, "job": "hold_or_cut"})

    def set_control(self, *, paused: bool, flatten: bool, now: float) -> None:
        """Applies the controls of a person and logs each change.

        Args:
            paused: True stops new bids and cancels the live bids. Open
                positions keep their exits.
            flatten: True also sells all positions at once with taker orders.
            now: The time of the change.
        """
        changes = []
        if paused != self.paused:
            changes.append("bids paused" if paused else "bids resumed")
        if flatten != self.force_flatten:
            changes.append("sell all coins now" if flatten else "sell all cancelled")
        self.paused, self.force_flatten = paused, flatten
        for change in changes:
            self.store.add_log(ts=now, kind="control", text=f"[{self.mode}] Person: {change}",
                               fields={"mode": self.mode, "paused": paused, "flatten": flatten})
            log.warning("[%s] control: %s", self.mode, change)

    # Snapshot --------------------------------------------------------------

    def snapshot(self, now: float) -> dict:
        """Returns the state that the supervisor sends to the model."""
        rate, source = self.market.usdt_rate(now)
        markets = {}
        for market in self.market.trade_markets:
            markets[market] = {
                "spread_pct": _round(self.market.spread_pct(market), 3),
                "fv": _round(self.market.fair_value(market, now), 8),
            }
        recent = self.trades[-10:]
        nets = [t["net_pct"] for t in self.trades]
        return {
            "time_utc": now, "mode": self.mode,
            "equity_tmn": round(self.equity(now)),
            "start_equity_tmn": self.risk.start_equity,
            "realized_tmn": round(self.realized_tmn),
            "halted": self.risk.halted(now),
            "usdt_rate": _round(rate, 2), "usdt_rate_source": source,
            "usdttmn_frozen": self.market.usdttmn_frozen(now),
            "filters_active": [f"{name}:{key}" for name, key in self.filters.active()],
            "slots": {market: {"state": slot.state, "qty": slot.qty,
                               "entry_price": slot.entry_price(),
                               "bid_price": slot.bid_price,
                               "exit_price": slot.exit_price}
                      for market, slot in self.slots.items()},
            "plan_in_use": self.plan.model_dump() if self.plan else None,
            "trades_total": len(self.trades),
            "median_net_pct": _round(statistics.median(nets), 3) if nets else None,
            "recent_trades": [{key: trade[key] for key in
                               ("market", "net_pct", "net_tmn", "hold_s", "exit_reason")}
                              for trade in recent],
            "markets": markets,
            "limits": self.cfg["limits"],
        }

    def status(self, now: float) -> str:
        """Returns one status line."""
        states = " ".join(f"{m}:{s.state}" for m, s in sorted(self.slots.items()))
        return (f"[{self.mode}] equity {self.equity(now):,.0f} trades {len(self.trades)} "
                f"net {self.realized_tmn:+,.0f} | {states or 'no slots'}"
                f" | budget refused {self.budget.refused}")


def _round(value: float | None, digits: int) -> float | None:
    """Rounds a value that can be None."""
    return None if value is None else round(value, digits)
