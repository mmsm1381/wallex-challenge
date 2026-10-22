"""Tests for the global drop filter and the FX shock filter."""

from sweepbot import config
from sweepbot import filters


def make_filters() -> filters.Filters:
    """Builds the filters with the default thresholds of the config."""
    return filters.Filters(config.load_config(None))


def test_global_drop_of_0_3_pct_in_60_s_trips_the_filter() -> None:
    """A Binance-led drop of 0.3% in 30 s must stop the bids of that coin.

    Scenario:
        - The BTC global mid is 100,000 at t=0 and 99,700 at t=30.
    Expected: a start event, and BTCTMN and BTCUSDT are blocked.
    """
    # given
    state = make_filters()
    state.on_global_mid("BTC", 100_000.0, 0.0)

    # when
    state.on_global_mid("BTC", 99_700.0, 30.0)
    events = state.update(30.0)

    # then
    assert [(e["filter"], e["key"], e["kind"]) for e in events] == [
        ("global_drop", "BTC", "start")]
    assert state.blocks("BTCTMN", "BTC", is_tmn=True) == "global_drop"
    assert state.blocks("BTCUSDT", "BTC", is_tmn=False) == "global_drop"
    assert state.blocks("ETHTMN", "ETH", is_tmn=True) is None


def test_global_drop_of_0_2_pct_does_not_trip() -> None:
    """A drop below the threshold is normal noise."""
    # given
    state = make_filters()
    state.on_global_mid("BTC", 100_000.0, 0.0)

    # when
    state.on_global_mid("BTC", 99_800.0, 30.0)

    # then
    assert state.update(30.0) == []
    assert state.blocks("BTCTMN", "BTC", is_tmn=True) is None


def test_slow_drop_over_more_than_60_s_does_not_trip() -> None:
    """A 0.3% fall over 2 minutes is not a fast drop."""
    # given
    state = make_filters()
    for second in range(0, 121, 10):
        state.on_global_mid("BTC", 100_000.0 * (1 - 0.000025 * second), float(second))

    # when
    events = state.update(120.0)

    # then
    assert events == []


def test_global_drop_filter_clears_120_s_after_the_drop_ends() -> None:
    """The filter waits global_wait_s after the last sample with the drop."""
    # given
    state = make_filters()
    state.on_global_mid("BTC", 100_000.0, 0.0)
    state.on_global_mid("BTC", 99_700.0, 30.0)
    state.update(30.0)
    state.on_global_mid("BTC", 99_700.0, 100.0)  # The 0 s high left the window.

    # when
    before = state.update(149.0)
    after = state.update(150.0)

    # then
    assert before == []
    assert [(e["filter"], e["kind"]) for e in after] == [("global_drop", "end")]
    assert state.blocks("BTCTMN", "BTC", is_tmn=True) is None


def test_fx_move_of_0_5_pct_in_5_minutes_trips_the_filter() -> None:
    """A USDTTMN move of 0.5% blocks toman markets only."""
    # given
    state = make_filters()
    state.on_usdt_rate(250_000.0, 0.0)

    # when
    state.on_usdt_rate(251_300.0, 200.0)
    events = state.update(200.0)

    # then
    assert [(e["filter"], e["kind"]) for e in events] == [("fx_shock", "start")]
    assert state.blocks("BTCTMN", "BTC", is_tmn=True) == "fx_shock"
    assert state.blocks("BTCUSDT", "BTC", is_tmn=False) is None


def test_usdttmn_crash_of_2026_10_01_trips_the_fx_filter() -> None:
    """At 05:36 UTC USDTTMN fell from 253k to 235k in one minute."""
    # given
    state = make_filters()
    state.on_usdt_rate(253_000.0, 0.0)

    # when
    state.on_usdt_rate(235_000.0, 60.0)
    events = state.update(60.0)

    # then
    assert events[0]["filter"] == "fx_shock"
    assert events[0]["move_pct"] < -7


def test_fx_filter_clears_10_minutes_after_the_move() -> None:
    """The FX filter waits fx_wait_s (600 s) after the last sample with the move."""
    # given
    state = make_filters()
    state.on_usdt_rate(250_000.0, 0.0)
    state.on_usdt_rate(251_300.0, 10.0)
    state.update(10.0)

    # when
    still_on = state.blocks("XRPTMN", "XRP", is_tmn=True)
    events = state.update(610.0)

    # then
    assert still_on == "fx_shock"
    assert [(e["filter"], e["kind"]) for e in events] == [("fx_shock", "end")]
    assert state.blocks("XRPTMN", "XRP", is_tmn=True) is None


def test_rolling_extremes_drop_old_samples() -> None:
    """The high and the low only see the samples of the window."""
    # given
    window = filters.RollingExtremes(10.0)
    window.add(0.0, 5.0)
    window.add(1.0, 1.0)

    # when
    window.add(12.0, 3.0)

    # then
    assert (window.high(), window.low()) == (3.0, 3.0)
