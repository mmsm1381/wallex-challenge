"""Tests for the plan validator: schema errors, rejects and clamps."""

import copy
import json

from sweepbot import config
from sweepbot.ai import schema
from sweepbot.ai import validator

EQUITY = 10_000_000.0


def make_cfg() -> dict:
    """Returns the default config."""
    return config.load_config(None)


def make_raw(slots: list[dict] | None = None, **fields) -> dict:
    """Builds a plan dict inside all limits, with `fields` applied on top."""
    if slots is None:
        slots = [{"market": "ETHTMN", "depth_pct": 1.3, "size_tmn": 1_000_000.0},
                 {"market": "ETHUSDT", "depth_pct": 1.6, "size_tmn": 500_000.0}]
    raw = {"valid_minutes": 30, "pause": False, "pause_reason": None,
           "slots": slots, "fx_risk": "normal", "reasoning": "test"}
    raw.update(fields)
    return raw


def test_valid_plan_passes_unchanged() -> None:
    """A plan inside all limits is accepted, with no note and no change."""
    # given
    raw = make_raw()

    # when
    result = validator.validate_plan(raw, cfg=make_cfg(), equity_tmn=EQUITY)

    # then
    assert result.accepted
    assert result.notes == []
    assert result.plan.model_dump() == raw


def test_plan_object_input_passes_unchanged() -> None:
    """A `Plan` object is accepted like a dict."""
    # given
    plan = schema.Plan(**make_raw())

    # when
    result = validator.validate_plan(plan, cfg=make_cfg(), equity_tmn=EQUITY)

    # then
    assert result.accepted
    assert result.plan == plan


def test_json_string_input_is_parsed() -> None:
    """A JSON string gives the same result as the dict."""
    # given
    raw = make_raw()

    # when
    result = validator.validate_plan(json.dumps(raw), cfg=make_cfg(),
                                     equity_tmn=EQUITY)

    # then
    assert result.accepted
    assert result.plan.model_dump() == raw


def test_invalid_json_is_rejected() -> None:
    """Text that is not JSON gives a reject with a note."""
    # given
    text = "{not json"

    # when
    result = validator.validate_plan(text, cfg=make_cfg(), equity_tmn=EQUITY)

    # then
    assert not result.accepted
    assert result.plan is None
    assert len(result.notes) == 1


def test_schema_error_is_rejected() -> None:
    """A missing field or an extra field gives a reject."""
    # given
    missing = {"valid_minutes": 30}
    extra = make_raw(surprise=1)

    # when
    results = [validator.validate_plan(raw, cfg=make_cfg(), equity_tmn=EQUITY)
               for raw in (missing, extra)]

    # then
    assert all(not r.accepted and r.plan is None and r.notes for r in results)


def test_bad_fx_risk_is_rejected() -> None:
    """An `fx_risk` outside {normal, high} gives a reject."""
    # given
    raw = make_raw(fx_risk="medium")

    # when
    result = validator.validate_plan(raw, cfg=make_cfg(), equity_tmn=EQUITY)

    # then
    assert not result.accepted


def test_unknown_market_is_rejected() -> None:
    """A market that is not in the config gives a reject."""
    # given
    raw = make_raw([{"market": "FOOTMN", "depth_pct": 1.3, "size_tmn": 1e6}])

    # when
    result = validator.validate_plan(raw, cfg=make_cfg(), equity_tmn=EQUITY)

    # then
    assert not result.accepted
    assert result.plan is None
    assert any("FOOTMN" in note for note in result.notes)


def test_duplicate_market_is_rejected() -> None:
    """Two slots on one market give a reject."""
    # given
    slot = {"market": "ETHTMN", "depth_pct": 1.3, "size_tmn": 1e6}
    raw = make_raw([slot, dict(slot)])

    # when
    result = validator.validate_plan(raw, cfg=make_cfg(), equity_tmn=EQUITY)

    # then
    assert not result.accepted
    assert any("ETHTMN" in note for note in result.notes)


def test_non_positive_size_is_rejected() -> None:
    """A size of zero or less gives a reject."""
    for size in (0.0, -5.0):
        # given
        raw = make_raw([{"market": "ETHTMN", "depth_pct": 1.3, "size_tmn": size}])

        # when
        result = validator.validate_plan(raw, cfg=make_cfg(), equity_tmn=EQUITY)

        # then
        assert not result.accepted
        assert result.plan is None


def test_depth_below_min_is_clamped_to_min() -> None:
    """Depth 0.5 becomes the minimum 1.0 with a note."""
    # given
    raw = make_raw([{"market": "ETHTMN", "depth_pct": 0.5, "size_tmn": 1e6}])

    # when
    result = validator.validate_plan(raw, cfg=make_cfg(), equity_tmn=EQUITY)

    # then
    assert result.accepted
    assert result.plan.slots[0].depth_pct == 1.0
    assert len(result.notes) == 1


