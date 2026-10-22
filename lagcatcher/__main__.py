"""Command line of the lagcatcher shadow test.

Commands:
    python -m lagcatcher check [--config F] [--seconds N]
        Connects each feed for N seconds and prints which ones deliver data.
    python -m lagcatcher run [--config F] [--duration S]
        Runs the shadow test until Ctrl+C, SIGTERM, or S seconds.
    python -m lagcatcher report [--out DIR]
        Prints the report of the session in DIR.
    python -m lagcatcher replay --raw DIR --out DIR [--config F]
        Replays recorded raw data with a grid of strategy settings.
"""

import argparse
import asyncio
import collections
import logging
import os
import time

from lagcatcher import config
from lagcatcher import engine
from lagcatcher import feeds
from lagcatcher import replay
from lagcatcher import report


async def check(cfg: dict, seconds: float) -> bool:
    """Runs every feed for `seconds` and prints a result line per feed.

    Returns:
        True when Wallex and at least `venues.min_venues` venues delivered
        data.
    """
    markets = cfg["markets"]
    trade_markets = ([f"{c}USDT" for c in markets["usdt"]]
                     + [f"{c}TMN" for c in markets["tmn"]])
    coins = sorted({engine.base_of(m) for m in trade_markets})
    stop = asyncio.Event()
    stats = {name: feeds.new_stats() for name in ["wallex"] + cfg["venues"]["enabled"]}
    books, trades = set(), collections.Counter()
    quotes = collections.defaultdict(set)

    def on_book(market, side, levels, now, seed_since):
        books.add((market, side))

    def on_trades(market, items, now):
        trades[market] += len(items)

    def on_quote(venue, coin, bid, ask, now):
        quotes[venue].add(coin)

    tasks = [asyncio.create_task(feeds.run_wallex(
        markets=trade_markets + ["USDTTMN"], on_book=on_book, on_trades=on_trades,
        stats=stats["wallex"], stop=stop))]
    for venue in cfg["venues"]["enabled"]:
        tasks.append(asyncio.create_task(feeds.QUOTE_RUNNERS[venue](
            coins=coins, on_quote=on_quote, stats=stats[venue], stop=stop)))
    await asyncio.sleep(seconds)
    stop.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    sides = len(trade_markets + ["USDTTMN"]) * 2
    wallex_ok = len(books) == sides
    print(f"wallex: {'OK' if wallex_ok else 'FAIL'} book sides {len(books)}/{sides}, "
          f"trades {sum(trades.values())}, msgs {stats['wallex']['msgs']}, "
          f"errors {stats['wallex']['errors']}")
    good_venues = 0
    for venue in cfg["venues"]["enabled"]:
        ok = len(quotes[venue]) >= len(coins) - 1
        good_venues += ok
        missing = sorted(set(coins) - quotes[venue])
        print(f"{venue}: {'OK' if ok else 'FAIL'} coins {len(quotes[venue])}/{len(coins)}"
              f" msgs {stats[venue]['msgs']} errors {stats[venue]['errors']}"
              + (f" missing {','.join(missing)}" if missing else ""))
    return wallex_ok and good_venues >= cfg["venues"]["min_venues"]


def main() -> None:
    """Parses the command line and runs the command."""
    parser = argparse.ArgumentParser(prog="lagcatcher")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "run", "report", "replay"):
        command = commands.add_parser(name)
        command.add_argument("--config", default="config.toml")
    commands.choices["check"].add_argument("--seconds", type=float, default=20.0)
    commands.choices["run"].add_argument("--duration", type=float, default=None)
    commands.choices["report"].add_argument("--out", default=None)
    commands.choices["replay"].add_argument("--raw", required=True)
    commands.choices["replay"].add_argument("--out", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.Formatter.converter = time.gmtime
    cfg = config.load_config(args.config if os.path.exists(args.config) else None)
    if args.command == "check":
        raise SystemExit(0 if asyncio.run(check(cfg, args.seconds)) else 1)
    if args.command == "run":
        asyncio.run(engine.Engine(cfg).run(duration_s=args.duration))
        return
    if args.command == "replay":
        replay.main(args.raw, args.out,
                    args.config if os.path.exists(args.config) else None)
        return
    report.main(args.out or cfg["run"]["out_dir"])


if __name__ == "__main__":
    main()
