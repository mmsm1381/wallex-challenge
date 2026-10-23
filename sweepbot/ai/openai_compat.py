"""Adapter for an OpenAI-compatible model router, such as 9router.

`OpenAICompatProvider` sends `POST {base_url}/chat/completions` with the
standard library. The answer must be one JSON object. The adapter puts the
JSON schema of the job into the prompt, parses the answer with pydantic,
and asks once more when the JSON is invalid.

Settings (`ai` section of the config):

* `provider = "openai_compat"`
* `base_url`: the router address, such as "http://127.0.0.1:20128/v1".
* `model`: the model name of the router.
* `fallback_models`: models to try in order when the main model fails
  (an HTTP error, a timeout, or no valid JSON after the retry).
* `request_timeout_s`: the timeout of one HTTP request. Keep the sum over
  all models below `timeout_s` of the supervisor.
* `api_key_env`: the environment variable with the key (default
  "AI_API_KEY"). The key never goes to a log.
* `json_mode`: true sends `response_format: {"type": "json_object"}`.
  Turn it off when the router or the model rejects it.
* `price_in_per_mtok`, `price_out_per_mtok`: USD per million tokens for
  the cost in `ai_calls` and the AI budget. 0 when the router does not bill.

The router has no web search tool, so `news` returns a normal-risk summary
that says so.
"""

import json
import os
import re
import urllib.error
import urllib.request

import pydantic

from sweepbot.ai import provider
from sweepbot.ai import schema

SYSTEM_PROMPT = """\
You are the supervisor of a small trading bot on the Wallex exchange.

How the bot works:
- It watches 18 Wallex markets and rests deep buy orders (bids) below the
  fair value of a coin. Fair value comes from global prices and the USDT rate.
- The bot itself chooses the markets for its bids: up to 6 toman places and
  4 USDT places, by live conditions and by a ranking from a 60-day backtest
  (the market with more fills first).
- When a big sell sweep hits the book, a bid fills at a low price. The bot
  then sells at fair value.

What your plan controls:
- pause: true stops all new bids (for example on a real risk).
- fx_risk: "high" stops bids on toman markets (a toman or USDT shock).
- slots: optional depth changes. Each slot names a market and the depth of
  its bid below fair value. Markets not in the list use the default depth
  (1.0% on all markets). size_tmn is 475000.

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

Hard limits. A validator enforces them and cuts any value outside them:
- Bid depth below fair value: 1.0% to 2.5%.
- At most 5 slots. At most 1,000,000 toman per bid.

Rules for your answers:
- Use only the data you receive. Do not invent numbers.
- Write short and factual reasoning. A person reads it in a decision log.
- Use plain simple English.
- Answer with one JSON object only, no other text.
"""

PLAN_INSTRUCTION = (
    "Propose the plan for the next 30 minutes from this state snapshot. "
    "Keep pause false, fx_risk normal and an empty slot list unless the data "
    "gives a clear reason.")
HOLD_OR_CUT_INSTRUCTION = (
    "This position has no exit fill after 5 minutes. Decide hold or cut. "
    "When adopted is true, the bot bought the position before a restart "
    "and asks at once. "
    "Cut when fair value or the global price fell since the entry.")
TRADE_LOG_INSTRUCTION = (
    "Write the decision log entry of this closed trade in 2 to 4 sentences, "
    "and one lesson.")
REVIEW_INSTRUCTION = (
    "Review this day of the bot and the no-AI twin. "
    "Propose changes inside the hard limits.")

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def _first_json_object(text: str) -> str:
    """Returns the JSON object in `text`, without code fences or other text."""
    text = _FENCE.sub("", text.strip())
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start >= 0 and end > start else text