def test_depth_above_max_is_clamped_to_max() -> None:
    """Depth 4.0 becomes the maximum 2.5 with a note."""
    # given
    raw = make_raw([{"market": "ETHTMN", "depth_pct": 4.0, "size_tmn": 1e6}])

    # when
    result = validator.validate_plan(raw, cfg=make_cfg(), equity_tmn=EQUITY)

    # then
    assert result.accepted
    assert result.plan.slots[0].depth_pct == 2.5
    assert len(result.notes) == 1


def test_size_above_max_bid_is_clamped() -> None:
    """A size of 3,000,000 becomes the limit 1,000,000 with a note."""
    # given
    raw = make_raw([{"market": "ETHTMN", "depth_pct": 1.3, "size_tmn": 3e6}])

    # when
    result = validator.validate_plan(raw, cfg=make_cfg(), equity_tmn=EQUITY)

    # then
    assert result.accepted
    assert result.plan.slots[0].size_tmn == 1_000_000.0
    assert len(result.notes) == 1


def test_too_many_slots_keeps_the_first_ones() -> None:
    """With max_slots 2, a plan of 4 slots keeps the first 2."""
    # given
    cfg = make_cfg()
    cfg["limits"]["max_slots"] = 2
    markets = ["ADATMN", "TRXTMN", "ETHTMN", "XRPTMN"]
    raw = make_raw([{"market": m, "depth_pct": 1.3, "size_tmn": 1e6}
                    for m in markets])

    # when
    result = validator.validate_plan(raw, cfg=cfg, equity_tmn=EQUITY)

    # then
    assert result.accepted
    assert [s.market for s in result.plan.slots] == markets[:2]
    assert len(result.notes) == 1


def test_usdt_share_drops_last_usdt_slots() -> None:
    """Equity 5,000,000 allows 2,000,000 in USDT. Three USDT slots of 1,000,000 give 3,000,000.

    Scenario:
        - Slots: ETHUSDT, ETHTMN, PEPEUSDT, ADAUSDT in this order.
        - The validator drops ADAUSDT. ETHUSDT and PEPEUSDT stay.
    """
    # given
    cfg = make_cfg()
    cfg["markets"]["usdt"] = ["ETH", "PEPE", "ADA"]
    raw = make_raw([{"market": m, "depth_pct": 1.3, "size_tmn": 1e6}
                    for m in ("ETHUSDT", "ETHTMN", "PEPEUSDT", "ADAUSDT")])

    # when
    result = validator.validate_plan(raw, cfg=cfg, equity_tmn=5_000_000.0)

    # then
    assert result.accepted
    assert [s.market for s in result.plan.slots] == ["ETHUSDT", "ETHTMN", "PEPEUSDT"]
    assert len(result.notes) == 1


def test_valid_minutes_is_clamped_to_5_and_120() -> None:
    """Valid minutes 1 becomes 5, and 500 becomes 120, each with a note."""
    for given, expected in ((1, 5), (500, 120)):
        # given
        raw = make_raw(valid_minutes=given)

        # when
        result = validator.validate_plan(raw, cfg=make_cfg(), equity_tmn=EQUITY)

        # then
        assert result.accepted
        assert result.plan.valid_minutes == expected
        assert len(result.notes) == 1


def test_input_is_not_mutated() -> None:
    """A plan that needs clamps leaves the input dict and Plan unchanged."""
    # given
    raw = make_raw([{"market": "ETHTMN", "depth_pct": 9.0, "size_tmn": 9e6}],
                   valid_minutes=999)
    raw_copy = copy.deepcopy(raw)
    plan = schema.Plan(**raw)

    # when
    validator.validate_plan(raw, cfg=make_cfg(), equity_tmn=EQUITY)
    validator.validate_plan(plan, cfg=make_cfg(), equity_tmn=EQUITY)

    # then
    assert raw == raw_copy
    assert plan.model_dump() == raw_copy


def test_default_plan_is_valid_and_passes_unchanged() -> None:
    """The default plan of the default config needs no change."""
    # given
    cfg = make_cfg()
    plan = validator.default_plan(cfg)

    # when
    result = validator.validate_plan(plan, cfg=cfg, equity_tmn=EQUITY)

    # then
    assert [s.market for s in plan.slots] == cfg["plan"]["markets"]
    assert plan.reasoning == "default plan"
    assert plan.valid_minutes == cfg["plan"]["valid_minutes"]
    assert result.accepted
    assert result.notes == []
    assert result.plan.model_dump() == plan.model_dump()


def test_default_plan_depth_depends_on_the_quote_currency() -> None:
    """Toman markets bid 1.3% below fair value, USDT markets 1.0%.

    Scenario: the default plan at the start equity of 5M toman.
    Expected: both USDT slots stay (2M is 40% of 5M), with depth 1.0%.
    """
    # given
    cfg = make_cfg()

    # when
    result = validator.validate_plan(validator.default_plan(cfg), cfg=cfg,
                                      equity_tmn=5_000_000.0)

    # then
    depths = {s.market: s.depth_pct for s in result.plan.slots}
    assert depths == {"ADATMN": 1.3, "ETHUSDT": 1.0, "TRXTMN": 1.3,
                      "ETHTMN": 1.3, "PEPEUSDT": 1.0}
    assert result.notes == []
