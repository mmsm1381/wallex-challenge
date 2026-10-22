import sys, importlib.util
import numpy as np, pandas as pd
spec = importlib.util.spec_from_file_location('L', '../scripts/sim60_lib.py'); L = importlib.util.module_from_spec(spec); spec.loader.exec_module(L)
idx, spread, FEES, days = L.idx, L.spread, L.FEES, L.days
pairs = [s for s in L.PAIRS if (spread.get(s) or 9) <= 0.35]
def thr_for(sym, base):
    q = 'TMN' if sym.endswith('TMN') else 'USDT'; mk, tk = FEES[q]
    return max(base, tk + mk + (spread.get(sym) or 0.5) + 0.3)
print(f"{'base':>4s} {'lat':>3s} {'n/day':>6s} {'mean':>7s} {'median':>7s} {'wins1%':>7s} {'wins2%':>7s} | per-pair mean net winsorized at 1% (n/day)")
for base in (1.0, 1.25, 1.5):
    for lat in (0, 1):
        tr = [t for s in pairs for t in L.signals_thr(s, thr_for(s, base), lat)]
        adj = [(te, tx, s, q, g, net - (spread.get(s) or 0.3) / 2) for (te, tx, s, q, g, net) in tr]
        taken, _, _ = L.portfolio2(adj)
        d = pd.DataFrame(taken, columns=['te', 'tx', 'pair', 'quote', 'gross', 'net'])
        d['w1'] = d.net.clip(-1, 1)
        g = d.groupby('pair').agg(n=('w1', 'size'), m=('w1', 'mean')).sort_values('m', ascending=False)
        pp = ' '.join(f"{p}:{r.m:+.2f}({r.n/days:.1f})" for p, r in g.iterrows() if r.n >= 10)
        print(f"{base:4.2f} {lat:3d} {len(d)/days:6.1f} {d.net.mean():+7.3f} {d.net.median():+7.3f} {d.w1.mean():+7.3f} {d.net.clip(-2,2).mean():+7.3f} | {pp}")
