"""Tests for the sweep bot market state: fair value and the USDT rate."""

import math

import pytest

from sweepbot import config
from sweepbot import market


def make_market(tmp_path) -> market.Market:
    """Builds a market state with a premium that is ready after 1 sample."""
    cfg = config.load_config(None)
    cfg["bot"]["out_dir"] = str(tmp_path)
    cfg["premium"]["min_samples"] = 1
    return market.Market(cfg)


def test_fair_value_of_tmn_market_uses_global_mid_usdttmn_and_premium(tmp_path) -> None:
    """Fair value = global mid 100 x USDTTMN mid 250000 x exp(premium 0.01)."""
    # given
    state = make_market(tmp_path)
    state.on_book("USDTTMN", "bid", [(249999.0, 10.0)], 1.0)
    state.on_book("USDTTMN", "ask", [(250001.0, 10.0)], 1.0)
    state.on_quote("bybit", "XRP", 99.0, 101.0, 1.0)
    state.on_quote("kucoin", "XRP", 99.0, 101.0, 1.0)
    state.engine.premiums["XRPTMN"].add(1.0, 0.01)

    # when
    fv = state.fair_value("XRPTMN", 2.0)

    # then
    assert fv == pytest.approx(25_000_000.0 * math.exp(0.01))


def test_usdt_rate_with_live_usdttmn_uses_its_mid(tmp_path) -> None:
    """A USDTTMN book that changed 10 s ago gives the USDTTMN mid."""
    # given
    state = make_market(tmp_path)
    state.on_book("USDTTMN", "bid", [(249_000.0, 10.0)], 100.0)
    state.on_book("USDTTMN", "ask", [(251_000.0, 10.0)], 100.0)

    # when
    rate, source = state.usdt_rate(110.0)

    # then
    assert (rate, source) == (250_000.0, "usdttmn")


def test_usdt_rate_with_frozen_usdttmn_uses_implied_rate(tmp_path) -> None:
    """At night USDTTMN is closed. The rate comes from coin TMN / coin USDT.

    Scenario:
        - USDTTMN last changed 1000 s ago at a mid of 250,000.
        - BTCTMN mid / BTCUSDT mid = 260,000 and ETHTMN / ETHUSDT = 262,000.
    Expected: the implied rate is the median, 261,000.
    """
    # given
    state = make_market(tmp_path)
    state.on_book("USDTTMN", "bid", [(249_999.0, 10.0)], 0.0)
    state.on_book("USDTTMN", "ask", [(250_001.0, 10.0)], 0.0)
    for name, mid in (("BTCUSDT", 100_000.0), ("BTCTMN", 26_000_000_000.0),
                      ("ETHUSDT", 4_000.0), ("ETHTMN", 1_048_000_000.0)):
        state.on_book(name, "bid", [(mid * 0.9999, 1.0)], 1000.0)
        state.on_book(name, "ask", [(mid * 1.0001, 1.0)], 1000.0)

    # when
    rate, source = state.usdt_rate(1000.0)

    # then
    assert source == "implied"
    assert rate == pytest.approx(261_000.0)


def test_implied_rate_ignores_wide_books(tmp_path) -> None:
    """A coin with a spread above filters.max_spread_pct does not count."""
    # given
    state = make_market(tmp_path)
    state.on_book("BTCUSDT", "bid", [(99_990.0, 1.0)], 1.0)
    state.on_book("BTCUSDT", "ask", [(100_010.0, 1.0)], 1.0)
    state.on_book("BTCTMN", "bid", [(25_000_000_000.0, 1.0)], 1.0)
    state.on_book("BTCTMN", "ask", [(26_000_000_000.0, 1.0)], 1.0)

    # when
    rate = state.implied_usdt_rate()

    # then
    assert rate is None


def test_listeners_get_books_and_trades(tmp_path) -> None:
    """A paper broker needs each book update and each trade."""
    # given
    state = make_market(tmp_path)
    seen = []
    state.add_listener(on_book=lambda m, book, now: seen.append(("book", m)),
                       on_trade=lambda m, p, q, bm, now: seen.append(("trade", m, p)))

    # when
    state.on_book("BTCTMN", "bid", [(1.0, 1.0)], 1.0)
    state.on_trades("BTCTMN", [(2.0, 0.5, True, None)], 1.0)

    # then
    assert seen == [("book", "BTCTMN"), ("trade", "BTCTMN", 2.0)]


def test_saved_premiums_make_fair_value_ready_after_a_restart(tmp_path) -> None:
    """The premium of a market survives a restart through the premium file.

    Scenario:
        - The first market state learns XRPTMN samples and saves them at t=10.
        - A new market state (the restarted bot) loads the file at t=20.
    Expected: the new state has a fair value at once, with the same premium.
    """
    # given
    first = make_market(tmp_path)
    first.engine.premiums["XRPTMN"].add(5.0, 0.01)
    path = str(tmp_path / "premiums.json")
    first.save_premiums(path, 10.0)
    second = make_market(tmp_path)
    second.on_book("USDTTMN", "bid", [(249999.0, 10.0)], 20.0)
    second.on_book("USDTTMN", "ask", [(250001.0, 10.0)], 20.0)
    second.on_quote("bybit", "XRP", 99.0, 101.0, 20.0)
    second.on_quote("kucoin", "XRP", 99.0, 101.0, 20.0)

    # when
    loaded = second.load_premiums(path, 20.0)

    # then
    assert loaded == 1
    assert second.fair_value("XRPTMN", 20.0) == pytest.approx(25_000_000.0 * math.exp(0.01))


def test_missing_or_broken_premium_file_loads_nothing(tmp_path) -> None:
    """A first start has no file. A broken file must not stop the bot."""
    # given
    state = make_market(tmp_path)
    (tmp_path / "broken.json").write_text("{not json")

    # when / then
    assert state.load_premiums(str(tmp_path / "missing.json"), 1.0) == 0
    assert state.load_premiums(str(tmp_path / "broken.json"), 1.0) == 0
