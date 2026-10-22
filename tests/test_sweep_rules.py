"""Tests for the Wallex market rules: ticks, steps and minimum sizes."""

import pytest

from sweepbot import rules


def test_load_rules_reads_decimals_as_tick_and_step() -> None:
    """BTCTMN has integer toman prices and 8 decimals of quantity."""
    # given
    path = "research/wallex_markets.json"

    # when
    loaded = rules.load_rules(path)

    # then
    btc = loaded["BTCTMN"]
    assert (btc.price_tick, btc.qty_step) == (1.0, pytest.approx(1e-8))
    assert btc.min_notional == 50_000
    assert loaded["ETHUSDT"].price_tick == pytest.approx(0.01)
    assert loaded["PEPEUSDT"].qty_step == 1.0


def test_order_qty_rounds_down_to_the_step() -> None:
    """1,000,000 toman of ADA at 3,333 toman is 300.03 ADA, rounded to 300.0."""
    # given
    rule = rules.MarketRule(price_tick=1.0, qty_step=0.1, min_qty=0.1, min_notional=50_000)

    # when
    qty = rule.order_qty(1_000_000.0, 3_333.0)

    # then
    assert qty == pytest.approx(300.0)


def test_order_qty_below_min_notional_is_none() -> None:
    """An order below 50,000 toman is not allowed."""
    # given
    rule = rules.MarketRule(price_tick=1.0, qty_step=0.1, min_qty=0.1, min_notional=50_000)

    # when
    qty = rule.order_qty(40_000.0, 100.0)

    # then
    assert qty is None


def test_load_rules_without_path_gives_no_rules() -> None:
    """No file means no rounding, as in the replay tests."""
    # given / when
    loaded = rules.load_rules("")

    # then
    assert loaded == {}
