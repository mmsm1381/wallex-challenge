"""Model provider interface of the AI part (DESIGN section 8.1).

A provider answers the jobs of the supervisor with typed results. Every
call returns a `ProviderResult` or raises `ProviderError`. `FakeProvider`
is a scripted provider for tests and for replay.
"""

import dataclasses
import time
from typing import Protocol

import pydantic

from sweepbot.ai import schema


class ProviderError(Exception):
    """A provider call failed: timeout, refusal, invalid output or API error."""


@dataclasses.dataclass
class ProviderResult:
    """The answer of one provider call.

    Attributes:
        value: A `schema.Plan`, `HoldOrCut`, `TradeLog`, `Review` or
            `NewsSummary`.
        input_tokens: All input tokens, with the cache tokens.
        output_tokens: Output tokens.
        cost_usd: Cost of the call in USD.
        raw_json: `value.model_dump_json()` or the raw text.
    """

    value: pydantic.BaseModel
    input_tokens: int
    output_tokens: int
    cost_usd: float
    raw_json: str


class ModelProvider(Protocol):
    """The interface that every model adapter implements."""

    name: str
    model: str

    def propose_plan(self, snapshot: dict) -> ProviderResult:
        """Returns a `schema.Plan` for the next period."""
        ...

    def hold_or_cut(self, context: dict) -> ProviderResult:
        """Returns a `schema.HoldOrCut` for a position without an exit fill."""
        ...

    def write_trade_log(self, trade: dict) -> ProviderResult:
        """Returns a `schema.TradeLog` for a closed trade."""
        ...

    def daily_review(self, day: dict) -> ProviderResult:
        """Returns a `schema.Review` for a day."""
        ...

    def news(self) -> ProviderResult:
        """Returns a `schema.NewsSummary` from web search."""
        ...


class FakeProvider:
    """Scripted provider for tests and for replay.

    Each list is consumed in order. When a list is empty, the method returns
    a simple default. All calls are recorded in `calls`.
    """

    name = "fake"
    model = "fake"

    def __init__(self, *, plans=None, hold_or_cut=None, trade_logs=None,
                 reviews=None, news=None, delay_s: float = 0.0,
                 error: Exception | None = None) -> None:
        """Stores the scripts.

        Args:
            plans: Plans for `propose_plan`.
            hold_or_cut: Answers for `hold_or_cut`.
            trade_logs: Answers for `write_trade_log`.
            reviews: Answers for `daily_review`.
            news: Answers for `news`.
            delay_s: Seconds to sleep before each answer.
            error: When set, every call raises it.
        """
        self._scripts = {
            "propose_plan": list(plans or []),
            "hold_or_cut": list(hold_or_cut or []),
            "write_trade_log": list(trade_logs or []),
            "daily_review": list(reviews or []),
            "news": list(news or []),
        }
        self._delay_s = delay_s
        self._error = error
        self.calls: list[tuple[str, object]] = []

    def _answer(self, job: str, data, default) -> ProviderResult:
        self.calls.append((job, data))
        if self._delay_s:
            time.sleep(self._delay_s)
        if self._error is not None:
            raise self._error
        script = self._scripts[job]
        value = script.pop(0) if script else default
        return ProviderResult(value=value, input_tokens=0, output_tokens=0,
                              cost_usd=0.0, raw_json=value.model_dump_json())

    def propose_plan(self, snapshot: dict) -> ProviderResult:
        """Returns the next scripted plan, or an empty plan."""
        return self._answer("propose_plan", snapshot, schema.Plan(slots=[]))

    def hold_or_cut(self, context: dict) -> ProviderResult:
        """Returns the next scripted answer, or "hold"."""
        return self._answer("hold_or_cut", context,
                            schema.HoldOrCut(decision="hold", reason="fake"))

    def write_trade_log(self, trade: dict) -> ProviderResult:
        """Returns the next scripted log, or a short default."""
        return self._answer("write_trade_log", trade,
                            schema.TradeLog(text="fake", lesson="fake"))

    def daily_review(self, day: dict) -> ProviderResult:
        """Returns the next scripted review, or a short default."""
        return self._answer(
            "daily_review", day,
            schema.Review(summary="fake", what_worked="", what_failed="",
                          proposed_changes=[]))

    def news(self) -> ProviderResult:
        """Returns the next scripted news, or a normal-risk default."""
        return self._answer("news", None,
                            schema.NewsSummary(summary="fake", risk="normal"))
