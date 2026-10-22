"""Sweep study on 1m candles: do resting bids far below fair value pay?

The study reads the files of `sweep_fetch.py` and prints the tables of
`docs/RESEARCH.md` section 3.

Rules of the simulation:

* Fair value per minute = Binance close x USDTTMN close (toman markets) x
  exp(premium). The premium is the median of log(Wallex close / raw fair
  value) in the 60 minutes before, over minutes with Wallex volume.
* The bid in minute t uses the fair value of minute t-1. So the bid does not
  know the global move inside minute t.
* The bid is at fair value x (1 - depth). It fills when the Wallex low of the
  minute is strictly below it. Our higher bid had priority, so such a trade
  means that our bid filled first and in full.
* Exit: a maker sell at the fair value of minute k-1, when the Wallex high of
  minute k is strictly above it (k = 1..15). Else a taker sell at the close
  of minute 15 minus 0.1%.
* One position per market at a time. Size 1M toman. Real challenge fees.

Filters (the plan uses both):

* global: skip the fill when Binance fell 0.3% or more in the same minute.
* fx: skip a toman market when USDTTMN moved 0.5% or more in the last
  5 minutes.

Usage (no third-party packages):
    python research/scripts/sweep_study.py DATA_DIR [DEPTHS_PCT] [REPORT_DEPTH] [RAW_DIR]
RAW_DIR adds 1m global closes from lagcatcher raw quotes for minutes that the
Binance files do not have yet (the current day).
"""
import bisect
import collections
import datetime
import gzip
import json
import math
import pathlib
import statistics
import sys
import zlib

DATA = sys.argv[1]
DEPTHS = [float(x) for x in (sys.argv[2] if len(sys.argv) > 2
                              else "0.8,1.0,1.3,1.6,2.0,2.5").split(",")]
REPORT_DEPTH = float(sys.argv[3]) if len(sys.argv) > 3 else 1.3
RAW_DIR = sys.argv[4] if len(sys.argv) > 4 else None
FEES = {"TMN": (0.0025, 0.0030), "USDT": (0.0010, 0.0013)}
GLOBAL_DROP_PCT = -0.3
FX_SHOCK_PCT = 0.5


