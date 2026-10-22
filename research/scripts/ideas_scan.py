"""Quick scans for new ideas on the raw Wallex data.

1. Cross-market loop: coin TMN vs coin USDT x USDTTMN, at the touch.
2. Trade flow per market: trades per hour, toman volume, share at bid/ask.
Usage: python ideas_scan.py RAW_DIR
"""
import collections, gzip, json, pathlib, statistics, sys, zlib

raw = pathlib.Path(sys.argv[1])
def recs(prefix):
    for p in sorted(raw.glob(f"{prefix}-*.jsonl.gz")):
        try:
            with gzip.open(p, "rt") as fh:
                for l in fh:
                    if l.endswith("\n"): yield json.loads(l)
        except (EOFError, zlib.error): pass

COINS = ["BTC","ETH","XRP","SOL","PEPE","DOGE","BNB","ADA"]
best = collections.defaultdict(dict)
loopA = collections.defaultdict(list); loopB = collections.defaultdict(list); dev = collections.defaultdict(list)
usdt_series = []
next_s = 0
for r in recs("wallex_book"):
    best[r["m"]][r["s"]] = r["lv"][0][0] if r["lv"] else None
    if r["ts"] < next_s: continue
    next_s = r["ts"] + 1.0
    u = best["USDTTMN"]; ub, ua = u.get("bid"), u.get("ask")
    if not (ub and ua): continue
    usdt_series.append((r["ts"], (ub+ua)/2))
    for c in COINS:
        t, d = best[c+"TMN"], best[c+"USDT"]
        if not all((t.get("bid"), t.get("ask"), d.get("bid"), d.get("ask"))): continue
        # A: TMN->coin(TMN ask)->USDT(USDT bid)->TMN(USDTTMN bid)
        loopA[c].append(100*(d["bid"]*ub/t["ask"]-1))
        loopB[c].append(100*(t["bid"]/(ua*d["ask"])-1))
        dev[c].append(100*(((t["bid"]+t["ask"])/2)/(((d["bid"]+d["ask"])/2)*(ub+ua)/2)-1))
print("Loop gross % at the touch, sampled 1/s (taker cost 0.73%, maker-ish ~0.6%)")
print(f"{'coin':5s} {'n':>6s} {'A p50':>6s} {'A p99':>6s} {'A>0.73':>7s} {'B p50':>6s} {'B p99':>6s} {'B>0.73':>7s} {'mid dev p5/p50/p95':>20s}")
for c in COINS:
    a, b, m = sorted(loopA[c]), sorted(loopB[c]), sorted(dev[c])
    if not a: continue
    q = lambda v, x: v[int(x*(len(v)-1))]
    print(f"{c:5s} {len(a):6d} {q(a,.5):6.2f} {q(a,.99):6.2f} {100*sum(x>0.73 for x in a)/len(a):6.2f}% {q(b,.5):6.2f} {q(b,.99):6.2f} {100*sum(x>0.73 for x in b)/len(b):6.2f}%  {q(m,.05):+.2f}/{q(m,.5):+.2f}/{q(m,.95):+.2f}")

# Trade flow.
first = last = None
flow = collections.defaultdict(lambda: [0, 0.0, 0, 0])
u_mid = usdt_series[-1][1] if usdt_series else 260000
for r in recs("wallex_trade"):
    first = first or r["ts"]; last = r["ts"]
    f = flow[r["m"]]
    f[0] += 1
    f[1] += r["p"]*r["q"]*(1 if r["m"].endswith("TMN") else u_mid)
    f[2 if r["bm"] else 3] += 1
hours = (last-first)/3600
print(f"\nTrade flow over {hours:.1f} h (toman volume; 'sells' = seller hit the bid)")
for m, (n, vol, sells, buys) in sorted(flow.items(), key=lambda kv: -kv[1][1]):
    print(f"{m:9s} trades/h {n/hours:7.1f}  Mtoman/h {vol/hours/1e6:9.1f}  sells {100*sells/n:4.0f}%")
print("\nUSDTTMN mid first/last:", usdt_series[0], usdt_series[-1])
