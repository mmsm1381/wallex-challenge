"""Replay of recorded raw data through the engine and the paper strategies.

The live run records the Wallex books and trades and the global quotes
(see `lagcatcher.recorder`). The replay reads those files in time order and
gives each record to an `Engine`, with a simulated clock for the timers and
the premium samples. So we can test new settings, such as a lower gap
hurdle, on real data without a new live run.

`build_variants` makes a grid of taker and resting-bid strategies with
different safety buffers. `main` runs the grid and prints the report.

Limits: the global quotes are recorded at most every 250 ms per venue and
coin, and the books keep the top 10 levels, so the replay is a little less
exact than the live engine.
"""

import collections
import gzip
import heapq
import json
import logging
import pathlib
import statistics
import zlib
from collections.abc import Iterable, Iterator

from lagcatcher import config
from lagcatcher import engine
from lagcatcher import report
from lagcatcher import shadow

STREAMS = {"wallex_book": "book", "wallex_trade": "trade", "global": "quote"}


def iter_jsonl_gz(path: str | pathlib.Path) -> Iterator[dict]:
    """Yields the records of a gzip JSONL file.

    The live recorder flushes the current hourly file but does not close
    it. Such a file has no gzip end marker, so the reader stops at the last
    complete line instead of failing.
    """
    with gzip.open(path, "rt", encoding="utf-8") as jsonl_file:
        try:
            for line in jsonl_file:
                if line.endswith("\n"):
                    yield json.loads(line)
        except (EOFError, zlib.error):
            return


def load_records(raw_dir: str) -> Iterator[tuple[float, str, dict]]:
    """Yields (ts, kind, record) of all raw streams, merged in time order."""
    folder = pathlib.Path(raw_dir)

    def stream(prefix: str, kind: str) -> Iterator[tuple[float, str, dict]]:
        for path in sorted(folder.glob(f"{prefix}-*.jsonl.gz")):
            for record in iter_jsonl_gz(path):
                yield (record["ts"], kind, record)

    return heapq.merge(*(stream(prefix, kind) for prefix, kind in STREAMS.items()),
                       key=lambda item: item[0])


def run(live: engine.Engine, records: Iterable[tuple[float, str, dict]],
        timer_step_s: float = 0.1) -> None:
    """Gives each record to the engine and runs the timers on replay time.

    Before each record, the replay runs every timer tick and premium sample
    that is due at or before the record time. So a strategy latency of
    0.3 s ends between the right records.
    """
    premium_step_s = live.cfg["premium"]["sample_s"]
    next_timer = next_premium = None
    for ts, kind, record in records:
        if next_timer is None:
            next_timer = ts
            next_premium = ts + premium_step_s
        while next_premium <= ts:
            live.sample_premiums(next_premium)
            next_premium += premium_step_s
        while next_timer <= ts:
            live.tick_timers(next_timer)
            next_timer += timer_step_s
        if kind == "book":
            live.stats["wallex"]["last"] = ts
            live.on_book(record["m"], record["s"],
                         [tuple(level) for level in record["lv"]], ts, None)
        elif kind == "trade":
            live.stats["wallex"]["last"] = ts
            live.on_trades(record["m"], [(record["p"], record["q"], record["bm"],
                                          record.get("tts"))], ts)
        else:
            live.on_quote(record["v"], record["c"], record["b"], record["a"], ts)


