"""Tests for the supervisor: plan life cycle, failures, budget and jobs.

All tests use `FakeProvider` and an in-memory store. They make no network call.
"""

import asyncio

from sweepbot import config
from sweepbot import store as store_lib
from sweepbot.ai import provider
from sweepbot.ai import schema
from sweepbot.ai import supervisor

EQUITY = 5_000_000.0


def make_cfg(**ai) -> dict:
    """Returns the default config with `ai` values applied."""
    cfg = config.load_config(None)
    cfg["ai"].update(ai)
    return cfg


def make_supervisor(fake, **ai):
    """Returns (supervisor, store, cfg) built on `fake`."""
    cfg = make_cfg(**ai)
    store = store_lib.Store(":memory:")
    sup = supervisor.Supervisor(cfg, fake, store, equity_fn=lambda now: EQUITY)
    return sup, store, cfg


def good_plan(cfg: dict, valid_minutes: int = 30) -> schema.Plan:
    """Returns a one-slot plan that the validator accepts."""
    market = config.trade_markets(cfg)[0]
    return schema.Plan(
        valid_minutes=valid_minutes, reasoning="calm night",
        slots=[schema.SlotPlan(market=market, depth_pct=1.5, size_tmn=500_000)])


def bad_plan() -> schema.Plan:
    """Returns a plan with an unknown market."""
    return schema.Plan(slots=[schema.SlotPlan(
        market="XXXTMN", depth_pct=1.5, size_tmn=500_000)])


def test_default_plan_is_active_before_any_call() -> None:
    """Scenario: a new supervisor has no model plan yet."""
    # given
    sup, store, cfg = make_supervisor(provider.FakeProvider())

    # when
    plan, plan_id = sup.active_plan(0.0)

    # then
    assert plan.reasoning == "default plan"
    rows = store.rows("plans")
    assert len(rows) == 1
    assert rows[0]["provider"] == "config"
    assert rows[0]["accepted"] is True
    assert plan_id == rows[0]["id"]


def test_accepted_plan_becomes_active_and_is_logged() -> None:
    """Scenario: the model returns a valid plan."""
    # given
    cfg = make_cfg()
    plan = good_plan(cfg)
    sup, store, _ = make_supervisor(provider.FakeProvider(plans=[plan]))

    # when
    asyncio.run(sup.run_plan(100.0, {"equity": 1}))
    active, plan_id = sup.active_plan(101.0)

    # then
    assert active == plan
    rows = store.rows("plans")
    assert rows[-1]["provider"] == "fake"
    assert rows[-1]["accepted"] is True
    assert plan_id == rows[-1]["id"]
    logs = store.rows("logs", "kind = 'plan'")
    assert len(logs) == 1
    assert logs[0]["text"].startswith(f"Plan {plan_id}: calm night")
    assert logs[0]["fields"]["valid_minutes"] == 30


def test_plan_expires_after_valid_minutes() -> None:
    """Scenario: a plan of 10 minutes is active at 599 s and gone at 600 s."""
    # given
    cfg = make_cfg()
    sup, store, _ = make_supervisor(
        provider.FakeProvider(plans=[good_plan(cfg, valid_minutes=10)]))
    asyncio.run(sup.run_plan(1000.0, {}))

    # when
    before, _ = sup.active_plan(1599.0)
    after, after_id = sup.active_plan(1600.0)

    # then
    assert before.reasoning == "calm night"
    assert after.reasoning == "default plan"
    assert after_id == store.rows("plans")[0]["id"]


def test_timeout_keeps_last_valid_plan() -> None:
    """Scenario: the second call times out. The first plan stays active."""
    # given
    cfg = make_cfg(timeout_s=0.05)
    plan = good_plan(cfg)
    fake = provider.FakeProvider(plans=[plan])
    sup, store, _ = make_supervisor(fake, timeout_s=0.05)
    asyncio.run(sup.run_plan(0.0, {}))
    fake._delay_s = 0.3

    # when
    asyncio.run(sup.run_plan(10.0, {}))
    active, _ = sup.active_plan(11.0)

    # then
    assert active == plan
    ai_logs = store.rows("logs", "kind = 'ai'")
    assert len(ai_logs) == 1
    assert "timeout" in ai_logs[0]["text"]
    calls = store.rows("ai_calls")
    assert calls[-1]["error"] is not None
    assert calls[-1]["input_tokens"] == 0


