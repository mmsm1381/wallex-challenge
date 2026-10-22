"""Long-only fair-value dip buying on Wallex with the challenge account's real fees.

Fees (credit account): TMN markets maker 0.25% / taker 0.30%; USDT markets maker 0.10% / taker 0.13%.
Signal: dev = log(Wallex price) - log(global fair value) minus trailing 60m median, in %.
Buy when dev < -thr; exit when dev >= 0 or after 15 min. Entry at the 1m close of the signal
minute (lat=0) or of the next minute (lat=1). Prices are 1m last trades (optimistic for thin pairs).
Cost A: taker in + taker out + full spread. Cost B: taker in + maker exit at fair value + half spread.
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
FEES = {'TMN': (0.25, 0.30), 'USDT': (0.10, 0.13)}
PAIRS = [('BTCTMN', 'BTCUSDT', 0.06), ('ETHTMN', 'ETHUSDT', 0.36), ('SOLTMN', 'SOLUSDT', 0.39), ('XRPTMN', 'XRPUSDT', 0.6),
         ('PEPETMN', 'PEPEUSDT', 0.2), ('DOGETMN', 'DOGEUSDT', 0.3), ('BNBTMN', 'BNBUSDT', 0.36), ('ARBTMN', 'ARBUSDT', 0.26),
         ('XAUTTMN', 'PAXGUSDT', 0.22),
         ('BTCUSDT', 'BTCUSDT', 0.035), ('ETHUSDT', 'ETHUSDT', 0.22), ('SOLUSDT', 'SOLUSDT', 0.49), ('XRPUSDT', 'XRPUSDT', 0.06),
         ('PEPEUSDT', 'PEPEUSDT', 0.43), ('DOGEUSDT', 'DOGEUSDT', 0.23), ('XAUTUSDT', 'PAXGUSDT', 0.004)]
rows = []
for ws, bs, spr in PAIRS:
    q = 'TMN' if ws.endswith('TMN') else 'USDT'
    mk, tk = FEES[q]
    wc = close(W[ws]); bc = close(B[bs])
    wp = wc / usdt if q == 'TMN' else wc
    b = (np.log(wp) - np.log(bc)) * 100
    dev = (b - b.rolling(60, min_periods=30).median().shift(1)).values
    px = wc.values; N = len(idx)
    for thr in (0.3, 0.5, 0.75, 1.0):
        for lat in (0, 1):
            i, rets = 0, []
            while i < N - 17:
                if not np.isnan(dev[i]) and dev[i] < -thr:
                    e = i + lat; j = e + 1
                    while j < min(e + 15, N - 1) and (np.isnan(dev[j]) or dev[j] < 0):
                        j += 1
                    rets.append((px[j] / px[e] - 1) * 100); i = j + 1
                else:
                    i += 1
            if rets:
                g = float(np.mean(rets))
                rows.append(dict(pair=ws, quote=q, thr=thr, lat=lat, n_day=len(rets) / days, gross=g,
                                 netA=g - 2 * tk - spr, netB=g - tk - mk - spr / 2))
r = pd.DataFrame(rows)
pd.set_option('display.width', 220)
for q in ('USDT', 'TMN'):
    print(f'\n=== {q} pairs: per pair at thr=0.5 and 1.0 ===')
    print(r[(r.quote == q) & (r.thr.isin([0.5, 1.0]))].round(3).to_string(index=False))
print('\n=== pooled by quote/threshold/latency (trade-weighted net %/trade; trades/day summed) ===')
for (q, thr, lat), g in r.groupby(['quote', 'thr', 'lat']):
    w = g.n_day / g.n_day.sum()
    print(f"  {q:4s} thr={thr:4.2f} lat={lat}: trades/day {g.n_day.sum():6.1f}  netA {(g.netA*w).sum():+.3f}%  netB {(g.netB*w).sum():+.3f}%  pairs netB>0 {(g.netB>0).sum()}/{len(g)}")
