import sys, importlib.util
import numpy as np, pandas as pd
spec = importlib.util.spec_from_file_location('L', '../scripts/sim60_lib.py'); L = importlib.util.module_from_spec(spec); spec.loader.exec_module(L)
idx, spread, FEES = L.idx, L.spread, L.FEES
pairs = [s for s in L.PAIRS if (spread.get(s) or 9) <= 0.35]
def thr_for(sym, base):
    q = 'TMN' if sym.endswith('TMN') else 'USDT'; mk, tk = FEES[q]
    return max(base, tk + mk + (spread.get(sym) or 0.5) + 0.3)
weeks = pd.date_range('2026-08-22', '2026-10-21', freq='7D')
print('week starts:', ' '.join(w.strftime('%m-%d') for w in weeks[:-1]))
for base in (1.0, 1.25):
    for lat in (0, 1):
        tr = [t for s in pairs for t in L.signals_thr(s, thr_for(s, base), lat)]
        for extra, label in ((0.0, 'half-spread entry'), (1.0, 'full-spread entry')):
            adj = [(te, tx, s, q, g, net - extra * (spread.get(s) or 0.3) / 2) for (te, tx, s, q, g, net) in tr]
            taken, strat, _ = L.portfolio2(adj)
            e = pd.Series(strat.to_numpy(), index=pd.to_datetime(idx, unit='s'))
            wk = [(e[(e.index >= a) & (e.index < b)].iloc[-1] - e[e.index < a].iloc[-1] if (e.index < a).any() else e[(e.index < b)].iloc[-1] - 5e6) / 5e6 * 100 for a, b in zip(weeks[:-1], weeks[1:])]
            nt = [sum(1 for t in taken if a.timestamp() <= t[0] < b.timestamp()) for a, b in zip(weeks[:-1], weeks[1:])]
            nets = np.array([t[5] for t in taken])
            print(f"base {base:4.2f} lat {lat} {label:17s} net/trade {nets.mean():+.3f}% | weekly %: " + ' '.join(f'{x:+6.2f}' for x in wk) + ' | trades/wk: ' + ' '.join(str(n) for n in nt))
