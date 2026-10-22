"""Tests for the AI providers: the fake provider and the Claude adapter.

The Claude tests use a fake client. They make no network call.
"""

import types

import anthropic
import pytest

from sweepbot.ai import claude
from sweepbot.ai import provider
from sweepbot.ai import schema

AI_CFG = {"model": "claude-opus-5-5", "effort": "high", "timeout_s": 60.0,
          "web_search": True}


def usage(**kwargs) -> types.SimpleNamespace:
    """Builds a usage object. Missing token fields are 0."""
    base = {"input_tokens": 0, "output_tokens": 0,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    base.update(kwargs)
    return types.SimpleNamespace(**base)


class FakeClient:
    """Fake Anthropic client. It records the kwargs of each call."""

    def __init__(self, parse_responses=(), create_responses=(), error=None):
        self.parse_calls: list[dict] = []
        self.create_calls: list[dict] = []
        self._parse = list(parse_responses)
        self._create = list(create_responses)
        self._error = error
        messages = types.SimpleNamespace(parse=self._do_parse,
                                         create=self._do_create)
        self.beta = types.SimpleNamespace(messages=messages)

    def _do_parse(self, **kwargs):
        self.parse_calls.append(kwargs)
        if self._error:
            raise self._error
        return self._parse.pop(0)

    def _do_create(self, **kwargs):
        self.create_calls.append(kwargs)
        if self._error:
            raise self._error
        return self._create.pop(0)


def parsed_response(value, stop_reason="end_turn", **usage_kwargs):
    """Builds a fake parse response."""
    return types.SimpleNamespace(
        parsed_output=value, stop_reason=stop_reason, model="claude-opus-5-5",
        usage=usage(**usage_kwargs))


def text_response(text, stop_reason="end_turn", searches=0, **usage_kwargs):
    """Builds a fake create response with one text block."""
    server = types.SimpleNamespace(web_search_requests=searches)
    return types.SimpleNamespace(
        content=[types.SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason, model="claude-opus-5-5",
        usage=usage(server_tool_use=server, **usage_kwargs))


def test_propose_plan_returns_parsed_plan_and_cost() -> None:
    """One plan call returns the Plan, the token counts and the cost.

    Scenario: 1000 input, 200 output, 500 cache write and 2000 cache read
    tokens at 4 / 20 USD per million.
    """
    # given
    plan = schema.Plan(slots=[])
    client = FakeClient([parsed_response(
        plan, input_tokens=1000, output_tokens=200,
        cache_creation_input_tokens=500, cache_read_input_tokens=2000)])
    ai = claude.ClaudeProvider(AI_CFG, client=client)

    # when
    result = ai.propose_plan({"equity": 1})

    # then
    expected = (1000 * 4 + 200 * 20 + 500 * 4 * 1.25 + 2000 * 4 * 0.1) / 1e6
    assert result.value == plan
    assert result.input_tokens == 3500
    assert result.output_tokens == 200
    assert result.cost_usd == pytest.approx(expected)
    assert result.raw_json == plan.model_dump_json()


def test_call_kwargs_hold_model_schema_effort_and_fallbacks() -> None:
    """The parse call sends the model, the schema class, effort and fallbacks."""
    # given
    client = FakeClient([parsed_response(schema.HoldOrCut(decision="hold", reason="x"))])
    ai = claude.ClaudeProvider(AI_CFG, client=client)

    # when
    ai.hold_or_cut({"entry": 1})

    # then
    kwargs = client.parse_calls[0]
    assert kwargs["model"] == "claude-opus-5-5"
    assert kwargs["output_format"] is schema.HoldOrCut
    assert kwargs["output_config"] == {"effort": "high"}
    assert kwargs["betas"] == ["server-side-fallback-2026-07-01"]
    assert kwargs["fallbacks"] == "default"
    assert kwargs["max_tokens"] == 16000
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert '"entry": 1' in kwargs["messages"][0]["content"]


def test_fallbacks_off_removes_betas_and_fallbacks() -> None:
    """With `fallbacks` false, the call has no betas and no fallbacks."""
    # given
    client = FakeClient([parsed_response(schema.TradeLog(text="t", lesson="l"))])
    ai = claude.ClaudeProvider({**AI_CFG, "fallbacks": False}, client=client)

    # when
    ai.write_trade_log({"id": 1})

    # then
    assert "betas" not in client.parse_calls[0]
    assert "fallbacks" not in client.parse_calls[0]


def test_refusal_raises_provider_error() -> None:
    """A response with stop reason "refusal" raises ProviderError."""
    # given
    client = FakeClient([parsed_response(None, stop_reason="refusal")])
    ai = claude.ClaudeProvider(AI_CFG, client=client)

    # when / then
    with pytest.raises(provider.ProviderError, match="refusal"):
        ai.propose_plan({})


def test_missing_parsed_output_raises_provider_error() -> None:
    """A response without parsed output raises ProviderError."""
    # given
    client = FakeClient([parsed_response(None)])
    ai = claude.ClaudeProvider(AI_CFG, client=client)

    # when / then
    with pytest.raises(provider.ProviderError, match="no parsed output"):
        ai.daily_review({})


def test_sdk_timeout_raises_provider_error() -> None:
    """An SDK timeout becomes a ProviderError."""
    # given
    error = anthropic.APITimeoutError.__new__(anthropic.APITimeoutError)
    client = FakeClient(error=error)
    ai = claude.ClaudeProvider(AI_CFG, client=client)

    # when / then
    with pytest.raises(provider.ProviderError):
        ai.propose_plan({})


def test_news_joins_text_and_reads_high_risk() -> None:
    """News joins the text blocks, drops the RISK line and counts searches."""
    # given
    response = text_response("Line one.\nLine two.\nRISK: HIGH", searches=2,
                             input_tokens=1000, output_tokens=100)
    client = FakeClient(create_responses=[response])
    ai = claude.ClaudeProvider(AI_CFG, client=client)

    # when
    result = ai.news()

    # then
    assert result.value == schema.NewsSummary(summary="Line one.\nLine two.",
                                              risk="high")
    assert result.cost_usd == pytest.approx(
        (1000 * 4 + 100 * 20) / 1e6 + 2 * claude.WEB_SEARCH_USD)
    kwargs = client.create_calls[0]
    assert kwargs["tools"][0]["type"] == "web_search_20260209"
    assert "output_format" not in kwargs
    assert kwargs["betas"] == ["server-side-fallback-2026-07-01"]


def test_news_without_risk_line_is_normal() -> None:
    """A news answer that ends with RISK: NORMAL gives risk normal."""
    # given
    client = FakeClient(create_responses=[text_response("Calm.\nRISK: NORMAL")])
    ai = claude.ClaudeProvider(AI_CFG, client=client)

    # when
    result = ai.news()

    # then
    assert result.value.risk == "normal"
    assert result.value.summary == "Calm."


def test_news_with_web_search_off_makes_no_call() -> None:
    """With `web_search` false, news returns a fixed summary at zero cost."""
    # given
    client = FakeClient()
    ai = claude.ClaudeProvider({**AI_CFG, "web_search": False}, client=client)

    # when
    result = ai.news()

    # then
    assert result.value == schema.NewsSummary(summary="web search is off",
                                              risk="normal")
    assert result.cost_usd == 0.0
    assert client.create_calls == []


def test_news_resumes_after_pause_turn() -> None:
    """On "pause_turn" the provider sends the assistant content again."""
    # given
    client = FakeClient(create_responses=[
        text_response("Part one.", stop_reason="pause_turn", searches=1,
                      input_tokens=100),
        text_response("Part two.\nRISK: NORMAL", input_tokens=100)])
    ai = claude.ClaudeProvider(AI_CFG, client=client)

    # when
    result = ai.news()

    # then
    assert len(client.create_calls) == 2
    second = client.create_calls[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant"]
    assert result.value.summary == "Part one.\nPart two."
    assert result.input_tokens == 200
    assert result.cost_usd >= claude.WEB_SEARCH_USD


def test_fake_provider_consumes_lists_in_order_and_records_calls() -> None:
    """Scripted answers come in order. Then the defaults come."""
    # given
    first = schema.Plan(slots=[], pause=True)
    fake = provider.FakeProvider(plans=[first])

    # when
    a = fake.propose_plan({"n": 1})
    b = fake.propose_plan({"n": 2})
    c = fake.hold_or_cut({"p": 1})

    # then
    assert a.value == first
    assert b.value == schema.Plan(slots=[])
    assert c.value.decision == "hold"
    assert a.cost_usd == 0.0
    assert fake.calls == [("propose_plan", {"n": 1}), ("propose_plan", {"n": 2}),
                          ("hold_or_cut", {"p": 1})]
    assert (fake.name, fake.model) == ("fake", "fake")


def test_fake_provider_raises_the_error() -> None:
    """With `error` set, every call raises it."""
    # given
    fake = provider.FakeProvider(error=provider.ProviderError("down"))

    # when / then
    with pytest.raises(provider.ProviderError, match="down"):
        fake.news()
