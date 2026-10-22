"""Tests for the daily report of the sweep bot."""

import calendar
import os

import pytest

from sweepbot import report
from sweepbot import store

DAY = "2026-10-22"
START = calendar.timegm((2026, 10, 21, 20, 30, 0))  # Tehran midnight.


@pytest.fixture
def db() -> store.Store:
    """Returns an in-memory store."""
    return store.Store(":memory:")


def add_trade(db: store.Store, ts: float, mode: str, net_tmn,
              net_pct: float = 1.0) -> None:
    """Adds one closed trade with fixed values except the given ones."""
    db.add_trade(ts=ts, market="BTCTMN", mode=mode, entry_fill_ids=[1],
                 exit_fill_ids=[2], entry_price=100.0, exit_price=101.0,
                 qty=1.0, net_quote=1.0, net_tmn=net_tmn, net_pct=net_pct,
                 hold_s=30.0, exit_reason="target")


def test_day_bounds_start_at_tehran_midnight() -> None:
    """Tehran day 2026-10-22 starts at 2026-10-21T20:30:00Z and lasts 24 h."""
    # given
    day = DAY

    # when
    start, end = report.day_bounds(day)

    # then
    assert start == START
    assert end == START + 86400


def test_mode_summary_numbers_and_drawdown() -> None:
    """A known sequence gives the expected totals and a drawdown of -70."""
    # given
    trades = [
        {"net_tmn": 100.0, "net_pct": 1.0},
        {"net_tmn": -30.0, "net_pct": -0.3},
        {"net_tmn": -40.0, "net_pct": -0.4},
        {"net_tmn": 50.0, "net_pct": 0.5},
        {"net_tmn": None, "net_pct": 0.0},
    ]

    # when
    summary = report.mode_summary(trades)

    # then
    assert summary == {"trades": 5, "wins": 2, "win_pct": 40.0,
                       "median_pct": 0.0, "net_tmn": 80.0,
                       "worst_tmn": -40.0, "max_drawdown_tmn": -70.0}


def test_mode_summary_has_zero_drawdown_when_net_only_rises() -> None:
    """Cumulative net that never dips gives a drawdown of 0."""
    # given
    trades = [{"net_tmn": 10.0, "net_pct": 1.0},
              {"net_tmn": 20.0, "net_pct": 2.0}]

    # when
    summary = report.mode_summary(trades)

    # then
    assert summary["max_drawdown_tmn"] == 0
    assert summary["median_pct"] == 1.5


def test_empty_store_report_has_all_sections_with_none(db) -> None:
    """An empty store gives every section and "None." in each."""
    # given
    # an empty store

    # when
    text = report.daily_report(db, DAY)

    # then
    assert text.startswith(f"# Daily report {DAY} (Tehran)")
    for name in ("Bot and twin", "Trades", "Plans", "Filters", "AI",
                 "Decision log"):
        assert f"## {name}" in text
    assert text.count("None.") >= 5
    assert "AI effect" not in text


def test_report_shows_paper_and_twin_rows_and_ai_effect(db) -> None:
    """Paper nets 1,500 and twin nets 1,000, so the AI effect is 500."""
    # given
    add_trade(db, START + 3600, "paper", 1500.0)
    add_trade(db, START + 7200, "twin", 1000.0)

    # when
    text = report.daily_report(db, DAY)

    # then
    assert "| paper | 1 | 1 | 100.00 | 1.00 | 1,500 | 1,500 | 0 |" in text
    assert "| twin | 1 | 1 | 100.00 | 1.00 | 1,000 | 1,000 | 0 |" in text
    assert "AI effect: 500 toman" in text
    assert "| 21:30:00 | paper | BTCTMN |" in text


def test_report_excludes_trades_of_next_tehran_day(db) -> None:
    """A trade at the end bound belongs to the next day and is left out."""
    # given
    add_trade(db, START + 86400, "paper", 999.0)
    add_trade(db, START + 86399, "paper", 5.0)

    # when
    text = report.daily_report(db, DAY)

    # then
    assert "999" not in text
    assert "| paper | 1 | 1 |" in text


def test_report_counts_plans_filters_ai_and_logs(db) -> None:
    """Plans, filter starts, AI calls and logs of the day are summarized."""
    # given
    db.add_plan(ts=START + 10, provider="p", model="m", raw_json="{}",
                validated_json="{}", accepted=True, notes="")
    db.add_plan(ts=START + 20, provider="p", model="m", raw_json="{}",
                validated_json=None, accepted=False, notes="bad")
    db.add_filter_event(ts=START + 30, filter_name="spread", key="BTCTMN",
                        kind="start")
    db.add_filter_event(ts=START + 40, filter_name="spread", key="BTCTMN",
                        kind="end")
    db.add_ai_call(ts=START + 50, job="plan", model="m", input_tokens=1,
                   output_tokens=1, cost_usd=0.25, latency_s=1.0, error=None)
    db.add_ai_call(ts=START - 50, job="plan", model="m", input_tokens=1,
                   output_tokens=1, cost_usd=0.5, latency_s=1.0, error="x")
    db.add_log(ts=START + 61, kind="plan", text="New plan.", fields={})

    # when
    text = report.daily_report(db, DAY)

    # then
    assert "Plans: 2 (accepted 1, rejected 1)" in text
    assert "spread: 1" in text
    assert "Calls: 1" in text
    assert "Errors: 0" in text
    assert "Cost that day: 0.25 USD" in text
    assert "Cost total: 0.75 USD" in text
    assert "- 20:31:01 UTC [plan] New plan." in text


def test_write_daily_report_creates_the_file(db, tmp_path) -> None:
    """The report is written to out_dir/report-DAY.md and the path returned."""
    # given
    out_dir = str(tmp_path / "reports" / "new")

    # when
    path = report.write_daily_report(db, DAY, out_dir)

    # then
    assert path == os.path.join(out_dir, f"report-{DAY}.md")
    with open(path, encoding="utf-8") as handle:
        assert handle.read() == report.daily_report(db, DAY)
