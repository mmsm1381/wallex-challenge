"""Claude adapter of the model provider (DESIGN section 8.1).

`ClaudeProvider` uses the `anthropic` SDK. The four typed jobs use
structured output (`beta.messages.parse`). The news job uses the web search
tool. The key comes from the environment variable ANTHROPIC_API_KEY, which
the SDK reads. This module never logs or prints a key.
"""

import json
import re

import anthropic

from sweepbot.ai import provider
from sweepbot.ai import schema

BETAS = ["server-side-fallback-2026-07-01"]
MAX_TOKENS = 16000
MAX_PAUSE_TURNS = 3

# (input, output) USD per million tokens.
PRICES_PER_MTOK = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}
WEB_SEARCH_USD = 0.01

SYSTEM_PROMPT = """\
You are the supervisor of a small trading bot on the Wallex exchange.

How the bot works:
- It rests deep buy orders (bids) below the fair value of a coin.
- When a big sell sweep hits the book, a bid fills at a low price.
- The bot then sells at fair value.
- Fair value comes from global prices and the USDT rate in toman.

Hard limits. A validator enforces them and cuts any value outside them:
- Bid depth below fair value: 1.0% to 2.5%.
- At most 5 slots.
- At most 1,000,000 toman per bid.
- The USDT share of the equity is at most 40%.

USDT rate and fx_risk:
- Wallex USDTTMN closes every night from 20:00 to 08:00 UTC (23:30 to 11:30
  Tehran), and on some days it opens earlier. While it is closed,
  usdttmn_frozen is true and usdt_rate_source is "implied": the bot then uses
  the USDT rate of the coin markets. This is normal. It is NOT a reason for
  fx_risk "high".
- The bot has its own fast USDT shock filter: it stops toman bids within a
  second when the USDT rate moves 0.5% in 5 minutes. Set fx_risk "high" only
  for a real toman or USDT shock that this filter does not cover, such as
  clear news. Do not switch fx_risk back and forth without a new reason.

Jobs:
- plan: choose the markets, the depth and the size for the next 30 minutes.
- hold_or_cut: decide for a position that has no exit fill.
- trade log: explain one closed trade and give one lesson.
- review: review a day of the bot and the twin that has no AI.

Rules for your answers:
- Use only the data you receive. Do not invent numbers.
- Write short and factual reasoning. A person reads it in a decision log.
- Use plain simple English.
"""

PLAN_INSTRUCTION = (
    "Propose the plan for the next 30 minutes from this state snapshot. "
    "Keep the default plan unless the data gives a reason. "
    "Pause or set fx_risk high on a real risk.")
HOLD_OR_CUT_INSTRUCTION = (
    "This position has no exit fill after 5 minutes. Decide hold or cut. "
    "When adopted is true, the bot bought the position before a restart "
    "and asks at once. "
    "Cut when fair value or the global price fell since the entry, "
    "or on news risk.")
TRADE_LOG_INSTRUCTION = (
    "Write the decision log entry of this closed trade in 2 to 4 sentences, "
    "and one lesson.")
REVIEW_INSTRUCTION = (
    "Review this day of the bot and the no-AI twin. "
    "Propose changes inside the hard limits.")
NEWS_INSTRUCTION = (
    "Search the web for news of the last 12 hours that can move crypto "
    "prices or the Iranian toman (USDT/IRT). Summarize it in a few short "
    "lines. End the answer with one line: \"RISK: HIGH\" or \"RISK: NORMAL\".")

_RISK_RE = re.compile(r"^\s*RISK:\s*(HIGH|NORMAL)\s*$", re.IGNORECASE)


def _tokens(usage, name: str) -> int:
    return getattr(usage, name, 0) or 0


def _usage_cost(model: str, usage) -> tuple[int, int, float]:
    """Returns (input tokens with cache, output tokens, cost in USD)."""
    price_in, price_out = PRICES_PER_MTOK.get(model, (0.0, 0.0))
    plain = _tokens(usage, "input_tokens")
    out = _tokens(usage, "output_tokens")
    created = _tokens(usage, "cache_creation_input_tokens")
    read = _tokens(usage, "cache_read_input_tokens")
    server = getattr(usage, "server_tool_use", None)
    searches = getattr(server, "web_search_requests", 0) or 0
    cost = (plain * price_in / 1e6 + out * price_out / 1e6
            + created * price_in * 1.25 / 1e6 + read * price_in * 0.1 / 1e6
            + searches * WEB_SEARCH_USD)
    return plain + created + read, out, cost


