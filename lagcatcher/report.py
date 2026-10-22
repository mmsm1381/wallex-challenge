"""Report of a shadow-test session: is the Wallex lag edge real?

The report reads `events.jsonl` and `episodes.jsonl` from an output folder
and prints three tables:

1. Per strategy: signals, fills, misses, fill rate, exits, win rate, median
   and mean net % per trade, and net toman as a percent of the 5M account.
2. The same per strategy and market, for the markets that traded.
3. Gap episodes per market and threshold: count per hour, median and p90
   length in seconds, and median ask depth below the threshold in toman.

The edge counts as real when a realistic-latency strategy has a median net
per trade above +0.05% and enough fills for 10 or more trades a day.
"""

import collections
import json
import math
import pathlib
import statistics

EQUITY_TMN = 5_000_000.0


def _read_jsonl(path: pathlib.Path) -> list[dict]:
    """Returns the records of a JSONL file, or an empty list."""
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as jsonl_file:
        return [json.loads(line) for line in jsonl_file if line.strip()]


def _p90(values: list[float]) -> float:
    """Returns the nearest-rank 90th percentile of a non-empty list."""
    ordered = sorted(values)
    return ordered[math.ceil(0.9 * len(ordered)) - 1]


def _row(name: str, events: list[dict], equity_tmn: float) -> dict:
    """Summarizes the events of one strategy (or one strategy and market)."""
    counts = collections.Counter(event["type"] for event in events)
    exits = [event for event in events if event["type"] == "exit"]
    nets = [event["net_pct"] for event in exits]
    net_tmn = sum(event["net_tmn"] for event in exits if event.get("net_tmn") is not None)
    tries = counts["fill"] + counts["miss"]
    return {
        "strategy": name, "signals": counts["signal"], "fills": counts["fill"],
        "misses": counts["miss"],
        "fill_rate_pct": 100 * counts["fill"] / tries if counts["miss"] else None,
        "exits": len(exits),
        "win_pct": 100 * sum(1 for net in nets if net > 0) / len(nets) if nets else None,
        "median_net_pct": statistics.median(nets) if nets else None,
        "mean_net_pct": statistics.fmean(nets) if nets else None,
        "net_tmn": net_tmn, "net_pct_of_equity": 100 * net_tmn / equity_tmn,
    }


def summarize(events: list[dict], equity_tmn: float) -> list[dict]:
    """Returns one summary row per strategy, in name order."""
    by_strategy = collections.defaultdict(list)
    for event in events:
        by_strategy[event["strategy"]].append(event)
    return [_row(name, by_strategy[name], equity_tmn) for name in sorted(by_strategy)]


def summarize_by_market(events: list[dict], equity_tmn: float) -> list[dict]:
    """Returns one row per strategy and market that had at least one fill."""
    groups = collections.defaultdict(list)
    for event in events:
        groups[(event["strategy"], event["market"])].append(event)
    rows = []
    for (name, market), group in sorted(groups.items()):
        row = _row(name, group, equity_tmn)
        if row["fills"]:
            row["market"] = market
            rows.append(row)
    return rows


def summarize_episodes(closed: list[dict], hours: float) -> list[dict]:
    """Returns one row per market and threshold, most frequent first."""
    groups = collections.defaultdict(list)
    for episode in closed:
        groups[(episode["market"], episode["threshold_pct"])].append(episode)
    rows = []
    for (market, threshold), group in groups.items():
        durations = [episode["duration_s"] for episode in group]
        depths = [episode["depth_tmn_at_max"] for episode in group
                  if episode.get("depth_tmn_at_max") is not None]
        rows.append({
            "market": market, "threshold_pct": threshold, "count": len(group),
            "per_hour": len(group) / hours if hours else None,
            "median_duration_s": statistics.median(durations),
            "p90_duration_s": _p90(durations),
            "median_depth_tmn": statistics.median(depths) if depths else None,
        })
    rows.sort(key=lambda row: row["count"], reverse=True)
    return rows


def _fmt(value, digits: int = 3) -> str:
    """Formats a number for the tables, or "-" for None."""
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _table(rows: list[dict], columns: list[str]) -> str:
    """Returns a plain text table of `rows`."""
    cells = [[_fmt(row.get(column)) for column in columns] for row in rows]
    widths = [max([len(column)] + [len(line[i]) for line in cells])
              for i, column in enumerate(columns)]
    lines = ["  ".join(column.rjust(width) for column, width in zip(columns, widths))]
    lines += ["  ".join(cell.rjust(width) for cell, width in zip(line, widths))
              for line in cells]
    return "\n".join(lines)


def main(out_dir: str) -> None:
    """Prints the report of the session in `out_dir`."""
    folder = pathlib.Path(out_dir)
    events = _read_jsonl(folder / "events.jsonl")
    closed = _read_jsonl(folder / "episodes.jsonl")
    times = [event["ts"] for event in events] + [e["end_ts"] for e in closed]
    starts = [e["start_ts"] for e in closed]
    span_h = (max(times) - min(times + starts)) / 3600 if times else 0.0
    print(f"Session: {len(events)} events, {len(closed)} gap episodes, "
          f"about {span_h:.2f} hours with activity.\n")
    columns = ["strategy", "signals", "fills", "misses", "fill_rate_pct", "exits",
               "win_pct", "median_net_pct", "mean_net_pct", "net_tmn",
               "net_pct_of_equity"]
    print("Per strategy (1M toman per trade, net after real fees):")
    print(_table(summarize(events, EQUITY_TMN), columns))
    print("\nPer strategy and market:")
    print(_table(summarize_by_market(events, EQUITY_TMN), ["market"] + columns))
    print("\nGap episodes (best ask below fair value by the threshold):")
    print(_table(summarize_episodes(closed, span_h)[:30],
                 ["market", "threshold_pct", "count", "per_hour",
                  "median_duration_s", "p90_duration_s", "median_depth_tmn"]))
