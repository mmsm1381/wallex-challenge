"""Lead-lag and basis study: Wallex 1m candles vs Binance 1m candles."""
import json
import numpy as np
import pandas as pd

raw = json.load(open('candles.json'))

def frame(rows):
    df = pd.DataFrame.from_dict({int(k): v for k, v in rows.items()}, orient='index', columns=['o', 'h', 'l', 'c', 'v']).sort_index()
    return df

W = {s: frame(r) for s, r in raw['wallex'].items()}
B = {s: frame(r) for s, r in raw['binance'].items()}
t0 = max(max(df.index.min() for df in W.values()), max(df.index.min() for df in B.values()))
t1 = min(min(df.index.max() for df in W.values()), min(df.index.max() for df in B.values()))
idx = pd.Index(range(t0, t1 + 60, 60))
print(f'overlap: {pd.to_datetime(t0, unit="s")} -> {pd.to_datetime(t1, unit="s")}  ({len(idx)} minutes)')

def close(df):
    return df['c'].reindex(idx).ffill()
def vol(df):
    return df['v'].reindex(idx).fillna(0)

usdt = close(W['USDTTMN'])
PAIRS = [  # wallex symbol, binance symbol, typical spread % from snapshot
    ('BTCUSDT', 'BTCUSDT', 0.035), ('ETHUSDT', 'ETHUSDT', 0.22), ('SOLUSDT', 'SOLUSDT', 0.49), ('XRPUSDT', 'XRPUSDT', 0.06),
    ('PEPEUSDT', 'PEPEUSDT', 0.43), ('DOGEUSDT', 'DOGEUSDT', 0.23), ('XAUTUSDT', 'PAXGUSDT', 0.004),
    ('BTCTMN', 'BTCUSDT', 0.06), ('ETHTMN', 'ETHUSDT', 0.36), ('SOLTMN', 'SOLUSDT', 0.39), ('XRPTMN', 'XRPUSDT', 0.6),
    ('PEPETMN', 'PEPEUSDT', 0.2), ('DOGETMN', 'DOGEUSDT', 0.3), ('BNBTMN', 'BNBUSDT', 0.36), ('ARBTMN', 'ARBUSDT', 0.26),
    ('XAUTTMN', 'PAXGUSDT', 0.22),
]
FEE = 0.25  # % per side, assumed taker fee (to verify with GET /v1/account/fee)

print('\n=== A. Lead-lag: corr(Wallex ret(t), Binance ret(t-k)) on 1m log returns; share of Binance move absorbed by lag ===')
print(f"{'pair':9s} {'k=0':>6s} {'k=1':>6s} {'k=2':>6s} {'k=3':>6s} {'k=5':>6s} | {'beta0':>6s} {'beta1':>6s} {'beta2+':>6s} | {'%min traded':>11s}")
for ws, bs, spr in PAIRS:
    wc = close(W[ws]); bc = close(B[bs])
    wp = wc / usdt if ws.endswith('TMN') else wc
    rw = np.log(wp).diff(); rb = np.log(bc).diff()
    cs = [rw.corr(rb.shift(k)) for k in (0, 1, 2, 3, 5)]
    X = pd.concat([rb.shift(k) for k in range(0, 6)], axis=1).dropna()
    y = rw.reindex(X.index)
    m = y.notna()
    beta = np.linalg.lstsq(X[m].values, y[m].values, rcond=None)[0]
    traded = (vol(W[ws]) > 0).mean() * 100
    print(f"{ws:9s} " + ' '.join(f'{c:6.3f}' for c in cs) + f" | {beta[0]:6.2f} {beta[1]:6.2f} {beta[2:].sum():6.2f} | {traded:10.1f}%")

print('\n=== B. Basis = log(Wallex price in USDT) - log(Binance); dev = basis - trailing 60m median ===')
print(f"{'pair':9s} {'mean%':>7s} {'std(dev)%':>9s} {'p90|dev|':>8s} {'p99|dev|':>8s} {'>0.5%':>7s} {'>1%':>6s} {'AR1':>5s} {'halflife(min)':>13s}")
devs = {}
for ws, bs, spr in PAIRS:
    wc = close(W[ws]); bc = close(B[bs])
    wp = wc / usdt if ws.endswith('TMN') else wc
    b = (np.log(wp) - np.log(bc)) * 100
    med = b.rolling(60, min_periods=30).median().shift(1)
    dev = (b - med).dropna()
    devs[ws] = (dev, b)
    ar1 = dev.autocorr(1)
    hl = np.log(0.5) / np.log(ar1) if 0 < ar1 < 1 else float('nan')
    a = dev.abs()
    print(f"{ws:9s} {b.mean():7.3f} {dev.std():9.3f} {a.quantile(.9):8.3f} {a.quantile(.99):8.3f} {(a>0.5).mean()*100:6.2f}% {(a>1).mean()*100:5.2f}% {ar1:5.2f} {hl:13.1f}")

