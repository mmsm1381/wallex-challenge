"""Tests for the engine state: fair value and the REST seed rule."""

import math

import pytest

from lagcatcher import config
from lagcatcher import engine


def test_engine_fair_value_for_tmn_market_uses_usdttmn_mid_and_premium(tmp_path) -> None:
    """Fair value = global mid 100 x USDTTMN mid 250000 x exp(premium 0.01)."""
    # given
    cfg = config.load_config(path=None)
    cfg["run"]["out_dir"] = str(tmp_path)
    cfg["premium"]["min_samples"] = 1
    live = engine.Engine(cfg)
    live.on_book("USDTTMN", "bid", [(249999.0, 10.0)], 1.0, None)
    live.on_book("USDTTMN", "ask", [(250001.0, 10.0)], 1.0, None)
    live.on_quote("bybit", "XRP", 99.0, 101.0, 1.0)
    live.on_quote("okx", "XRP", 99.0, 101.0, 1.0)
    live.premiums["XRPTMN"].add(1.0, 0.01)

    # when
    fv = live.fair_value("XRPTMN", 2.0)

    # then
    assert fv == pytest.approx(25000000.0 * math.exp(0.01))


def test_engine_rest_seed_after_newer_push_keeps_push_book(tmp_path) -> None:
    """A REST snapshot that started before a websocket push must not replace it."""
    # given
    cfg = config.load_config(path=None)
    cfg["run"]["out_dir"] = str(tmp_path)
    live = engine.Engine(cfg)
    live.on_book("XRPTMN", "ask", [(101.0, 5.0)], 10.0, None)

    # when
    live.on_book("XRPTMN", "ask", [(105.0, 5.0)], 11.0, 9.0)

    # then
    assert live.books["XRPTMN"].asks == [(101.0, 5.0)]


def test_engine_with_stale_wallex_feed_sends_no_signal(tmp_path) -> None:
    """A dead Wallex feed leaves old books in memory. They must not give signals.

    Scenario:
        - The XRPTMN ask is 10% below fair value, which is a strong signal.
        - The last Wallex message is 10 s old, and the stale limit is 5 s.
    Expected: the taker strategies record no signal.
    """
    # given
    cfg = config.load_config(path=None)
    cfg["run"]["out_dir"] = str(tmp_path)
    cfg["run"]["wallex_stale_s"] = 5.0
    cfg["premium"]["min_samples"] = 1
    live = engine.Engine(cfg)
    live.on_book("USDTTMN", "bid", [(249999.0, 10.0)], 1.0, None)
    live.on_book("USDTTMN", "ask", [(250001.0, 10.0)], 1.0, None)
    live.on_book("XRPTMN", "bid", [(22400000.0, 10.0)], 1.0, None)
    live.on_book("XRPTMN", "ask", [(22500000.0, 10.0)], 1.0, None)
    live.premiums["XRPTMN"].add(1.0, 0.0)
    live.stats["wallex"]["last"] = 1.0

    # when
    live.on_quote("bybit", "XRP", 99.0, 101.0, 11.0)
    live.on_quote("kucoin", "XRP", 99.0, 101.0, 11.0)

    # then
    assert live.counts["taker_1s"]["signal"] == 0


def test_engine_with_live_wallex_feed_sends_signal_on_big_gap(tmp_path) -> None:
    """The same 10% gap gives a signal when the Wallex feed is live."""
    # given
    cfg = config.load_config(path=None)
    cfg["run"]["out_dir"] = str(tmp_path)
    cfg["run"]["wallex_stale_s"] = 5.0
    cfg["premium"]["min_samples"] = 1
    live = engine.Engine(cfg)
    live.on_book("USDTTMN", "bid", [(249999.0, 10.0)], 1.0, None)
    live.on_book("USDTTMN", "ask", [(250001.0, 10.0)], 1.0, None)
    live.on_book("XRPTMN", "bid", [(22400000.0, 10.0)], 1.0, None)
    live.on_book("XRPTMN", "ask", [(22500000.0, 10.0)], 1.0, None)
    live.premiums["XRPTMN"].add(1.0, 0.0)
    live.stats["wallex"]["last"] = 10.0

    # when
    live.on_quote("bybit", "XRP", 99.0, 101.0, 11.0)
    live.on_quote("kucoin", "XRP", 99.0, 101.0, 11.0)

    # then
    assert live.counts["taker_1s"]["signal"] == 1


def test_disk_action_with_enough_free_space_returns_ok() -> None:
    """900 MB free is above both limits."""
    # given
    free_mb = 900.0

    # when
    action = engine.disk_action(free_mb=free_mb, min_record_mb=600.0, min_run_mb=300.0)

    # then
    assert action == "ok"


def test_disk_action_below_record_limit_returns_stop_raw() -> None:
    """500 MB free: stop the raw files, but keep the paper test running."""
    # given
    free_mb = 500.0

    # when
    action = engine.disk_action(free_mb=free_mb, min_record_mb=600.0, min_run_mb=300.0)

    # then
    assert action == "stop_raw"


def test_disk_action_below_run_limit_returns_stop() -> None:
    """250 MB free: stop the whole run to protect the other programs on the host."""
    # given
    free_mb = 250.0

    # when
    action = engine.disk_action(free_mb=free_mb, min_record_mb=600.0, min_run_mb=300.0)

    # then
    assert action == "stop"


def test_disk_action_with_raw_off_and_space_back_above_margin_returns_resume_raw() -> None:
    """Raw recording comes back when free space is 100 MB above the limit again."""
    # given
    free_mb = 750.0

    # when
    action = engine.disk_action(free_mb=free_mb, min_record_mb=600.0,
                                min_run_mb=300.0, raw_off=True)

    # then
    assert action == "resume_raw"


def test_disk_action_with_raw_off_and_space_just_above_limit_returns_stop_raw() -> None:
    """650 MB is above the 600 MB limit but inside the 100 MB margin, so raw stays off."""
    # given
    free_mb = 650.0

    # when
    action = engine.disk_action(free_mb=free_mb, min_record_mb=600.0,
                                min_run_mb=300.0, raw_off=True)

    # then
    assert action == "stop_raw"


def test_engine_premium_sample_with_wide_spread_is_skipped(tmp_path) -> None:
    """Only the narrow XRPUSDT book gives a premium sample.

    Scenario:
        - The global mid of XRP is 1.0.
        - XRPUSDT has a 0.2% spread around 1.0. ADAUSDT has a 2% spread.
        - The premium limit is 0.3%.
    Expected: XRPUSDT has a premium of 0. ADAUSDT has no premium.
    """
    # given
    cfg = config.load_config(path=None)
    cfg["run"]["out_dir"] = str(tmp_path)
    cfg["premium"]["min_samples"] = 1
    cfg["premium"]["max_spread_pct"] = 0.3
    live = engine.Engine(cfg)
    for venue in ("bybit", "okx"):
        live.on_quote(venue, "XRP", 0.999, 1.001, 1.0)
        live.on_quote(venue, "ADA", 0.999, 1.001, 1.0)
    live.on_book("XRPUSDT", "bid", [(0.999, 10.0)], 1.0, None)
    live.on_book("XRPUSDT", "ask", [(1.001, 10.0)], 1.0, None)
    live.on_book("ADAUSDT", "bid", [(0.99, 10.0)], 1.0, None)
    live.on_book("ADAUSDT", "ask", [(1.01, 10.0)], 1.0, None)

    # when
    live.sample_premiums(2.0)

    # then
    assert live.premiums["XRPUSDT"].value(2.0) == pytest.approx(0.0)
    assert live.premiums["ADAUSDT"].value(2.0) is None
