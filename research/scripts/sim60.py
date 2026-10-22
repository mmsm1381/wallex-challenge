"""60-day check of long-only fair-value dip buying on the challenge account (no margin, real fees).

Books: USDT pairs (maker 0.10% / taker 0.13%) and TMN pairs (maker 0.25% / taker 0.30%).
Signal: dev = log(Wallex price in USDT) - log(Binance) minus its trailing 60m median, in %.
Buy when dev < -thr; exit when dev >= 0 (maker exit) or after 15 min (taker exit).
Entry at the Wallex 1m close of the signal minute (lat=0) or the next minute (lat=1).
Portfolio: 5M toman, 1M per trade, max 4 open, 1 per pair; USDT float = 40% of equity.
Prices are 1m last trades, so fills are optimistic for thin pairs; spreads from live snapshots.
"""
import glob, json, sys
import numpy as np, pandas as pd

BIN_DIR = sys.argv[1]
W = json.load(open('wallex60.json'))
spread = W['spread']

def wallex(sym):
    r = W['wallex'][sym]
    return pd.Series({int(k): v[0] for k, v in r.items()}).sort_index()

def binance(coin):
    cols = ['t', 'o', 'h', 'l', 'c', 'v', 'ct', 'qv', 'n', 'tbv', 'tbq', 'x']
    df = pd.concat(pd.read_csv(p, header=None, names=cols, usecols=[0, 4]) for p in sorted(glob.glob(f'{BIN_DIR}/{coin}USDT/*.csv')))
    t = df['t'].to_numpy(); ts = np.where(t > 10**14, t // 10**6, t // 1000)
    return pd.Series(df['c'].to_numpy(), index=ts).groupby(level=0).last().sort_index()

usdt_raw = wallex('USDTTMN')
t0 = max(usdt_raw.index.min(), int(pd.Timestamp('2026-08-22').timestamp()))
t1 = int(pd.Timestamp('2026-10-20 23:59').timestamp())
idx = pd.Index(range(t0 - t0 % 60, t1 + 60, 60))
usdt = usdt_raw.reindex(idx).ffill()
FEES = {'TMN': (0.25, 0.30), 'USDT': (0.10, 0.13)}

def signals(sym, thr, lat):
    q = 'TMN' if sym.endswith('TMN') else 'USDT'; coin = sym[:-len(q)]
    wc = wallex(sym).reindex(idx).ffill(); bc = binance(coin).reindex(idx).ffill()
    wp = wc / usdt if q == 'TMN' else wc
    b = (np.log(wp) - np.log(bc)) * 100
    dev = (b - b.rolling(60, min_periods=30).median().shift(1)).to_numpy(); px = wc.to_numpy()
    mk, tk = FEES[q]; hs = (spread.get(sym) or 0.3) / 2
    out, i, N = [], 0, len(idx)
    while i < N - 17 - lat:
        if not np.isnan(dev[i]) and dev[i] < -thr and not np.isnan(px[i + lat]):
            e = i + lat; j = e + 1
            while j < e + 15 and (np.isnan(dev[j]) or dev[j] < 0):
                j += 1
            converged = not np.isnan(dev[j]) and dev[j] >= 0
            gross = (px[j] / px[e] - 1) * 100
            cost = tk + hs + (mk if converged else tk + hs)
            out.append((idx[e], idx[j], sym, q, gross, gross - cost))
            i = j + 1
        else:
            i += 1
    return out

PAIRS = [s for s in W['wallex'] if s != 'USDTTMN' and glob.glob(f"{BIN_DIR}/{s[:-3 if s.endswith('TMN') else -4]}USDT/*.csv")]
PAIRS = [s for s in W['wallex'] if s != 'USDTTMN' and glob.glob(f"{BIN_DIR}/{(s[:-3] if s.endswith('TMN') else s[:-4])}USDT/*.csv")]
days = len(idx) / 1440
print(f'window {pd.to_datetime(idx[0], unit="s")} -> {pd.to_datetime(idx[-1], unit="s")} ({days:.1f} days), pairs: {len(PAIRS)}')

def portfolio(trades, size=1_000_000, max_open=4, float_frac=0.4):
    trades = sorted(trades)
    open_pos, pnl_events, taken = [], [], []
    for (te, tx, sym, q, g, net) in trades:
        open_pos = [p for p in open_pos if p[0] > te]
        if len(open_pos) >= max_open or any(p[1] == sym for p in open_pos):
            continue
        if q == 'USDT' and sum(1 for p in open_pos if p[2] == 'USDT') >= int(5_000_000 * float_frac // size):
            continue
        open_pos.append((tx, sym, q)); taken.append((te, tx, sym, q, g, net)); pnl_events.append((tx, size * net / 100))
    s = pd.Series(dict(pnl_events)).groupby(level=0).sum().reindex(idx, fill_value=0.0)
    strat = 5_000_000 + s.cumsum()
    fx = 5_000_000 * float_frac * (usdt / usdt.iloc[0] - 1)
    return taken, strat, strat + fx

def stats(eq):
    e = pd.Series(eq.to_numpy(), index=pd.to_datetime(idx, unit='s'))
    dd = (1 - e / e.cummax()).max() * 100
    h = e.resample('1h').last()
    r3 = (h.shift(-72) / h - 1).dropna() * 100
    dd3 = []
    v = h.to_numpy()
    for i in range(0, len(v) - 72, 6):
        w = v[i:i + 73]; dd3.append((1 - (w / np.maximum.accumulate(w)).min()) * 100)
    return (e.iloc[-1] / 5_000_000 - 1) * 100, dd, r3.median(), r3.quantile(.1), (r3 > 0).mean() * 100, np.percentile(dd3, 90)

print(f"\n{'thr':>4s} {'lat':>3s} {'trades/day':>10s} {'net/trade':>9s} {'win%':>5s} | strategy only: {'60d%':>6s} {'maxDD%':>6s} {'3d med':>6s} {'3d p10':>6s} {'3d>0%':>5s} {'3d DDp90':>8s} | with USDT float FX: {'60d%':>6s} {'maxDD%':>6s} {'3d med':>6s} {'3d p10':>6s}")
cache = {}
for thr in (0.75, 1.0, 1.25):
    for lat in (0, 1):
        tr = [t for s in PAIRS for t in signals(s, thr, lat)]
        cache[(thr, lat)] = tr
        taken, strat, withfx = portfolio(tr)
        n = len(taken); nets = np.array([t[5] for t in taken])
        a = stats(strat); b = stats(withfx)
        print(f"{thr:4.2f} {lat:3d} {n/days:10.1f} {nets.mean():+9.3f} {(nets>0).mean()*100:5.1f} | {a[0]:+14.2f} {a[1]:6.2f} {a[2]:+6.2f} {a[3]:+6.2f} {a[4]:5.0f} {a[5]:8.2f} | {b[0]:+19.2f} {b[1]:6.2f} {b[2]:+6.2f} {b[3]:+6.2f}")

print('\nPer pair at thr=1.0 (trades/day, mean net %/trade) lat=0 | lat=1:')
for s in PAIRS:
    a = [t[5] for t in cache[(1.0, 0)] if t[2] == s]; b = [t[5] for t in cache[(1.0, 1)] if t[2] == s]
    if a:
        print(f"  {s:9s} spread {spread.get(s) or float('nan'):.2f}%  {len(a)/days:5.1f}/d {np.mean(a):+.3f}% | {len(b)/days:5.1f}/d {np.mean(b) if b else float('nan'):+.3f}%")
print('\nWeekly strategy PnL (% of 5M), thr=1.0:')
for lat in (0, 1):
    taken, strat, _ = portfolio(cache[(1.0, lat)])
    e = pd.Series(strat.to_numpy(), index=pd.to_datetime(idx, unit='s')).resample('7D').last()
    print(f'  lat={lat}:', ' '.join(f'{x:+.2f}' for x in (e.diff().fillna(e.iloc[0] - 5_000_000) / 5_000_000 * 100)))
