"""Daily Markdown report of the sweep bot.

The report covers one Tehran day. It compares the bot modes with the twin
and lists the trades, plans, filters, AI cost and decision log. See DESIGN
sections 8.4 and 10.
"""

import calendar
import datetime
import os
import statistics

from sweepbot import risk

_DAY_S = 86400
_NONE = "None."


def day_bounds(day: str) -> tuple[float, float]:
    """Returns the start and end of a Tehran day in UTC epoch seconds.

    Args:
        day: The Tehran date as "YYYY-MM-DD".

    Returns:
        The tuple (start, end). The day is the range [start, end).
    """
    parsed = datetime.datetime.strptime(day, "%Y-%m-%d")
    start = calendar.timegm(parsed.timetuple()) - risk.TEHRAN_OFFSET_S
    return float(start), float(start + _DAY_S)


def mode_summary(trades: list[dict]) -> dict:
    """Returns the numbers of the trades of one mode.

    Args:
        trades: Trade rows in time order. A `net_tmn` of None counts as 0.

    Returns:
        A dict with trades, wins, win_pct, median_pct, net_tmn, worst_tmn
        and max_drawdown_tmn. The drawdown is the most negative dip of the
        cumulative net below its running peak, or 0 if it never dips.
    """
    nets = [t.get("net_tmn") or 0.0 for t in trades]
    pcts = [t.get("net_pct") or 0.0 for t in trades]
    total = 0.0
    peak = 0.0
    drawdown = 0.0
    for net in nets:
        total += net
        peak = max(peak, total)
        drawdown = min(drawdown, total - peak)
    wins = sum(1 for net in nets if net > 0)
    return {
        "trades": len(trades),
        "wins": wins,
        "win_pct": 100.0 * wins / len(trades) if trades else 0.0,
        "median_pct": statistics.median(pcts) if pcts else 0.0,
        "net_tmn": total,
        "worst_tmn": min(nets) if nets else 0.0,
        "max_drawdown_tmn": drawdown,
    }


def _tmn(value: float | None) -> str:
    """Formats toman with thousands separators and no decimals."""
    return f"{(value or 0.0):,.0f}"


def _clock(ts: float) -> str:
    """Returns the UTC time of `ts` as "HH:MM:SS"."""
    return datetime.datetime.fromtimestamp(
        ts, tz=datetime.timezone.utc).strftime("%H:%M:%S")


def _price(value: float | None) -> str:
    """Formats a price without trailing noise."""
    return "" if value is None else f"{value:,.8g}"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    """Returns the lines of a Markdown table."""
    lines = ["| " + " | ".join(header) + " |",
             "|" + "|".join("---" for _ in header) + "|"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def _bot_section(trades: list[dict], modes: tuple[str, ...]) -> list[str]:
    """Returns the lines of the "Bot and twin" section."""
    summaries = {}
    for mode in modes:
        mode_trades = [t for t in trades if t["mode"] == mode]
        if mode_trades:
            summaries[mode] = mode_summary(mode_trades)
    if not summaries:
        return [_NONE]
    header = ["mode", "trades", "wins", "win %", "median %", "net toman",
              "worst toman", "max drawdown toman"]
    rows = [[mode, str(s["trades"]), str(s["wins"]), f"{s['win_pct']:.2f}",
             f"{s['median_pct']:.2f}", _tmn(s["net_tmn"]),
             _tmn(s["worst_tmn"]), _tmn(s["max_drawdown_tmn"])]
            for mode, s in summaries.items()]
    lines = _table(header, rows)
    other = next((m for m in summaries if m != "twin"), None)
    if "twin" in summaries and other is not None:
        effect = summaries[other]["net_tmn"] - summaries["twin"]["net_tmn"]
        lines += ["", f"AI effect: {_tmn(effect)} toman"]
    return lines


def _trades_section(trades: list[dict]) -> list[str]:
    """Returns the lines of the "Trades" section."""
    if not trades:
        return [_NONE]
    header = ["time UTC", "mode", "market", "entry price", "exit price",
              "net %", "net toman", "hold s", "exit reason"]
    rows = [[_clock(t["ts"]), t["mode"], t["market"],
             _price(t["entry_price"]), _price(t["exit_price"]),
             f"{(t['net_pct'] or 0.0):.2f}", _tmn(t["net_tmn"]),
             f"{(t['hold_s'] or 0.0):.0f}", t["exit_reason"] or ""]
            for t in trades]
    return _table(header, rows)


def _plans_section(plans: list[dict]) -> list[str]:
    """Returns the lines of the "Plans" section."""
    if not plans:
        return [_NONE]
    accepted = sum(1 for p in plans if p["accepted"])
    return [f"Plans: {len(plans)} (accepted {accepted}, "
            f"rejected {len(plans) - accepted})"]


def _filters_section(events: list[dict]) -> list[str]:
    """Returns the lines of the "Filters" section."""
    counts: dict[str, int] = {}
    for event in events:
        if event["kind"] == "start":
            counts[event["filter"]] = counts.get(event["filter"], 0) + 1
    if not counts:
        return [_NONE]
    return [f"* {name}: {count}" for name, count in sorted(counts.items())]


def _ai_section(store, calls: list[dict]) -> list[str]:
    """Returns the lines of the "AI" section."""
    errors = sum(1 for c in calls if c["error"])
    cost = sum(c["cost_usd"] or 0.0 for c in calls)
    lines = [f"* Calls: {len(calls)}",
             f"* Errors: {errors}",
             f"* Cost that day: {cost:.2f} USD",
             f"* Cost total: {store.ai_cost_total():.2f} USD"]
    if not calls:
        return [_NONE] + [""] + lines[3:]
    return lines


def _log_section(logs: list[dict]) -> list[str]:
    """Returns the lines of the "Decision log" section."""
    if not logs:
        return [_NONE]
    return [f"- {_clock(r['ts'])} UTC [{r['kind']}] {r['text']}"
            for r in logs]


def daily_report(store, day: str,
                 modes: tuple[str, ...] = ("live", "paper", "replay", "twin")) -> str:
    """Builds the Markdown report of one Tehran day.

    Args:
        store: The `Store` to read from.
        day: The Tehran date as "YYYY-MM-DD".
        modes: The modes to show, in table order.

    Returns:
        The report as Markdown text.
    """
    start, end = day_bounds(day)
    where, params = "ts >= ? AND ts < ?", (start, end)
    trades = store.rows("trades", where, params)
    sections = [
        ("Bot and twin", _bot_section(trades, modes)),
        ("Trades", _trades_section(trades)),
        ("Plans", _plans_section(store.rows("plans", where, params))),
        ("Filters", _filters_section(
            store.rows("filter_events", where, params))),
        ("AI", _ai_section(store, store.rows("ai_calls", where, params))),
        ("Decision log", _log_section(store.rows("logs", where, params))),
    ]
    lines = [f"# Daily report {day} (Tehran)"]
    for title, body in sections:
        lines += ["", f"## {title}", ""] + body
    return "\n".join(lines) + "\n"


def write_daily_report(store, day: str, out_dir: str) -> str:
    """Writes the report of a day to `out_dir/report-YYYY-MM-DD.md`.

    Args:
        store: The `Store` to read from.
        day: The Tehran date as "YYYY-MM-DD".
        out_dir: The output folder. It is created when missing.

    Returns:
        The path of the written file.
    """
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"report-{day}.md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(daily_report(store, day))
    return path