class OpenAICompatProvider:
    """`ModelProvider` on an OpenAI-compatible chat completions endpoint."""

    name = "openai_compat"

    def __init__(self, ai_cfg: dict, transport=None) -> None:
        """Reads the settings.

        Args:
            ai_cfg: The `ai` section of the config.
            transport: transport(url, headers, body, timeout) -> dict. Tests
                pass a fake. None sends real HTTP requests.

        Raises:
            ValueError: `base_url` or `model` is missing.
            RuntimeError: The key variable is not set.
        """
        self._cfg = ai_cfg
        self.model = ai_cfg.get("model") or ""
        self._url = (ai_cfg.get("base_url") or "").rstrip("/") + "/chat/completions"
        if not ai_cfg.get("base_url") or not self.model:
            raise ValueError("ai.base_url and ai.model must be set")
        key_env = ai_cfg.get("api_key_env") or "AI_API_KEY"
        self._key = os.environ.get(key_env)
        if not self._key:
            raise RuntimeError(f"{key_env} is not set")
        self._send = transport or self._http
        # The model that gave the last answer (the main model or a fallback).
        self.last_model = self.model

    def _http(self, url: str, headers: dict, body: dict, timeout: float) -> dict:
        """Sends one request. Errors become `ProviderError` without the key."""
        request = urllib.request.Request(url, data=json.dumps(body).encode(),
                                         headers=headers, method="POST")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            detail = error.read()[:300].decode(errors="replace")
            raise provider.ProviderError(f"HTTP {error.code}: {detail}") from None
        except (OSError, ValueError) as error:
            raise provider.ProviderError(f"{type(error).__name__}: {error}") from None

    def _ask(self, instruction: str, data, output: type[pydantic.BaseModel]):
        """Asks the main model, then each fallback model, until one answers."""
        errors = []
        for model in [self.model] + list(self._cfg.get("fallback_models") or []):
            try:
                result = self._ask_model(model, instruction, data, output)
            except provider.ProviderError as error:
                errors.append(f"{model}: {error}")
                continue
            self.last_model = model
            return result
        raise provider.ProviderError("all models failed: " + " | ".join(errors)[:1000])

    def _ask_model(self, model: str, instruction: str, data,
                   output: type[pydantic.BaseModel]):
        """Sends one job to `model` and returns its typed result. Retries once on bad JSON."""
        user = (instruction + "\n\nAnswer with one JSON object that follows this "
                "JSON schema:\n" + json.dumps(output.model_json_schema())
                + "\n\nData:\n" + json.dumps(data, sort_keys=True, default=str))
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user}]
        headers = {"Authorization": f"Bearer {self._key}",
                   "Content-Type": "application/json"}
        tokens_in = tokens_out = 0
        for attempt in range(2):
            # Some routers stream by default. The adapter reads one JSON body.
            body = {"model": model, "messages": messages, "stream": False,
                    "max_tokens": int(self._cfg.get("max_tokens", 4000))}
            if self._cfg.get("json_mode", True):
                body["response_format"] = {"type": "json_object"}
            timeout = float(self._cfg.get("request_timeout_s") or self._cfg["timeout_s"])
            answer = self._send(self._url, headers, body, timeout)
            usage = answer.get("usage") or {}
            tokens_in += int(usage.get("prompt_tokens") or 0)
            tokens_out += int(usage.get("completion_tokens") or 0)
            try:
                text = answer["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, TypeError):
                raise provider.ProviderError("no message in the answer") from None
            try:
                value = output.model_validate_json(_first_json_object(text))
            except pydantic.ValidationError as error:
                if attempt == 1:
                    raise provider.ProviderError(f"invalid JSON answer: {error}") from None
                messages = messages + [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": "That JSON is not valid for the schema: "
                     f"{str(error)[:500]}. Answer again with one valid JSON object only."}]
                continue
            cost = (tokens_in * float(self._cfg.get("price_in_per_mtok", 0.0))
                    + tokens_out * float(self._cfg.get("price_out_per_mtok", 0.0))) / 1e6
            return provider.ProviderResult(value, tokens_in, tokens_out, cost,
                                           value.model_dump_json())
        raise provider.ProviderError("no valid answer")

    def propose_plan(self, snapshot: dict) -> provider.ProviderResult:
        """Asks for a `schema.Plan`."""
        return self._ask(PLAN_INSTRUCTION, snapshot, schema.Plan)

    def hold_or_cut(self, context: dict) -> provider.ProviderResult:
        """Asks for a `schema.HoldOrCut`."""
        return self._ask(HOLD_OR_CUT_INSTRUCTION, context, schema.HoldOrCut)

    def write_trade_log(self, trade: dict) -> provider.ProviderResult:
        """Asks for a `schema.TradeLog`."""
        return self._ask(TRADE_LOG_INSTRUCTION, trade, schema.TradeLog)

    def daily_review(self, day: dict) -> provider.ProviderResult:
        """Asks for a `schema.Review`."""
        return self._ask(REVIEW_INSTRUCTION, day, schema.Review)

    def news(self) -> provider.ProviderResult:
        """Returns a normal-risk summary: the router has no web search."""
        value = schema.NewsSummary(summary="no web search on this provider", risk="normal")
        return provider.ProviderResult(value, 0, 0, 0.0, value.model_dump_json())
