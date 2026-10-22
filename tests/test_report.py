"""Tests for the report summaries."""

import pytest

from lagcatcher import report


def test_summarize_with_two_exits_and_one_miss_reports_rates_and_net() -> None:
    """Two fills (+0.5% and -0.1%) and one miss for one taker strategy."""
    # given
    events = [
        {"type": "signal", "strategy": "taker_1s", "market": "XRPTMN", "ts": 0.0},
        {"type": "fill", "strategy": "taker_1s", "market": "XRPTMN", "ts": 1.0},
        {"type": "exit", "strategy": "taker_1s", "market": "XRPTMN", "ts": 60.0,
         "net_pct": 0.5, "net_tmn": 5000.0},
        {"type": "signal", "strategy": "taker_1s", "market": "XRPTMN", "ts": 100.0},
        {"type": "miss", "strategy": "taker_1s", "market": "XRPTMN", "ts": 101.0},
        {"type": "signal", "strategy": "taker_1s", "market": "XRPTMN", "ts": 200.0},
        {"type": "fill", "strategy": "taker_1s", "market": "XRPTMN", "ts": 201.0},
        {"type": "exit", "strategy": "taker_1s", "market": "XRPTMN", "ts": 260.0,
         "net_pct": -0.1, "net_tmn": -1000.0},
    ]

    # when
    rows = report.summarize(events, equity_tmn=5_000_000.0)

    # then
    assert rows[0]["strategy"] == "taker_1s"
    assert rows[0]["fills"] == 2
    assert rows[0]["misses"] == 1
    assert rows[0]["fill_rate_pct"] == pytest.approx(66.6667, abs=1e-3)
    assert rows[0]["win_pct"] == 50.0
    assert rows[0]["median_net_pct"] == pytest.approx(0.2)
    assert rows[0]["net_tmn"] == 4000.0
    assert rows[0]["net_pct_of_equity"] == pytest.approx(0.08)


def test_summarize_episodes_reports_count_per_hour_and_durations() -> None:
    """Three 1% episodes in two hours, lasting 2, 4, and 30 seconds."""
    # given
    closed = [
        {"market": "XRPTMN", "threshold_pct": 1.0, "duration_s": 2.0,
         "depth_tmn_at_max": 100000.0},
        {"market": "XRPTMN", "threshold_pct": 1.0, "duration_s": 4.0,
         "depth_tmn_at_max": 300000.0},
        {"market": "XRPTMN", "threshold_pct": 1.0, "duration_s": 30.0,
         "depth_tmn_at_max": 500000.0},
    ]

    # when
    rows = report.summarize_episodes(closed, hours=2.0)

    # then
    assert rows[0]["count"] == 3
    assert rows[0]["per_hour"] == 1.5
    assert rows[0]["median_duration_s"] == 4.0
    assert rows[0]["p90_duration_s"] == 30.0
    assert rows[0]["median_depth_tmn"] == 300000.0
