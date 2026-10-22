"""Tests for the OpenAI-compatible router adapter, with a fake transport."""

import json

import pytest

from sweepbot.ai import openai_compat
from sweepbot.ai import provider
from sweepbot.ai import schema

CFG = {"provider": "openai_compat", "base_url": "http://router.test/v1/",
       "model": "router-model", "timeout_s": 30.0, "json_mode": True,
       "price_in_per_mtok": 1.0, "price_out_per_mtok": 4.0}


class FakeTransport:
    """Returns scripted answer texts and records the requests.

    An exception in the script is raised instead of an answer.
    """

    def __init__(self, *texts) -> None:
        self.texts = list(texts)
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, headers, body, timeout))
        text = self.texts.pop(0)
        if isinstance(text, Exception):
            raise text
        return {"choices": [{"message": {"content": text}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}


def make(*texts, monkeypatch):
    """Builds the adapter with a key in the environment and a fake transport."""
    monkeypatch.setenv("AI_API_KEY", "test-key")
    transport = FakeTransport(*texts)
    return openai_compat.OpenAICompatProvider(dict(CFG), transport=transport), transport


def test_plan_answer_is_parsed_and_the_request_is_openai_shaped(monkeypatch) -> None:
    """A JSON plan in a code fence is parsed. The request has the bearer key.

    Expected: the plan, the token counts, the cost from the configured
    prices, and a POST to {base_url}/chat/completions with JSON mode.
    """
    # given
    answer = "```json\n" + json.dumps({"pause": False, "fx_risk": "normal", "slots": [],
                                       "reasoning": "calm market"}) + "\n```"
    adapter, transport = make(answer, monkeypatch=monkeypatch)

    # when
    result = adapter.propose_plan({"equity_tmn": 5_000_000})

    # then
    assert isinstance(result.value, schema.Plan)
    assert result.value.reasoning == "calm market"
    assert (result.input_tokens, result.output_tokens) == (1000, 100)
    assert result.cost_usd == pytest.approx((1000 * 1.0 + 100 * 4.0) / 1e6)
    url, headers, body, _ = transport.calls[0]
    assert url == "http://router.test/v1/chat/completions"
    assert headers["Authorization"] == "Bearer test-key"
    assert body["model"] == "router-model"
    assert body["stream"] is False
    assert body["response_format"] == {"type": "json_object"}


def test_invalid_json_is_asked_again_once(monkeypatch) -> None:
    """The first answer breaks the schema, the second one is valid."""
    # given
    good = json.dumps({"decision": "cut", "reason": "fair value fell"})
    adapter, transport = make('{"decision": "maybe"}', good, monkeypatch=monkeypatch)

    # when
    result = adapter.hold_or_cut({"market": "BTCTMN"})

    # then
    assert result.value.decision == "cut"
    assert len(transport.calls) == 2
    assert "not valid" in transport.calls[1][2]["messages"][-1]["content"]


def test_two_invalid_answers_raise_provider_error(monkeypatch) -> None:
    """The supervisor keeps the last plan when the provider fails."""
    # given
    adapter, _ = make("no json here", "still none", monkeypatch=monkeypatch)

    # when / then
    with pytest.raises(provider.ProviderError):
        adapter.write_trade_log({"market": "BTCTMN"})


def test_missing_key_raises(monkeypatch) -> None:
    """Without the key variable the bot must not start the AI part."""
    # given
    monkeypatch.delenv("AI_API_KEY", raising=False)

    # when / then
    with pytest.raises(RuntimeError):
        openai_compat.OpenAICompatProvider(dict(CFG), transport=FakeTransport())


def test_news_has_no_web_search_and_normal_risk(monkeypatch) -> None:
    """The router has no web search tool."""
    # given
    adapter, transport = make(monkeypatch=monkeypatch)

    # when
    result = adapter.news()

    # then
    assert result.value.risk == "normal"
    assert transport.calls == []


def test_fallback_model_answers_when_the_main_model_fails(monkeypatch) -> None:
    """The main model gives HTTP 503. The first fallback model answers.

    Expected: the answer of the fallback, and `last_model` names it.
    """
    # given
    monkeypatch.setenv("AI_API_KEY", "test-key")
    good = json.dumps({"decision": "hold", "reason": "price is back near fair value"})
    transport = FakeTransport(provider.ProviderError("HTTP 503: busy"), good)
    cfg = dict(CFG, model="cx/main", fallback_models=["gemini/backup"])
    adapter = openai_compat.OpenAICompatProvider(cfg, transport=transport)

    # when
    result = adapter.hold_or_cut({"market": "BTCTMN"})

    # then
    assert result.value.decision == "hold"
    assert [call[2]["model"] for call in transport.calls] == ["cx/main", "gemini/backup"]
    assert adapter.last_model == "gemini/backup"


def test_all_models_failing_raises_one_error_with_each_reason(monkeypatch) -> None:
    """Main and fallback fail: the supervisor gets one error and keeps its plan."""
    # given
    monkeypatch.setenv("AI_API_KEY", "test-key")
    transport = FakeTransport(provider.ProviderError("HTTP 429"),
                              provider.ProviderError("timeout"))
    cfg = dict(CFG, model="cx/main", fallback_models=["gemini/backup"])
    adapter = openai_compat.OpenAICompatProvider(cfg, transport=transport)

    # when / then
    with pytest.raises(provider.ProviderError, match="cx/main: HTTP 429"):
        adapter.propose_plan({})
