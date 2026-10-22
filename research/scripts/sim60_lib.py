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

def signals_thr(sym, thr, lat, max_hold=15):
    q = 'TMN' if sym.endswith('TMN') else 'USDT'; coin = sym[:-len(q)]
    wc = wallex(sym).reindex(idx).ffill(); bc = binance(coin).reindex(idx).ffill()
    wp = wc / usdt if q == 'TMN' else wc
    b = (np.log(wp) - np.log(bc)) * 100
    dev = (b - b.rolling(60, min_periods=30).median().shift(1)).to_numpy(); px = wc.to_numpy()
    mk, tk = FEES[q]; hs = (spread.get(sym) or 0.3) / 2
    out, i, N = [], 0, len(idx)
    while i < N - max_hold - 2 - lat:
        if not np.isnan(dev[i]) and dev[i] < -thr and not np.isnan(px[i + lat]):
            e = i + lat; j = e + 1
            while j < e + max_hold and (np.isnan(dev[j]) or dev[j] < 0):
                j += 1
            conv = not np.isnan(dev[j]) and dev[j] >= 0
            g = (px[j] / px[e] - 1) * 100
            out.append((idx[e], idx[j], sym, q, g, g - (tk + hs + (mk if conv else tk + hs))))
            i = j + 1
        else:
            i += 1
    return out

def portfolio2(trades, size=1_000_000, max_open=4, float_frac=0.4, day_stop=0.015, cooldown=300):
    trades = sorted(trades)
    open_pos, events, taken, last_exit = [], [], [], {}
    eq, day, day_start, day_pnl, stopped = 5_000_000.0, None, 5_000_000.0, 0.0, False
    for (te, tx, sym, q, g, net) in trades:
        d = te // 86400
        realized = [p for p in open_pos if p[0] <= te]
        for p in realized:
            eq += p[3]; day_pnl += p[3] if p[4] == d else 0.0
        open_pos = [p for p in open_pos if p[0] > te]
        if d != day:
            day, day_start, day_pnl, stopped = d, eq, 0.0, False
        if day_pnl <= -day_stop * day_start:
            stopped = True
        if stopped or len(open_pos) >= max_open or any(p[1] == sym for p in open_pos) or te - last_exit.get(sym, -10**9) < cooldown:
            continue
        if q == 'USDT' and sum(1 for p in open_pos if p[2] == 'USDT') >= int(5_000_000 * float_frac // size):
            continue
        pnl = size * net / 100
        open_pos.append((tx, sym, q, pnl, tx // 86400)); taken.append((te, tx, sym, q, g, net)); events.append((tx, pnl)); last_exit[sym] = tx
    s = pd.Series(dict(events)).groupby(level=0).sum() if events else pd.Series(dtype=float)
    s = s.reindex(idx, fill_value=0.0)
    strat = 5_000_000 + s.cumsum()
    fx = 5_000_000 * float_frac * (usdt / usdt.iloc[0] - 1)
    return taken, strat, strat + fx