def test_two_failures_in_a_row_give_default_plan() -> None:
    """Scenario: one good plan, then two provider errors."""
    # given
    cfg = make_cfg()
    fake = provider.FakeProvider(plans=[good_plan(cfg)])
    sup, store, _ = make_supervisor(fake)
    asyncio.run(sup.run_plan(0.0, {}))
    fake._error = provider.ProviderError("down")

    # when
    asyncio.run(sup.run_plan(10.0, {}))
    one_failure, _ = sup.active_plan(11.0)
    asyncio.run(sup.run_plan(20.0, {}))
    two_failures, plan_id = sup.active_plan(21.0)

    # then
    assert one_failure.reasoning == "calm night"
    assert two_failures.reasoning == "default plan"
    assert plan_id == store.rows("plans")[0]["id"]


def test_rejected_plan_counts_as_failure_and_keeps_last_plan() -> None:
    """Scenario: the model names an unknown market."""
    # given
    cfg = make_cfg()
    good = good_plan(cfg)
    sup, store, _ = make_supervisor(
        provider.FakeProvider(plans=[good, bad_plan()]))
    asyncio.run(sup.run_plan(0.0, {}))

    # when
    asyncio.run(sup.run_plan(10.0, {}))
    active, _ = sup.active_plan(11.0)

    # then
    assert active == good
    rows = store.rows("plans")
    assert rows[-1]["accepted"] is False
    assert rows[-1]["validated_json"] is None
    assert "unknown market" in rows[-1]["notes"]
    assert len(store.rows("logs", "kind = 'ai'")) == 1


def test_alert_makes_due_and_goes_into_snapshot() -> None:
    """Scenario: an alert after a call. The next snapshot carries it once."""
    # given
    fake = provider.FakeProvider()
    sup, _, _ = make_supervisor(fake)
    asyncio.run(sup.run_plan(0.0, {"equity": 1}))
    assert not sup.due(10.0)

    # when
    sup.alert("filter trip", 10.0)
    due = sup.due(10.0)
    asyncio.run(sup.run_plan(10.0, {"equity": 1}))

    # then
    assert due
    assert fake.calls[1][1]["alerts"] == ["filter trip"]
    assert fake.calls[1][1]["equity"] == 1
    assert not sup.due(11.0)
    asyncio.run(sup.run_plan(20.0, {}))
    assert fake.calls[2][1]["alerts"] == []


def test_due_before_first_call_and_after_interval() -> None:
    """Scenario: due at start, not due after a call, due after 900 s."""
    # given
    sup, _, _ = make_supervisor(provider.FakeProvider())
    assert sup.due(0.0)

    # when
    asyncio.run(sup.run_plan(100.0, {}))

    # then
    assert not sup.due(999.0)
    assert sup.due(1000.0)


def test_due_is_false_while_a_plan_call_runs() -> None:
    """Scenario: an alert is pending but a call is in the thread."""
    # given
    sup, _, _ = make_supervisor(provider.FakeProvider(delay_s=0.2))
    seen = []

    async def scenario() -> None:
        task = asyncio.create_task(sup.run_plan(0.0, {}))
        await asyncio.sleep(0.05)
        sup.alert("loss", 1.0)
        seen.append(sup.due(2.0))
        await task

    # when
    asyncio.run(scenario())

    # then
    assert seen == [False]


def test_budget_reached_makes_no_call_and_one_log_row() -> None:
    """Scenario: the spent cost equals the budget. Two runs, no call."""
    # given
    fake = provider.FakeProvider()
    sup, store, _ = make_supervisor(fake, budget_usd=1.0)
    store.add_ai_call(ts=0.0, job="plan", model="x", input_tokens=1,
                      output_tokens=1, cost_usd=1.0, latency_s=0.1, error=None)

    # when
    asyncio.run(sup.run_plan(10.0, {}))
    asyncio.run(sup.run_plan(20.0, {}))

    # then
    assert fake.calls == []
    assert sup.budget_left() == 0.0
    logs = store.rows("logs", "kind = 'ai'")
    assert len(logs) == 1
    assert logs[0]["text"].startswith("AI budget reached")


