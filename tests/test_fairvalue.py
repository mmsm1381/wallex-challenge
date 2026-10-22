"""Tests for the global fair value and the rolling Wallex premium."""

import pytest

from lagcatcher import fairvalue


def test_global_mid_with_one_stale_venue_returns_median_of_fresh_venues() -> None:
    """Two fresh venues and one stale venue: the stale quote must not count."""
    # given
    quotes = fairvalue.Quotes(max_quote_age_s=10.0, min_venues=2)
    quotes.update(venue="bybit", coin="BTC", bid=99.0, ask=101.0, ts=100.0)
    quotes.update(venue="okx", coin="BTC", bid=101.0, ask=103.0, ts=105.0)
    quotes.update(venue="gate", coin="BTC", bid=50.0, ask=52.0, ts=80.0)

    # when
    mid = quotes.global_mid(coin="BTC", now=110.0)

    # then
    assert mid == 101.0


def test_global_mid_with_too_few_fresh_venues_returns_none() -> None:
    """A silent feed makes its quote stale, so the fair value must stop."""
    # given
    quotes = fairvalue.Quotes(max_quote_age_s=10.0, min_venues=2)
    quotes.update(venue="bybit", coin="BTC", bid=99.0, ask=101.0, ts=100.0)
    quotes.update(venue="okx", coin="BTC", bid=101.0, ask=103.0, ts=95.0)

    # when
    mid = quotes.global_mid(coin="BTC", now=106.0)

    # then
    assert mid is None


def test_global_mid_with_crossed_quote_ignores_that_venue() -> None:
    """A quote with bid above ask is bad data, so the venue does not count."""
    # given
    quotes = fairvalue.Quotes(max_quote_age_s=10.0, min_venues=2)
    quotes.update(venue="bybit", coin="BTC", bid=99.0, ask=101.0, ts=100.0)
    quotes.update(venue="okx", coin="BTC", bid=104.0, ask=102.0, ts=100.0)
    quotes.update(venue="kucoin", coin="BTC", bid=101.0, ask=103.0, ts=100.0)

    # when
    mid = quotes.global_mid(coin="BTC", now=101.0)

    # then
    assert mid == 101.0


def test_premium_value_before_min_samples_returns_none() -> None:
    """The premium needs a warm-up, so no signal can use a guess."""
    # given
    premium = fairvalue.Premium(window_s=3600.0, min_samples=3)
    premium.add(ts=100.0, log_basis=0.01)
    premium.add(ts=105.0, log_basis=0.02)

    # when
    value = premium.value(now=106.0)

    # then
    assert value is None


def test_premium_value_after_warmup_returns_median_of_window() -> None:
    """Three samples in the window give their median."""
    # given
    premium = fairvalue.Premium(window_s=3600.0, min_samples=3)
    premium.add(ts=100.0, log_basis=0.03)
    premium.add(ts=105.0, log_basis=0.01)
    premium.add(ts=110.0, log_basis=0.02)

    # when
    value = premium.value(now=111.0)

    # then
    assert value == 0.02


def test_premium_value_drops_samples_older_than_window() -> None:
    """A sample older than the window must not count toward the median."""
    # given
    premium = fairvalue.Premium(window_s=100.0, min_samples=2)
    premium.add(ts=0.0, log_basis=0.50)
    premium.add(ts=150.0, log_basis=0.01)
    premium.add(ts=160.0, log_basis=0.03)

    # when
    value = premium.value(now=170.0)

    # then
    assert value == 0.02


def test_premium_export_and_load_keeps_only_samples_in_the_window() -> None:
    """A restart loads the saved samples. Samples older than the window are dropped.

    Scenario:
        - The window is 100 s and 2 samples are needed.
        - Samples at t=10, 50 and 90 are exported and loaded at t=140.
    Expected: the t=10 sample is too old. The other 2 make the value ready.
    """
    # given
    old = fairvalue.Premium(window_s=100.0, min_samples=2)
    for ts, basis in ((10.0, 0.5), (50.0, 0.01), (90.0, 0.03)):
        old.add(ts, basis)
    new = fairvalue.Premium(window_s=100.0, min_samples=2)

    # when
    new.load(old.export(), now=140.0)

    # then
    assert new.export() == [(50.0, 0.01), (90.0, 0.03)]
    assert new.value(140.0) == pytest.approx(0.02)
