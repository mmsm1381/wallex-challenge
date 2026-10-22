import sys, importlib.util
import numpy as np, pandas as pd
sys.argv = [sys.argv[0], sys.argv[1]]
spec = importlib.util.spec_from_file_location('L', '../scripts/sim60_lib.py'); L = importlib.util.module_from_spec(spec); spec.loader.exec_module(L)
idx, spread, FEES, days = L.idx, L.spread, L.FEES, L.days
SPLIT = int(pd.Timestamp('2026-10-14').timestamp())

def thr_for(sym, base):
    q = 'TMN' if sym.endswith('TMN') else 'USDT'; mk, tk = FEES[q]
    return max(base, tk + mk + (spread.get(sym) or 0.5) + 0.3)

def window_stats(eq, lo, hi):
    e = pd.Series(eq.to_numpy(), index=idx); e = e[(e.index >= lo) & (e.index < hi)]
    e.index = pd.to_datetime(e.index, unit='s'); h = e.resample('1h').last()
    r3 = (h.shift(-72) / h - 1).dropna() * 100
    dd3 = [(1 - (w / np.maximum.accumulate(w)).min()) * 100 for w in (h.to_numpy()[i:i + 73] for i in range(0, len(h) - 72, 6))]
    ret = (e.iloc[-1] / e.iloc[0] - 1) * 100; mdd = (1 - e / e.cummax()).max() * 100
    return ret, mdd, r3.median(), r3.quantile(.1), (r3 > 0).mean() * 100, np.percentile(dd3, 90)

UNIVERSES = {
    'all 26 pairs': [s for s in L.PAIRS],
    'spread<=0.35% (ex ante)': [s for s in L.PAIRS if (spread.get(s) or 9) <= 0.35],
}
for uname, pairs in UNIVERSES.items():
    print(f'\n##### universe: {uname} ({len(pairs)} pairs): {" ".join(pairs)}')
    print(f"{'base':>4s} {'lat':>3s} {'book':5s} {'tr/day':>6s} {'net/tr':>7s} {'win%':>5s} || OOS Aug22-Oct13: {'ret%':>6s} {'mDD%':>5s} {'3dmed':>6s} {'3dp10':>6s} {'3d>0':>5s} {'3dDDp90':>7s} || Oct14-20: {'ret%':>6s} || full+FX: {'ret%':>6s} {'mDD%':>5s}")
    for base in (1.0, 1.25):
        for lat in (0, 1):
            alltr = [t for s in pairs for t in L.signals_thr(s, thr_for(s, base), lat)]
            for book in ('both', 'USDT', 'TMN'):
                tr = [t for t in alltr if book == 'both' or t[3] == book]
                taken, strat, withfx = L.portfolio2(tr, float_frac=0.4 if book != 'TMN' else 0.0)
                if not taken:
                    continue
                nets = np.array([t[5] for t in taken])
                a = window_stats(strat, idx[0], SPLIT); b = window_stats(strat, SPLIT, idx[-1] + 60); c = window_stats(withfx, idx[0], idx[-1] + 60)
                print(f"{base:4.2f} {lat:3d} {book:5s} {len(taken)/days:6.1f} {nets.mean():+7.3f} {(nets>0).mean()*100:5.1f} || {a[0]:+22.2f} {a[1]:5.2f} {a[2]:+6.2f} {a[3]:+6.2f} {a[4]:5.0f} {a[5]:7.2f} || {b[0]:+14.2f} || {c[0]:+14.2f} {c[1]:5.2f}")
