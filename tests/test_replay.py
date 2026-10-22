"""Tests for the replay of recorded data through the engine."""

import gzip
import shutil

from lagcatcher import config
from lagcatcher import engine
from lagcatcher import replay


def test_iter_jsonl_gz_with_unfinished_file_returns_flushed_records(tmp_path) -> None:
    """The live recorder flushes but does not close the current hourly file."""
    # given
    live_path = tmp_path / "live.jsonl.gz"
    copy_path = tmp_path / "copy.jsonl.gz"
    writer = gzip.open(live_path, "at", encoding="utf-8")
    writer.write('{"ts": 1.0}\n{"ts": 2.0}\n')
    writer.flush()
    shutil.copy(live_path, copy_path)
    writer.close()

    # when
    records = list(replay.iter_jsonl_gz(copy_path))

    # then
    assert records == [{"ts": 1.0}, {"ts": 2.0}]


def test_replay_with_cheap_ask_gives_signal_and_fill_after_latency(tmp_path) -> None:
    """The XRPUSDT ask falls 2% below fair value at t=10. It is still there at t=10.5.

    Scenario:
        - Book and two venue quotes at t=0 give a fair mid of 1.0. USDTTMN
          sizes the 1M toman trade in USDT.
        - The premium sample at t=5 is 0, so fair value is 1.0.
        - At t=10 the ask falls to 0.98. A quote at t=10.5 moves the clock.
    Expected: taker_0.3s records one signal and one fill.
    """
    # given
    cfg = config.load_config(path=None)
    cfg["run"]["out_dir"] = str(tmp_path)
    cfg["premium"]["min_samples"] = 1
    live = engine.Engine(cfg)
    records = [
        (0.0, "book", {"m": "USDTTMN", "s": "bid", "lv": [[249999.0, 1e6]]}),
        (0.0, "book", {"m": "USDTTMN", "s": "ask", "lv": [[250001.0, 1e6]]}),
        (0.0, "book", {"m": "XRPUSDT", "s": "bid", "lv": [[0.999, 1e6]]}),
        (0.0, "book", {"m": "XRPUSDT", "s": "ask", "lv": [[1.001, 1e6]]}),
        (0.0, "quote", {"v": "bybit", "c": "XRP", "b": 0.99, "a": 1.01}),
        (0.0, "quote", {"v": "kucoin", "c": "XRP", "b": 0.99, "a": 1.01}),
        (6.0, "quote", {"v": "bybit", "c": "XRP", "b": 0.99, "a": 1.01}),
        (10.0, "quote", {"v": "kucoin", "c": "XRP", "b": 0.99, "a": 1.01}),
        (10.0, "book", {"m": "XRPUSDT", "s": "ask", "lv": [[0.98, 1e6]]}),
        (10.5, "quote", {"v": "bybit", "c": "XRP", "b": 0.99, "a": 1.01}),
    ]

    # when
    replay.run(live, records)

    # then
    assert live.counts["taker_0.3s"]["signal"] == 1
    assert live.counts["taker_0.3s"]["fill"] == 1