def test_ai_calls_rows_carry_the_cost() -> None:
    """Scenario: a provider result with tokens and a cost."""
    # given
    cfg = make_cfg()
    plan = good_plan(cfg)

    class Priced(provider.FakeProvider):
        def propose_plan(self, snapshot):
            result = super().propose_plan(snapshot)
            result.input_tokens, result.output_tokens = 1000, 200
            result.cost_usd = 0.25
            return result

    sup, store, _ = make_supervisor(Priced(plans=[plan]), budget_usd=10.0)

    # when
    asyncio.run(sup.run_plan(0.0, {}))

    # then
    row = store.rows("ai_calls")[0]
    assert row["job"] == "plan"
    assert row["model"] == "fake"
    assert row["input_tokens"] == 1000
    assert row["cost_usd"] == 0.25
    assert row["error"] is None
    assert row["latency_s"] >= 0.0
    assert sup.budget_left() == 9.75


def test_hold_or_cut_returns_decision_and_holds_on_timeout() -> None:
    """Scenario: the model says cut. Later the model is too slow."""
    # given
    fake = provider.FakeProvider(
        hold_or_cut=[schema.HoldOrCut(decision="cut", reason="fx moves")])
    sup, store, _ = make_supervisor(fake, timeout_s=0.05)

    # when
    answer = asyncio.run(sup.hold_or_cut({"entry": 1}, 0.0))
    fake._delay_s = 0.3
    slow = asyncio.run(sup.hold_or_cut({"entry": 1}, 10.0))

    # then
    assert answer == ("cut", "fx moves")
    assert slow[0] == "hold"
    assert slow[1].startswith("no answer: ")
    jobs = [row["job"] for row in store.rows("ai_calls")]
    assert jobs == ["hold_or_cut", "hold_or_cut"]


def test_trade_log_writes_a_trade_row() -> None:
    """Scenario: the model writes a log for trade 7."""
    # given
    fake = provider.FakeProvider(
        trade_logs=[schema.TradeLog(text="Bid filled", lesson="wait")])
    sup, store, _ = make_supervisor(fake)

    # when
    asyncio.run(sup.trade_log({"id": 7}, 5.0))

    # then
    rows = store.rows("logs", "kind = 'trade'")
    assert rows[0]["text"] == "Bid filled"
    assert rows[0]["fields"] == {"source": "ai", "lesson": "wait", "trade_id": 7}
    assert store.rows("ai_calls")[0]["job"] == "trade_log"


def test_trade_log_error_only_logs_ai_row() -> None:
    """Scenario: the provider fails on a trade log."""
    # given
    fake = provider.FakeProvider(error=provider.ProviderError("down"))
    sup, store, _ = make_supervisor(fake)

    # when
    asyncio.run(sup.trade_log({"id": 7}, 5.0))

    # then
    assert store.rows("logs", "kind = 'trade'") == []
    assert len(store.rows("logs", "kind = 'ai'")) == 1


def test_daily_review_writes_a_review_row() -> None:
    """Scenario: the model reviews a day."""
    # given
    review = schema.Review(summary="Good", what_worked="depth",
                           what_failed="night", proposed_changes=["size up"])
    sup, store, _ = make_supervisor(provider.FakeProvider(reviews=[review]))

    # when
    asyncio.run(sup.daily_review({"pnl": 1}, 5.0))

    # then
    rows = store.rows("logs", "kind = 'review'")
    assert len(rows) == 1
    for part in ("Good", "depth", "night", "size up"):
        assert part in rows[0]["text"]
    assert rows[0]["fields"]["summary"] == "Good"
    assert store.rows("ai_calls")[0]["job"] == "review"


def test_news_risk_high_raises_alert_and_is_rate_limited() -> None:
    """Scenario: high news risk makes a pending alert. A second call is skipped."""
    # given
    news = schema.NewsSummary(summary="Rate shock", risk="high")
    fake = provider.FakeProvider(news=[news])
    sup, store, _ = make_supervisor(fake)
    asyncio.run(sup.run_plan(0.0, {}))
    assert not sup.due(1.0)

    # when
    asyncio.run(sup.refresh_news(1.0))
    asyncio.run(sup.refresh_news(2.0))
    asyncio.run(sup.run_plan(3.0, {}))

    # then
    assert [name for name, _ in fake.calls].count("news") == 1
    assert fake.calls[-1][1]["alerts"] == ["news risk high"]
    assert fake.calls[-1][1]["news"]["summary"] == "Rate shock"
    assert store.rows("ai_calls")[1]["job"] == "news"


def test_news_normal_risk_raises_no_alert() -> None:
    """Scenario: normal news risk keeps the supervisor not due."""
    # given
    sup, _, _ = make_supervisor(provider.FakeProvider())
    asyncio.run(sup.run_plan(0.0, {}))

    # when
    asyncio.run(sup.refresh_news(1.0))

    # then
    assert not sup.due(2.0)