def build_variants(cfg: dict, buffers_pct: list[float], latencies_s: list[float],
                   resting_buffers_pct: list[float]) -> list:
    """Builds one taker strategy per (latency, buffer) and one resting per buffer.

    The taker hurdle is the round-trip fee (taker in, maker out) plus the
    buffer, with no extra floor. For USDT pairs a 0 buffer means 0.23%.
    """
    fees = {"TMN": config.fee_rates("TMN", cfg), "USDT": config.fee_rates("USDT", cfg)}
    exit_cfg = cfg["exit"]
    paper = cfg["paper"]

    def rules(latency: float) -> shadow.ExitRules:
        return shadow.ExitRules(max_hold_s=exit_cfg["max_hold_s"],
                                stop_pct=exit_cfg["stop_pct"],
                                requote_bps=exit_cfg["requote_bps"], latency_s=latency,
                                stop_confirm_s=exit_cfg["stop_confirm_s"],
                                cooldown_s=exit_cfg["cooldown_s"])

    def bucket() -> shadow.TokenBucket:
        return shadow.TokenBucket(capacity=cfg["limits"]["orders_per_10s"], per_s=10.0)

    variants: list = []
    for latency in latencies_s:
        for buffer in buffers_pct:
            variants.append(shadow.TakerStrategy(
                name=f"taker_{latency:g}s_b{buffer:g}", latency_s=latency,
                notional_tmn=paper["notional_tmn"], min_gap_pct=0.0,
                buffer_pct=buffer, fees=fees, exit_rules=rules(latency),
                bucket=bucket(), min_fill_fraction=paper["min_fill_fraction"],
                max_entry_spread_pct=paper["max_entry_spread_pct"]))
    for buffer in resting_buffers_pct:
        variants.append(shadow.RestingBidStrategy(
            name=f"resting_b{buffer:g}", latency_s=cfg["resting"]["latency_s"],
            notional_tmn=paper["notional_tmn"], buffer_pct=buffer,
            requote_bps=cfg["resting"]["requote_bps"], fees=fees,
            exit_rules=rules(cfg["resting"]["latency_s"]), bucket=bucket(),
            min_fill_fraction=paper["min_fill_fraction"],
            max_entry_spread_pct=paper["max_entry_spread_pct"]))
    return variants


def capture_rows(events: list[dict]) -> list[dict]:
    """Returns, per taker strategy, how much of the signal gap the trades kept.

    Capture = gross move from entry to exit / gap at the signal. A capture
    of 1.0 means the price came back all the way to fair value.
    """
    gaps: dict[tuple[str, str], float] = {}
    captures = collections.defaultdict(list)
    for event in events:
        key = (event["strategy"], event["market"])
        if event["type"] == "fill" and "gap_pct" in event:
            gaps[key] = event["gap_pct"]
        elif event["type"] == "exit" and key in gaps:
            gross_pct = 100 * (event["price"] / event["entry_price"] - 1)
            captures[event["strategy"]].append(gross_pct / gaps.pop(key))
    return [{"strategy": name, "trades": len(values),
             "median_capture": statistics.median(values)}
            for name, values in sorted(captures.items())]


def main(raw_dir: str, out_dir: str, config_path: str | None) -> None:
    """Replays `raw_dir` with the variant grid and prints the results."""
    logging.getLogger("lagcatcher").setLevel(logging.WARNING)
    cfg = config.load_config(config_path)
    cfg["run"]["out_dir"] = out_dir
    cfg["venues"]["enabled"] = ["bybit", "okx", "kucoin", "gate", "binance"]
    cfg["episodes"]["thresholds_pct"] = [0.15, 0.25, 0.35, 0.5]
    pathlib.Path(out_dir).mkdir(parents=True, exist_ok=True)
    for name in ("events.jsonl", "episodes.jsonl"):
        (pathlib.Path(out_dir) / name).unlink(missing_ok=True)
    variants = build_variants(cfg, buffers_pct=[0.0, 0.05, 0.12, 0.22, 0.37],
                              latencies_s=[0.3, 1.0],
                              resting_buffers_pct=[0.05, 0.15, 0.3])
    live = engine.Engine(cfg, strategies=variants)
    live.recorder.raw_enabled = False
    run(live, load_records(raw_dir))
    live.recorder.flush()
    live.recorder.close()
    report.main(out_dir)
    events = report._read_jsonl(pathlib.Path(out_dir) / "events.jsonl")
    print("\nHow much of the gap the taker trades kept (1.0 = all the way to fair value):")
    print(report._table(capture_rows(events), ["strategy", "trades", "median_capture"]))
