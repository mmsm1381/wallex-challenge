"""Fair-value gap trades on Wallex TMN margin pairs, long and short, with Wallex margin costs.

Signal: dev = (log Wallex price in USDT - log Binance) minus its trailing 60m median, in %.
Long when dev < -thr, short when dev > +thr; exit when dev crosses 0 or after 15 minutes.
Entry at the Wallex 1m close of the signal minute (lat=0) or the next minute (lat=1).
Cost per round trip: 2 x 0.25% fee + 0.015% open + 0.015% extension + full spread (taker both ways).
"""
import json
import numpy as np, pandas as pd

raw = json.load(open('candles.json'))
def frame(rows):
    return pd.DataFrame.from_dict({int(k): v for k, v in rows.items()}, orient='index', columns=['o', 'h', 'l', 'c', 'v']).sort_index()
W = {s: frame(r) for s, r in raw['wallex'].items()}; B = {s: frame(r) for s, r in raw['binance'].items()}
t0 = max(max(df.index.min() for df in W.values()), max(df.index.min() for df in B.values()))
t1 = min(min(df.index.max() for df in W.values()), min(df.index.max() for df in B.values()))
idx = pd.Index(range(t0, t1 + 60, 60)); days = len(idx) / 1440
close = lambda df: df['c'].reindex(idx).ffill()
usdt = close(W['USDTTMN'])
PAIRS = [('BTCTMN', 'BTCUSDT', 0.06), ('ETHTMN', 'ETHUSDT', 0.36), ('SOLTMN', 'SOLUSDT', 0.39), ('XRPTMN', 'XRPUSDT', 0.6),
         ('PEPETMN', 'PEPEUSDT', 0.2), ('DOGETMN', 'DOGEUSDT', 0.3), ('BNBTMN', 'BNBUSDT', 0.36), ('ARBTMN', 'ARBUSDT', 0.26),
         ('XAUTTMN', 'PAXGUSDT', 0.22)]
COST = 2 * 0.25 + 0.015 + 0.015
print(f"{'pair':8s} {'side':5s} {'thr':>4s} {'lat':>3s} {'n/day':>6s} {'gross%':>7s} {'net%':>7s} {'win%':>5s}")
summary = []
for ws, bs, spr in PAIRS:
    wc = close(W[ws]); bc = close(B[bs])
    b = (np.log(wc / usdt) - np.log(bc)) * 100
    dev = (b - b.rolling(60, min_periods=30).median().shift(1)).values
    px = wc.values; N = len(idx)
    for side in (1, -1):
        for thr in (0.75, 1.0, 1.5):
            for lat in (0, 1):
                i, rets = 0, []
                while i < N - 17:
                    v = dev[i]
                    if not np.isnan(v) and side * v < -thr:   # long: dev < -thr; short: dev > thr
                        e = i + lat; j = e + 1
                        while j < min(e + 15, N - 1) and (np.isnan(dev[j]) or side * dev[j] < 0):
                            j += 1
                        rets.append(side * (px[j] / px[e] - 1) * 100); i = j + 1
                    else:
                        i += 1
                if len(rets) >= 3:
                    g = float(np.mean(rets)); net = g - COST - spr
                    summary.append((ws, side, thr, lat, len(rets) / days, g, net))
                    if thr == 1.0:
                        print(f"{ws:8s} {'long' if side == 1 else 'short':5s} {thr:4.2f} {lat:3d} {len(rets)/days:6.1f} {g:7.3f} {net:7.3f} {np.mean(np.array(rets) > COST + spr) * 100:5.1f}")
s = pd.DataFrame(summary, columns=['pair', 'side', 'thr', 'lat', 'n_day', 'gross', 'net'])
print("\nAll pairs pooled (trade-weighted mean net % per trade, trades/day summed):")
for (side, thr, lat), g in s.groupby(['side', 'thr', 'lat']):
    w = g.n_day / g.n_day.sum()
    print(f"  {'long ' if side == 1 else 'short'} thr={thr:4.2f} lat={lat}: trades/day {g.n_day.sum():5.1f}  net/trade {(g.net * w).sum():+.3f}%  pairs net>0: {(g.net > 0).sum()}/{len(g)}")
