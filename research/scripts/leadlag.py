"""Does Binance move first? Event study on recorded global and Wallex mids.

For each coin, an event is a Binance mid move of at least MOVE_BPS within
1 s. For each other venue and for the Wallex USDT market, the script
measures how much of that move the venue made by +0.25 s ... +60 s after
the event (1.0 = the full Binance move). Quotes are recorded at most every
250 ms per venue and coin, so finer lead times are not visible.
Usage: python leadlag.py RAW_DIR START_UNIX [MOVE_BPS]
"""
import bisect
import collections
import gzip
import json
import pathlib
import statistics
import sys
import zlib

raw = pathlib.Path(sys.argv[1])
start = float(sys.argv[2])
move_bps = float(sys.argv[3]) if len(sys.argv) > 3 else 10.0
HORIZONS = [0.25, 0.5, 1, 2, 5, 10, 30, 60]


def records(prefix):
    for path in sorted(raw.glob(f"{prefix}-*.jsonl.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            try:
                for line in fh:
                    if line.endswith("\n"):
                        yield json.loads(line)
            except (EOFError, zlib.error):
                pass


series = collections.defaultdict(lambda: ([], []))  # (venue, coin) -> (ts, mid)
for r in records("global"):
    if r["ts"] >= start and r["b"] > 0 and r["a"] >= r["b"]:
        ts, mids = series[(r["v"], r["c"])]
        ts.append(r["ts"])
        mids.append((r["b"] + r["a"]) / 2)
best = collections.defaultdict(dict)
for r in records("wallex_book"):
    m = r["m"]
    if not m.endswith("USDT") or m == "USDTTMN":
        continue
    best[m][r["s"]] = r["lv"][0][0] if r["lv"] else None
    b, a = best[m].get("bid"), best[m].get("ask")
    if r["ts"] >= start and b and a and a >= b:
        ts, mids = series[("wallex", m[:-4])]
        ts.append(r["ts"])
        mids.append((b + a) / 2)


def value_at(key, t):
    ts, mids = series[key]
    i = bisect.bisect_right(ts, t) - 1
    return mids[i] if i >= 0 else None


import os
coins = sorted({c for v, c in series if v == "binance"} & set(os.environ.get("COINS", "BTC,ETH,SOL,XRP,BNB,DOGE").split(",")))
venues = ["bybit", "okx", "kucoin", "gate", "wallex"]
share = collections.defaultdict(list)  # (venue, horizon) -> moved share
n_events = 0
for coin in coins:
    ts, mids = series[("binance", coin)]
    last_event = 0.0
    j = 0
    for i in range(len(ts)):
        while ts[i] - ts[j] > 1.0:
            j += 1
        move = mids[i] / mids[j] - 1
        if abs(move) * 1e4 < move_bps or ts[i] - last_event < 60:
            continue
        last_event = t0 = ts[i]
        n_events += 1
        for venue in venues:
            if (venue, coin) not in series:
                continue
            before = value_at((venue, coin), ts[j] - 0.01)
            if before is None:
                continue
            for h in HORIZONS:
                after = value_at((venue, coin), t0 + h)
                if after is not None:
                    share[(venue, h)].append((after / before - 1) / move)
        # Binance itself after the event, to see if the move holds.
        for h in HORIZONS:
            after = value_at(("binance", coin), t0 + h)
            share[("binance", h)].append((after / mids[j] - 1) / move)

print(f"{n_events} Binance moves of {move_bps:g} bps or more within 1 s")
print("Median share of the Binance move made by each venue after t seconds:")
print("venue    " + "".join(f"{h:>7g}s" for h in HORIZONS))
for venue in ["binance"] + venues:
    row = []
    for h in HORIZONS:
        values = share[(venue, h)]
        row.append(f"{statistics.median(values):8.2f}" if values else "       -")
    print(f"{venue:8s} " + "".join(row) + f"   (n={len(share[(venue, 1)])})")
