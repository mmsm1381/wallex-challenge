"""Supervisor of the AI part (DESIGN sections 8 and 12).

The supervisor owns the active plan and runs every model job. The executor
never waits for the model. `active_plan` and `due` return at once. Each model
call runs in a worker thread with a timeout. The supervisor also enforces the
spending limit: when the budget is used, it makes no call.
"""

import asyncio
import dataclasses
import time
from collections.abc import Callable

from sweepbot.ai import provider as provider_lib
from sweepbot.ai import schema
from sweepbot.ai import validator
from sweepbot.store import Store


@dataclasses.dataclass
class _Outcome:
    """The result of one model call: the answer, or the reason of failure."""

    result: provider_lib.ProviderResult | None
    error: str | None


class Supervisor:
    """Keeps the active plan and runs the model jobs without blocking."""

    def __init__(self, cfg: dict, provider: provider_lib.ModelProvider,
                 store: Store, *, equity_fn: Callable[[float], float]) -> None:
        """Stores the settings and the default plan.

        Args:
            cfg: The config with the `ai` and `challenge` sections.
            provider: The model adapter.
            store: The store for plans, calls and logs.
            equity_fn: Returns the equity in toman for a time.
        """
        ai = cfg["ai"]
        self._cfg = cfg
        self._provider = provider
        self._store = store
        self._equity_fn = equity_fn
        self._timeout_s = ai["timeout_s"]
        self._every_s = ai["supervisor_every_s"]
        self._max_failures = ai["max_failures"]
        self._budget_usd = ai["budget_usd"]
        self._news_every_s = ai["news_every_s"]

        default = validator.validate_plan(
            validator.default_plan(cfg), cfg=cfg,
            equity_tmn=cfg["challenge"]["start_equity_tmn"])
        self._default_plan = default.plan
        self._default_id = store.add_plan(
            ts=time.time(), provider="config", model="default",
            raw_json=default.plan.model_dump_json(),
            validated_json=default.plan.model_dump_json(), accepted=True,
            notes="; ".join(default.notes))

        self._plan: schema.Plan | None = None
        self._plan_id: int | None = None
        self._plan_ts = 0.0
        self._failures = 0
        self._last_start: float | None = None
        self._running = False
        self._alerts: list[str] = []
        self._budget_logged = False
        self._news: schema.NewsSummary | None = None
        self._news_ts: float | None = None

    def active_plan(self, now: float) -> tuple[schema.Plan, int | None]:
        """Returns the plan to trade and its id. Never blocks.

        Args:
            now: The current time in seconds.

        Returns:
            The last accepted model plan while it is valid and the failures in
            a row are below the limit. Otherwise the default plan.
        """
        if (self._plan is not None and self._failures < self._max_failures
                and now < self._plan_ts + self._plan.valid_minutes * 60):
            return self._plan, self._plan_id
        return self._default_plan, self._default_id

    def due(self, now: float) -> bool:
        """Returns True when the executor should start a plan call."""
        if self._running:
            return False
        if self._last_start is None or self._alerts:
            return True
        return now >= self._last_start + self._every_s

    def alert(self, reason: str, now: float) -> None:
        """Remembers a reason. The next plan call sends it to the model."""
        self._alerts.append(reason)

    def status(self) -> dict:
        """Returns the state of the supervisor for the panel."""
        return {"failures": self._failures, "running": self._running,
                "news": self._news.summary if self._news else None,
                "budget_usd": self._budget_usd}

    def budget_left(self) -> float:
        """Returns the budget in USD that is not spent."""
        return self._budget_usd - self._store.ai_cost_total()

    def _budget_reached(self, now: float) -> bool:
        """Returns True when no budget is left. Logs this once."""
        if self.budget_left() > 0:
            return False
        if not self._budget_logged:
            self._budget_logged = True
            self._store.add_log(
                ts=now, kind="ai", fields={"budget_usd": self._budget_usd},
                text=f"AI budget reached ({self._budget_usd} USD). "
                     "No more model calls.")
        return True

    async def _call(self, job: str, now: float, func, *args) -> _Outcome:
        """Runs one provider call in a thread and records it in `ai_calls`.

        On a timeout, `asyncio.wait_for` stops the wait but not the thread.
        The thread ends when the provider returns. Its answer is dropped.
        """
        start = time.monotonic()
        result = None
        error = None
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(func, *args), timeout=self._timeout_s)
        except asyncio.TimeoutError:
            error = f"timeout after {self._timeout_s} s"
        except provider_lib.ProviderError as exc:
            error = f"provider error: {exc}"
        latency = time.monotonic() - start
        self._store.add_ai_call(
            ts=now, job=job, model=self._provider.model,
            input_tokens=result.input_tokens if result else 0,
            output_tokens=result.output_tokens if result else 0,
            cost_usd=result.cost_usd if result else 0.0,
            latency_s=latency, error=error)
        return _Outcome(result, error)

    def _log_error(self, job: str, error: str, now: float) -> None:
        """Adds a log row of kind "ai" for a failed job."""
        self._store.add_log(ts=now, kind="ai", text=f"{job} failed: {error}",
                            fields={"job": job})

    async def run_plan(self, now: float, snapshot: dict) -> None:
        """Runs one supervisor call and updates the active plan.

        Args:
            now: The current time in seconds.
            snapshot: The state snapshot for the model. The call adds the
                pending alerts and the last news.
        """
        if self._budget_reached(now):
            return
        sent = len(self._alerts)
        data = dict(snapshot)
        data["alerts"] = list(self._alerts)
        data["news"] = self._news.model_dump() if self._news else None
        self._running = True
        self._last_start = now
        try:
            outcome = await self._call(
                "plan", now, self._provider.propose_plan, data)
        finally:
            self._running = False
            self._alerts = self._alerts[sent:]

        if outcome.result is None:
            self._failures += 1
            self._log_error("plan", outcome.error, now)
            return
        result = outcome.result
        checked = validator.validate_plan(
            result.value, cfg=self._cfg, equity_tmn=self._equity_fn(now))
        notes = "; ".join(checked.notes)
        plan_id = self._store.add_plan(
            ts=now, provider=self._provider.name, model=self._provider.model,
            raw_json=result.raw_json,
            validated_json=(checked.plan.model_dump_json()
                            if checked.accepted else None),
            accepted=checked.accepted, notes=notes)
        if not checked.accepted:
            self._failures += 1
            self._log_error("plan", f"rejected: {notes}", now)
            return
        self._plan, self._plan_id, self._plan_ts = checked.plan, plan_id, now
        self._failures = 0
        text = f"Plan {plan_id}: {checked.plan.reasoning}"
        if notes:
            text += f" ({notes})"
        self._store.add_log(ts=now, kind="plan", text=text,
                            fields=checked.plan.model_dump())

    async def hold_or_cut(self, context: dict, now: float) -> tuple[str, str]:
        """Asks the model to hold or cut a position.

        Returns:
            (decision, reason). On a spent budget, a timeout or an error, the
            answer is ("hold", "no answer: <why>").
        """
        if self._budget_reached(now):
            return "hold", "no answer: budget reached"
        outcome = await self._call("hold_or_cut", now,
                                   self._provider.hold_or_cut, context)
        if outcome.result is None:
            return "hold", f"no answer: {outcome.error}"
        value = outcome.result.value
        return value.decision, value.reason

    async def trade_log(self, trade: dict, now: float) -> None:
        """Writes the model log of a closed trade."""
        if self._budget_reached(now):
            return
        outcome = await self._call("trade_log", now,
                                   self._provider.write_trade_log, trade)
        if outcome.result is None:
            self._log_error("trade_log", outcome.error, now)
            return
        value = outcome.result.value
        self._store.add_log(
            ts=now, kind="trade", text=value.text,
            fields={"source": "ai", "lesson": value.lesson,
                    "trade_id": trade.get("id")})

    async def daily_review(self, day: dict, now: float) -> None:
        """Writes the model review of a day."""
        if self._budget_reached(now):
            return
        outcome = await self._call("review", now,
                                   self._provider.daily_review, day)
        if outcome.result is None:
            self._log_error("review", outcome.error, now)
            return
        value = outcome.result.value
        parts = [value.summary, f"Worked: {value.what_worked}",
                 f"Failed: {value.what_failed}"]
        if value.proposed_changes:
            parts.append("Changes: " + "; ".join(value.proposed_changes))
        self._store.add_log(ts=now, kind="review", text=" ".join(parts),
                            fields=value.model_dump())

    async def refresh_news(self, now: float) -> None:
        """Reads the news at most once per `news_every_s`.

        A news risk "high" raises an alert for the next plan call.
        """
        if (self._news_ts is not None
                and now < self._news_ts + self._news_every_s):
            return
        if self._budget_reached(now):
            return
        self._news_ts = now
        outcome = await self._call("news", now, self._provider.news)
        if outcome.result is None:
            self._log_error("news", outcome.error, now)
            return
        self._news = outcome.result.value
        if self._news.risk == "high":
            self.alert("news risk high", now)
