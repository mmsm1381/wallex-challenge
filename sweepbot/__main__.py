"""Command line of the sweep bot.

Commands:
    python -m sweepbot replay --raw DIR [--config F] [--start T] [--end T]
            [--markets A,B] [--depth PCT] [--depth-usdt PCT] [--db F] [--no-twin]
        Replays lagcatcher raw files with the paper broker and prints the
        trades. T is ISO 8601 UTC, such as 2026-10-22T07:00:00Z.
    python -m sweepbot run --mode paper [--config F] [--duration S] [--ai]
        Runs the bot on the live feeds with the paper broker and the twin.
        --ai turns the Claude supervisor on (needs ANTHROPIC_API_KEY).
    python -m sweepbot report --day YYYY-MM-DD [--config F] [--db F]
        Prints the daily report of a Tehran day from the SQLite store.
"""

import argparse
import asyncio
import itertools
import logging
import os
import time

from lagcatcher import replay as lag_replay
from sweepbot import config
from sweepbot import report
from sweepbot import risk
from sweepbot import runner as runner_mod
from sweepbot import store as store_mod

log = logging.getLogger("sweepbot")


def _load(path: str) -> dict:
    """Loads the config file when it exists, else the defaults."""
    return config.load_config(path if os.path.exists(path) else None)


def trade_table(trades: list[dict]) -> str:
    """Returns a plain text table of closed trades."""
    lines = [f"{'exit UTC':19}  {'mode':6}  {'market':9}  {'entry':>14}  {'exit':>14}"
             f"  {'net %':>7}  {'net toman':>10}  {'hold s':>7}  reason"]
    for trade in trades:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(trade["ts"]))
        net_tmn = trade["net_tmn"] if trade["net_tmn"] is not None else float("nan")
        lines.append(f"{stamp:19}  {trade['mode']:6}  {trade['market']:9}"
                     f"  {trade['entry_price']:14.8g}  {trade['exit_price']:14.8g}"
                     f"  {trade['net_pct']:+7.2f}  {net_tmn:+10,.0f}"
                     f"  {trade['hold_s']:7.1f}  {trade['exit_reason']}")
    return "\n".join(lines)


def cmd_replay(args: argparse.Namespace) -> None:
    """Runs the replay command."""
    cfg = _load(args.config)
    cfg["bot"]["mode"] = "replay"
    cfg["bot"]["out_dir"] = args.out
    if args.markets:
        cfg["plan"]["markets"] = args.markets.split(",")
    if args.depth is not None:
        cfg["plan"]["depth_pct_tmn"] = args.depth
    if args.depth_usdt is not None:
        cfg["plan"]["depth_pct_usdt"] = args.depth_usdt
    config.validate(cfg)
    start = risk.parse_utc(args.start) if args.start else None
    end = risk.parse_utc(args.end) if args.end else None
    records = lag_replay.load_records(args.raw)
    first = next(records, None)
    if first is None:
        raise SystemExit(f"no raw records in {args.raw}")
    store = store_mod.Store(args.db)
    runner = runner_mod.Runner(cfg, mode="replay", store=store,
                               start_ts=start or first[0], with_twin=not args.no_twin)
    began = time.time()
    asyncio.run(runner_mod.replay(runner, itertools.chain([first], records),
                                  start_ts=start, end_ts=end))
    trades = store.rows("trades")
    print(trade_table(trades))
    for mode in sorted({trade["mode"] for trade in trades}):
        summary = report.mode_summary([t for t in trades if t["mode"] == mode])
        print(f"{mode}: {summary}")
    print(f"replay took {time.time() - began:.0f} s, store {args.db}")


def cmd_run(args: argparse.Namespace) -> None:
    """Runs the bot on the live feeds."""
    cfg = _load(args.config)
    if args.mode == "live":
        raise SystemExit("live mode needs the WallexBroker of milestone 2 "
                         "(docs/IMPLEMENTATION_PLAN.md). Use --mode paper.")
    cfg["bot"]["mode"] = args.mode
    store = store_mod.Store(cfg["bot"]["db_path"])
    supervisor = None
    if args.ai or cfg["ai"]["enabled"]:
        from sweepbot.ai import claude
        from sweepbot.ai import supervisor as supervisor_mod
        holder: dict = {}
        supervisor = supervisor_mod.Supervisor(
            cfg, claude.ClaudeProvider(cfg["ai"]), store,
            equity_fn=lambda now: holder["runner"].bot.equity(now))
    os.makedirs(cfg["bot"]["out_dir"], exist_ok=True)
    runner = runner_mod.Runner(cfg, mode=args.mode, store=store, start_ts=time.time(),
                               supervisor=supervisor, record_raw=True)
    if supervisor is not None:
        holder["runner"] = runner
    asyncio.run(runner_mod.run_live(runner, duration_s=args.duration))


def cmd_report(args: argparse.Namespace) -> None:
    """Prints the daily report of one Tehran day."""
    cfg = _load(args.config)
    print(report.daily_report(store_mod.Store(args.db or cfg["bot"]["db_path"]), args.day))


def main() -> None:
    """Parses the command line and runs the command."""
    parser = argparse.ArgumentParser(prog="sweepbot")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("replay", "run", "report"):
        commands.add_parser(name).add_argument("--config", default="config.sweep.toml")
    replay = commands.choices["replay"]
    replay.add_argument("--raw", required=True, help="lagcatcher raw folder")
    replay.add_argument("--out", default="data/sweepbot-replay")
    replay.add_argument("--start", default=None)
    replay.add_argument("--end", default=None)
    replay.add_argument("--markets", default=None, help="comma list, replaces plan.markets")
    replay.add_argument("--depth", type=float, default=None, help="depth of toman markets")
    replay.add_argument("--depth-usdt", type=float, default=None, help="depth of USDT markets")
    replay.add_argument("--db", default=":memory:")
    replay.add_argument("--no-twin", action="store_true")
    run = commands.choices["run"]
    run.add_argument("--mode", choices=["paper", "live"], default="paper")
    run.add_argument("--duration", type=float, default=None)
    run.add_argument("--ai", action="store_true")
    report_cmd = commands.choices["report"]
    report_cmd.add_argument("--day", required=True)
    report_cmd.add_argument("--db", default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.Formatter.converter = time.gmtime
    {"replay": cmd_replay, "run": cmd_run, "report": cmd_report}[args.command](args)


if __name__ == "__main__":
    main()
