"""Replay test of the sweep bot on a rebuilt BTCTMN sweep.

The real raw files of 2026-10-22 are on the shadow-test server. This test
rebuilds the 07:03:53 UTC sweep of RESEARCH.md section 3 with the same
prices: fair value 21.996B toman, sellers down to 21.66B, a buyer at
22.018B 2.6 s later.
"""

import asyncio

import pytest

from sweepbot import config
from sweepbot import runner as runner_mod
from sweepbot import store as store_mod

FV = 21_996_000_000.0
USDTTMN = 219_960.0
BTC_USDT = 100_000.0


def sweep_records(sweep_at: float = 200.0) -> list[tuple[float, str, dict]]:
    """Returns a calm market for `sweep_at` seconds, the sweep and the rebound."""
    records = []
    for second in range(0, int(sweep_at) + 30):
        ts = float(second)
        for venue in ("bybit", "kucoin"):
            records.append((ts, "quote", {"v": venue, "c": "BTC",
                                          "b": BTC_USDT - 1, "a": BTC_USDT + 1}))
        for name, mid in (("USDTTMN", USDTTMN), ("BTCTMN", FV)):
            records.append((ts + 0.1, "book", {"m": name, "s": "bid", "lv": [[mid * 0.99995, 5.0]]}))
            records.append((ts + 0.1, "book", {"m": name, "s": "ask", "lv": [[mid * 1.00005, 5.0]]}))
    for step in range(85):
        price = 22_010_000_000.0 - step * (22_010_000_000.0 - 21_660_000_000.0) / 84
        records.append((sweep_at + 0.5 + step * 0.0002, "trade",
                        {"m": "BTCTMN", "p": price, "q": 0.3, "bm": True}))
    records.append((sweep_at + 3.1, "trade", {"m": "BTCTMN", "p": 22_018_000_000.0,
                                              "q": 0.1, "bm": False}))
    records.sort(key=lambda record: record[0])
    return records


def make_runner(out_dir) -> runner_mod.Runner:
    """Builds a replay runner with a BTCTMN plan and a short premium warm-up."""
    cfg = config.load_config(None)
    cfg["plan"]["markets"] = ["BTCTMN"]
    cfg["premium"]["min_samples"] = 12
    cfg["bot"]["out_dir"] = str(out_dir)
    return runner_mod.Runner(cfg, mode="replay", store=store_mod.Store(":memory:"),
                             start_ts=0.0, with_twin=False)


def test_replay_of_btctmn_sweep_gives_one_fill_and_exit_at_fair_value(tmp_path) -> None:
    """The 07:03 sweep fills a 1.3% bid near 21.71B and exits at fair value.

    Scenario:
        - Fair value is 21.996B for 200 s. The bid rests at 21.71B.
        - 85 seller trades go from 22.01B down to 21.66B in 17 ms.
        - A buyer pays 22.018B 2.6 s later.
    Expected: one trade, entry 21.71B, exit at fair value, net about +0.82%.
    """
    # given
    runner = make_runner(tmp_path)

    # when
    asyncio.run(runner_mod.replay(runner, sweep_records()))

    # then
    trades = runner.store.rows("trades")
    assert len(trades) == 1
    trade = trades[0]
    assert trade["market"] == "BTCTMN" and trade["mode"] == "replay"
    assert trade["entry_price"] == pytest.approx(FV * 0.987, rel=1e-4)
    assert trade["exit_price"] == pytest.approx(FV, rel=1e-4)
    assert trade["exit_reason"] == "target"
    assert trade["net_pct"] == pytest.approx(0.82, abs=0.03)
    logs = runner.store.rows("logs", "kind = ?", ("trade",))
    assert "BTCTMN: bought" in logs[0]["text"]


def test_replay_with_global_drop_before_the_sweep_has_no_fill(tmp_path) -> None:
    """A 0.5% global drop 10 s before the sweep cancels the bid (global filter)."""
    # given
    runner = make_runner(tmp_path)
    records = []
    for ts, kind, record in sweep_records():
        if kind == "quote" and ts >= 190.0:
            record = dict(record, b=record["b"] * 0.995, a=record["a"] * 0.995)
        records.append((ts, kind, record))

    # when
    asyncio.run(runner_mod.replay(runner, records))

    # then
    assert runner.store.rows("trades") == []
    events = runner.store.rows("filter_events")
    assert [(e["filter"], e["key"], e["kind"]) for e in events][:1] == [
        ("global_drop", "BTC", "start")]


def test_replay_before_start_time_does_not_trade(tmp_path) -> None:
    """Records before start_ts only warm up the state."""
    # given
    runner = make_runner(tmp_path)

    # when
    asyncio.run(runner_mod.replay(runner, sweep_records(), start_ts=210.0))

    # then
    assert runner.store.rows("trades") == []
    assert runner.store.rows("orders") != []
