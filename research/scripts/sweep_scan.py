"""Lists sell sweeps in lagcatcher raw trades.

A sweep here is a burst of seller trades in one market with gaps of 0.3 s or
less. For each burst, the script prints the drop from the best bid just
before the burst to the lowest trade, the volume in toman, and the best bid
5 s and 60 s later (recovery). USDT volume uses a fixed rate of 270,000
toman per USDT. The drop is measured from the best bid, so the distance
from fair value is a little larger.
Usage: python3 -I research/scripts/sweep_scan.py RAW_DIR MIN_DROP_PCT
"""
import collections, datetime, gzip, json, pathlib, sys, zlib
raw = pathlib.Path(sys.argv[1]); min_drop = float(sys.argv[2])
f = lambda t: datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%m-%d %H:%M:%S")
def recs(prefix):
    for p in sorted(raw.glob(f"{prefix}-*.jsonl.gz")):
        try:
            with gzip.open(p, "rt") as fh:
                for l in fh:
                    if l.endswith("\n"): yield json.loads(l)
        except (EOFError, zlib.error): pass
# best bid series per market
bids = collections.defaultdict(list)
for r in recs("wallex_book"):
    if r["s"] == "bid" and r["lv"]:
        bids[r["m"]].append((r["ts"], r["lv"][0][0]))
import bisect
def bid_at(m, t):
    s = bids[m]; i = bisect.bisect_right(s, (t, float("inf"))) - 1
    return s[i][1] if i >= 0 else None
usdt = 270000
bursts = []
cur = {}
for r in recs("wallex_trade"):
    m = r["m"]
    if m == "USDTTMN" or not r["bm"]:
        continue
    b = cur.get(m)
    if b and r["ts"] - b["last"] <= 0.3:
        b["last"] = r["ts"]; b["low"] = min(b["low"], r["p"]); b["vol"] += r["p"] * r["q"]; b["n"] += 1
    else:
        if b: bursts.append(b)
        cur[m] = {"m": m, "start": r["ts"], "last": r["ts"], "low": r["p"], "vol": r["p"] * r["q"], "n": 1}
bursts += [b for b in cur.values()]
out = []
for b in bursts:
    before = bid_at(b["m"], b["start"] - 0.05)
    if not before: continue
    drop = 100 * (1 - b["low"] / before)
    if drop < min_drop: continue
    after5, after60 = bid_at(b["m"], b["last"] + 5), bid_at(b["m"], b["last"] + 60)
    vol = b["vol"] * (1 if b["m"].endswith("TMN") else usdt)
    out.append((b["start"], b["m"], drop, b["n"], vol, 100 * (after5 / before - 1) if after5 else None, 100 * (after60 / before - 1) if after60 else None))
for t, m, d, n, v, a5, a60 in sorted(out):
    print(f"{f(t)} {m:9s} drop {d:5.2f}% trades {n:3d} vol {v/1e6:7.1f}M  bid+5s {a5:+.2f}%  bid+60s {a60:+.2f}%")
print(len(out), "bursts")
