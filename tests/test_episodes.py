"""Tests for the gap-episode tracker."""

import pytest

from lagcatcher import episodes


def test_episodes_with_gap_open_and_close_reports_duration_and_max_gap() -> None:
    """A 1% gap opens at t=0, peaks at 1.5% at t=2, and closes at t=5."""
    # given
    tracker = episodes.Episodes(thresholds_pct=[1.0])
    tracker.update(market="PEPETMN", now=0.0, gap=0.012, depth_tmn={1.0: 200000.0})
    tracker.update(market="PEPETMN", now=2.0, gap=0.015, depth_tmn={1.0: 500000.0})

    # when
    closed = tracker.update(market="PEPETMN", now=5.0, gap=0.004,
                            depth_tmn={1.0: 0.0})

    # then
    assert len(closed) == 1
    assert closed[0]["duration_s"] == 5.0
    assert closed[0]["max_gap_pct"] == pytest.approx(1.5)
    assert closed[0]["depth_tmn_at_max"] == 500000.0
    assert closed[0]["end_reason"] == "closed"


def test_episodes_with_missing_data_closes_open_episode() -> None:
    """When the fair value stops, an open episode ends with reason no_data."""
    # given
    tracker = episodes.Episodes(thresholds_pct=[1.0])
    tracker.update(market="PEPETMN", now=0.0, gap=0.012, depth_tmn={1.0: 200000.0})

    # when
    closed = tracker.update(market="PEPETMN", now=3.0, gap=None, depth_tmn=None)

    # then
    assert closed[0]["duration_s"] == 3.0
    assert closed[0]["end_reason"] == "no_data"