print('\n=== C. Rough convergence test on last-trade closes (long-only: buy when Wallex cheap vs Binance) ===')
print('entry at Wallex close of the signal minute (lat=0) or the next minute (lat=1); exit when dev>=0 or after 15 min')
print(f'net = gross - (2*{FEE}% fee + spread); last-trade prices make gross optimistic for wide-spread pairs')
print(f"{'pair':9s} {'thr':>4s} {'lat':>3s} {'n':>5s} {'n/day':>6s} {'gross%':>7s} {'net%':>7s} {'win%':>5s}")
days = len(idx) / 1440
for ws, bs, spr in PAIRS:
    dev, b = devs[ws]
    wc = close(W[ws])
    d = dev.reindex(idx)
    for thr in (0.5, 1.0):
        for lat in (0, 1):
            pos = np.where(idx.values[:, None] == 0, 0, 0)  # placeholder
            i, n, rets = 0, 0, []
            vals = d.values; px = wc.values
            N = len(idx)
            while i < N - 16 - lat:
                if not np.isnan(vals[i]) and vals[i] < -thr:
                    e = i + lat
                    j = e + 1
                    while j < min(e + 15, N - 1) and (np.isnan(vals[j]) or vals[j] < 0):
                        j += 1
                    rets.append((px[j] / px[e] - 1) * 100)
                    i = j + 1
                else:
                    i += 1
            if rets:
                g = float(np.mean(rets)); net = g - (2 * FEE + spr)
                print(f"{ws:9s} {thr:4.1f} {lat:3d} {len(rets):5d} {len(rets)/days:6.1f} {g:7.3f} {net:7.3f} {np.mean(np.array(rets) > 2*FEE+spr)*100:5.1f}")

print('\n=== D. USDTTMN behaviour ===')
u = usdt
for h in (1, 5, 15, 60, 240):
    r = (np.log(u).diff(h) * 100).dropna()
    print(f"  |ret {h:3d}m|: median {r.abs().median():.3f}%  p90 {r.abs().quantile(.9):.3f}%  share>0.5%: {(r.abs()>0.5).mean()*100:5.1f}%  share>1%: {(r.abs()>1).mean()*100:5.1f}%")
dd = pd.Series(u.values, index=pd.to_datetime(idx, unit='s')).resample('1D').agg(['min', 'max', 'first', 'last'])
dd['range%'] = (dd['max'] / dd['min'] - 1) * 100
dd['chg%'] = (dd['last'] / dd['first'] - 1) * 100
print(dd.round(2).to_string())
r1 = np.log(u).diff()
print('  USDTTMN 1m return autocorr lag1..3:', [round(r1.autocorr(k), 3) for k in (1, 2, 3)])
r5 = np.log(u).diff(5)
print('  USDTTMN 5m return autocorr (non-overlapping):', round(r5.iloc[::5].autocorr(1), 3))

print('\n=== E. How often do Wallex prices move more than the round-trip cost? (share of windows, TMN terms) ===')
print(f"{'sym':9s} {'15m>1%':>7s} {'60m>1%':>7s} {'60m>2%':>7s} {'240m>2%':>8s} {'daily rng%':>10s} {'med TMN vol/active min':>22s}")
for ws in ['USDTTMN', 'BTCTMN', 'ETHTMN', 'SOLTMN', 'XRPTMN', 'PEPETMN', 'DOGETMN', 'BNBTMN', 'ARBTMN', 'XAUTTMN']:
    c = close(W[ws]); lr = np.log(c)
    f = lambda h, x: ((lr.diff(h).abs() * 100) > x).mean() * 100
    dr = (pd.Series(c.values, index=pd.to_datetime(idx, unit='s')).resample('1D').agg(lambda s: s.max() / s.min() - 1) * 100).mean()
    v = vol(W[ws]) * c
    print(f"{ws:9s} {f(15,1):6.1f}% {f(60,1):6.1f}% {f(60,2):6.1f}% {f(240,2):7.1f}% {dr:10.2f} {v[v>0].median()/1e6:20.1f}M")