def fmt(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%m-%d %H:%M")


W = json.load(open(f"{DATA}/wallex_ohlc.json"))["ohlc"]
W = {m: {int(t): v for t, v in rows.items()} for m, rows in W.items()}
U = W["USDTTMN"]
U_TS = sorted(U)


def usdttmn_at(t):
    """Returns the last USDTTMN candle at or before t: [o, h, l, c, v]."""
    i = bisect.bisect_right(U_TS, t) - 1
    return U[U_TS[i]] if i >= 0 else None


def usdttmn_close(t):
    candle = usdttmn_at(t)
    return candle[3] if candle else None


def fx_shock_pct(t):
    """Returns the largest USDTTMN move in minutes t-5..t versus the close of t-6."""
    ref = usdttmn_at(t - 360)
    candles = [c for c in (usdttmn_at(t - 60 * k) for k in range(6)) if c is not None]
    if ref is None or len(candles) < 6:
        return 0.0
    hi = max(c[1] for c in candles)
    lo = min(c[2] for c in candles)
    return 100 * max(hi / ref[3] - 1, 1 - lo / ref[3])


# Global closes from lagcatcher raw files, for minutes without Binance files.
EXTRA = collections.defaultdict(dict)
if RAW_DIR:
    last = collections.defaultdict(dict)  # (coin, minute) -> venue -> mid
    for path in sorted(pathlib.Path(RAW_DIR).glob("global-*.jsonl.gz")):
        try:
            with gzip.open(path, "rt") as fh:
                for line in fh:
                    if line.endswith("\n"):
                        r = json.loads(line)
                        last[(r["c"], int(r["ts"]) // 60 * 60)][r["v"]] = (r["b"] + r["a"]) / 2
        except (EOFError, zlib.error):
            pass
    for (coin, minute), mids in last.items():
        EXTRA[coin][minute] = mids.get("binance") or statistics.median(mids.values())

# Event: (market, depth, entry_ts, net_pct, exit_reason, binance_move_pct, low_gap_pct, fx_shock_pct)
events = []
for market, rows in W.items():
    if market == "USDTTMN":
        continue
    quote = "USDT" if market.endswith("USDT") else "TMN"
    coin = market[: -len(quote)]
    try:
        B = {int(t): v for t, v in json.load(open(f"{DATA}/binance/{coin}USDT.json")).items()}
    except FileNotFoundError:
        continue
    for t, close in EXTRA.get(coin, {}).items():
        B.setdefault(t, [close, close, close, close])
    maker, taker = FEES[quote]
    ts = sorted(rows)

    def raw_fv(t):
        b = B.get(t)
        if b is None:
            return None
        if quote == "USDT":
            return b[3]
        u = usdttmn_close(t)
        return b[3] * u if u else None

    basis = collections.deque()  # (t, log basis) of minutes with volume
    fv_at = {}
    for t in ts:
        while basis and t - basis[0][0] > 3600:
            basis.popleft()
        raw = raw_fv(t)
        if raw and len(basis) >= 20:
            fv_at[t] = raw * math.exp(statistics.median(b for _, b in basis))
        if raw and rows[t][4] > 0:
            basis.append((t, math.log(rows[t][3] / raw)))

    for depth in DEPTHS:
        busy_until = 0
        for t in ts:
            if t < busy_until or t - 60 not in fv_at:
                continue
            fv = fv_at[t - 60]
            bid = fv * (1 - depth / 100)
            low = rows[t][2]
            if not low < bid:
                continue
            b, b_prev = B.get(t), B.get(t - 60)
            gmove = 100 * (b[3] / b_prev[3] - 1) if b and b_prev else 0.0
            exit_price, reason, end = None, "time", t + 15 * 60
            for k in range(1, 16):
                tk = t + 60 * k
                if tk in rows and tk - 60 in fv_at and rows[tk][1] > fv_at[tk - 60]:
                    exit_price, reason, end = fv_at[tk - 60], "target", tk
                    break
            if exit_price is None:
                i = bisect.bisect_right(ts, end) - 1
                exit_price = rows[ts[i]][3] * 0.999
            fee_out = maker if reason == "target" else taker
            net = (exit_price * (1 - fee_out)) / (bid * (1 + maker)) - 1
            shock = fx_shock_pct(t) if quote == "TMN" else 0.0
            events.append((market, depth, t, 100 * net, reason, gmove,
                           100 * (1 - low / fv), shock))
            busy_until = end + 60


def passes_filters(e):
    return e[5] > GLOBAL_DROP_PCT and e[7] < FX_SHOCK_PCT


def summary(rows, days):
    if not rows:
        return "no fills"
    nets = [r[3] for r in rows]
    daily = collections.defaultdict(float)
    for r in rows:
        daily[r[2] // 86400] += r[3] * 1e4  # toman at 1M per trade
    equity = peak = drawdown = 0.0
    for day in sorted(daily):
        equity += daily[day]
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    return (f"fills {len(nets):4d} ({len(nets) / days:4.1f}/day) "
            f"win {100 * sum(n > 0 for n in nets) / len(nets):3.0f}% "
            f"median {statistics.median(nets):+.2f}% mean {statistics.fmean(nets):+.2f}% "
            f"net {sum(nets) * 10:+6.0f}k ({sum(nets) * 10 / days:+5.1f}k/day) "
            f"max drawdown {drawdown / 1000:+5.0f}k "
            f"days+ {sum(v > 0 for v in daily.values())}/{len(daily)}")


first = min(e[2] for e in events)
last_ts = max(e[2] for e in events)
days = (last_ts - first) / 86400
split = first + 30 * 86400
print(f"{len(events)} fills over {days:.0f} days ({fmt(first)} to {fmt(last_ts)} UTC). "
      "Toman per trade: 1,000,000.\n")
for title, keep in [("No filters", lambda e: True), ("Both filters", passes_filters)]:
    print(f"== {title}")
    for depth in DEPTHS:
        rows = [e for e in events if e[1] == depth and keep(e)]
        print(f"  depth {depth:4.1f}%: {summary(rows, days)}")
    print()

print(f"== Both filters, last {(last_ts - split) / 86400:.0f} days only")
for depth in DEPTHS:
    rows = [e for e in events if e[1] == depth and passes_filters(e) and e[2] >= split]
    print(f"  depth {depth:4.1f}%: {summary(rows, (last_ts - split) / 86400)}")

report = [e for e in events if e[1] == REPORT_DEPTH]
print(f"\n== Split by the Binance move in the same minute (depth {REPORT_DEPTH}%, no filters)")
for name, keep in [("Binance calm (|move| < 0.3%)", lambda e: abs(e[5]) < 0.3),
                   ("Binance fell 0.3% or more", lambda e: e[5] <= -0.3),
                   ("Binance rose 0.3% or more", lambda e: e[5] >= 0.3)]:
    rows = [e for e in report if keep(e)]
    if rows:
        nets = [e[3] for e in rows]
        print(f"  {name:30s} fills {len(rows):4d} win {100 * sum(n > 0 for n in nets) / len(nets):3.0f}% "
              f"mean {statistics.fmean(nets):+.2f}%")

print(f"\n== Per market, depth {REPORT_DEPTH}%, both filters")
by_market = collections.defaultdict(list)
for e in report:
    if passes_filters(e):
        by_market[e[0]].append(e[3])
for market, nets in sorted(by_market.items(), key=lambda kv: -sum(kv[1])):
    print(f"  {market:9s} fills {len(nets):3d} win {100 * sum(n > 0 for n in nets) / len(nets):3.0f}% "
          f"median {statistics.median(nets):+.2f}% net {sum(nets) * 10:+5.0f}k")

train = collections.defaultdict(float)
for e in report:
    if passes_filters(e) and e[2] < split:
        train[e[0]] += e[3]
top5 = sorted(train, key=lambda m: -train[m])[:5]
test = [e for e in report if passes_filters(e) and e[2] >= split and e[0] in top5]
print(f"\n== Out of sample: 5 best markets of the first 30 days, tested on the rest")
print(f"  markets: {', '.join(top5)}")
print(f"  {summary(test, (last_ts - split) / 86400)}")

print(f"\n== Worst 8 trades, depth {REPORT_DEPTH}%, no filters")
for e in sorted(report, key=lambda e: e[3])[:8]:
    print(f"  {e[0]:9s} {fmt(e[2])} net {e[3]:+.2f}% {e[4]:6s} binance {e[5]:+.2f}% "
          f"usdttmn shock {e[7]:.1f}%")
json.dump(events, open(f"{DATA}/sweep_events.json", "w"))
