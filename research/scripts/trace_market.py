"""Traces one market through a replay: book, venue mids, premium, gap.

Usage (from the repo root, with PYTHONPATH=.): python research/scripts/trace_market.py RAW_DIR CONFIG MARKET [EVERY_S] [FROM_HHMM]
"""
import datetime
import logging
import math
import statistics
import sys
import tempfile

from lagcatcher import config, engine, replay

raw_dir, cfg_path, market = sys.argv[1:4]
every = float(sys.argv[4]) if len(sys.argv) > 4 else 30.0
start_hhmm = sys.argv[5] if len(sys.argv) > 5 else "00:00"

logging.getLogger("lagcatcher").setLevel(logging.WARNING)
cfg = config.load_config(cfg_path)
cfg["run"]["out_dir"] = tempfile.mkdtemp()
live = engine.Engine(cfg, strategies=[])
live.recorder.raw_enabled = False
coin = engine.base_of(market)
rows = []
orig = live.sample_premiums
last_row = [0.0]


def hm(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%H:%M:%S")


def sample(now):
    orig(now)
    if now - last_row[0] < every:
        return
    last_row[0] = now
    book = live.books.get(market)
    venues = live.quotes._quotes.get(coin, {})
    parts = []
    for name in ("bybit", "okx", "kucoin", "gate"):
        if name in venues:
            b, a, ts = venues[name]
            parts.append(f"{name[:2]}={(b + a) / 2:.6g}{'' if now - ts <= 10 else '(old)'}")
    gmid = live.quotes.global_mid(coin, now)
    raw = live.raw_fair_value(market, now)
    prem = live.premiums[market].value(now)
    fv = live.fair_value(market, now)
    bid = book.best_bid() if book else None
    ask = book.best_ask() if book else None
    u = live.usdttmn_mid()
    line = f"{hm(now)} "
    if bid and ask:
        mid = (bid + ask) / 2
        line += f"bid={bid:.6g} ask={ask:.6g} spr={100 * (ask / bid - 1):.2f}% "
        if raw:
            line += f"basis={100 * math.log(mid / raw):+.2f}% "
    line += f"prem={'-' if prem is None else f'{100 * prem:+.2f}%'} "
    if fv and ask:
        line += f"gap_ask={100 * (1 - ask / fv):+.2f}% "
    line += f"usdttmn={u} gmid={gmid and f'{gmid:.6g}'} " + " ".join(parts)
    if hm(now)[:5] >= start_hhmm:
        print(line)


live.sample_premiums = sample
replay.run(live, replay.load_records(raw_dir))