class ClaudeProvider:
    """Model provider that calls the Claude API."""

    name = "claude"

    def __init__(self, ai_cfg: dict, client=None) -> None:
        """Stores the config and builds the client when none is given.

        Args:
            ai_cfg: The `ai` section of the config.
            client: An Anthropic client. Tests pass a fake one.
        """
        self._cfg = ai_cfg
        self.model = ai_cfg["model"]
        if client is None:
            client = anthropic.Anthropic(timeout=ai_cfg["timeout_s"],
                                         max_retries=1)
        self._client = client

    def _extra(self) -> dict:
        if self._cfg.get("fallbacks", True):
            return {"betas": list(BETAS), "fallbacks": "default"}
        return {}

    def _model_of(self, response) -> str:
        model = getattr(response, "model", None)
        return model if model in PRICES_PER_MTOK else self.model

    def _parse(self, instruction: str, data: dict, output_format):
        content = instruction + "\n\n" + json.dumps(data, sort_keys=True,
                                                     default=str)
        try:
            response = self._client.beta.messages.parse(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=[{"type": "text", "text": SYSTEM_PROMPT,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": content}],
                output_format=output_format,
                output_config={"effort": self._cfg["effort"]},
                **self._extra())
        except (anthropic.APITimeoutError, anthropic.APIStatusError,
                anthropic.APIConnectionError) as exc:
            raise provider.ProviderError(
                f"{type(exc).__name__}: {exc}") from exc
        if response.stop_reason == "refusal":
            raise provider.ProviderError("refusal from the model")
        if response.parsed_output is None:
            raise provider.ProviderError("no parsed output")
        value = response.parsed_output
        tokens_in, tokens_out, cost = _usage_cost(self._model_of(response),
                                                  response.usage)
        return provider.ProviderResult(value, tokens_in, tokens_out, cost,
                                       value.model_dump_json())

    def propose_plan(self, snapshot: dict) -> provider.ProviderResult:
        """Asks for a `schema.Plan`."""
        return self._parse(PLAN_INSTRUCTION, snapshot, schema.Plan)

    def hold_or_cut(self, context: dict) -> provider.ProviderResult:
        """Asks for a `schema.HoldOrCut`."""
        return self._parse(HOLD_OR_CUT_INSTRUCTION, context, schema.HoldOrCut)

    def write_trade_log(self, trade: dict) -> provider.ProviderResult:
        """Asks for a `schema.TradeLog`."""
        return self._parse(TRADE_LOG_INSTRUCTION, trade, schema.TradeLog)

    def daily_review(self, day: dict) -> provider.ProviderResult:
        """Asks for a `schema.Review`."""
        return self._parse(REVIEW_INSTRUCTION, day, schema.Review)

    def news(self) -> provider.ProviderResult:
        """Searches the web and returns a `schema.NewsSummary`."""
        if not self._cfg.get("web_search", True):
            value = schema.NewsSummary(summary="web search is off",
                                       risk="normal")
            return provider.ProviderResult(value, 0, 0, 0.0,
                                           value.model_dump_json())
        messages = [{"role": "user", "content": NEWS_INSTRUCTION}]
        texts: list[str] = []
        total_in = total_out = 0
        total_cost = 0.0
        for _ in range(MAX_PAUSE_TURNS + 1):
            try:
                response = self._client.beta.messages.create(
                    model=self.model,
                    max_tokens=MAX_TOKENS,
                    system=[{"type": "text", "text": SYSTEM_PROMPT,
                             "cache_control": {"type": "ephemeral"}}],
                    messages=list(messages),
                    tools=[{"type": "web_search_20260209",
                            "name": "web_search", "max_uses": 3}],
                    output_config={"effort": self._cfg["effort"]},
                    **self._extra())
            except (anthropic.APITimeoutError, anthropic.APIStatusError,
                    anthropic.APIConnectionError) as exc:
                raise provider.ProviderError(
                    f"{type(exc).__name__}: {exc}") from exc
            if response.stop_reason == "refusal":
                raise provider.ProviderError("refusal from the model")
            tokens_in, tokens_out, cost = _usage_cost(self._model_of(response),
                                                      response.usage)
            total_in += tokens_in
            total_out += tokens_out
            total_cost += cost
            texts.extend(b.text for b in response.content
                         if getattr(b, "type", None) == "text")
            if response.stop_reason != "pause_turn":
                break
            messages = messages + [{"role": "assistant",
                                    "content": response.content}]
        lines = "\n".join(texts).splitlines()
        risk = "normal"
        kept = []
        for line in lines:
            match = _RISK_RE.match(line)
            if match:
                risk = "high" if match.group(1).upper() == "HIGH" else "normal"
            else:
                kept.append(line)
        value = schema.NewsSummary(summary="\n".join(kept).strip(), risk=risk)
        return provider.ProviderResult(value, total_in, total_out, total_cost,
                                       value.model_dump_json())
