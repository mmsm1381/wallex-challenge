import sys, importlib.util
import numpy as np, pandas as pd
spec = importlib.util.spec_from_file_location('L', '../scripts/sim60_lib.py'); L = importlib.util.module_from_spec(spec); spec.loader.exec_module(L)
idx, spread, FEES, days = L.idx, L.spread, L.FEES, L.days
pairs = [s for s in L.PAIRS if (spread.get(s) or 9) <= 0.35]
def thr_for(sym, base):
    q = 'TMN' if sym.endswith('TMN') else 'USDT'; mk, tk = FEES[q]
    return max(base, tk + mk + (spread.get(sym) or 0.5) + 0.3)
for base, lat in ((1.25, 1), (1.0, 1)):
    tr = [t for s in pairs for t in L.signals_thr(s, thr_for(s, base), lat)]
    adj = [(te, tx, s, q, g, net - (spread.get(s) or 0.3) / 2) for (te, tx, s, q, g, net) in tr]  # full-spread entry
    taken, strat, _ = L.portfolio2(adj)
    d = pd.DataFrame(taken, columns=['te', 'tx', 'pair', 'quote', 'gross', 'net'])
    tot = d.net.sum()
    top = d.sort_values('net', ascending=False)
    print(f"\n=== base {base} lat {lat} full-spread entry: {len(d)} trades, mean net {d.net.mean():+.3f}%, median net {d.net.median():+.3f}%, win {(d.net>0).mean()*100:.1f}%")
    print(f"  share of total PnL from top 1% trades: {top.net.head(max(1, len(d)//100)).sum()/tot*100:.0f}%  | top 5% {top.net.head(len(d)//20).sum()/tot*100:.0f}%")
    print(f"  trades with |gross| > 3%: {(d.gross.abs() > 3).sum()}   max gross {d.gross.max():+.2f}%  min gross {d.gross.min():+.2f}%")
    for cap in (2.0, 1.0):
        w = d.net.clip(-cap, cap)
        print(f"  winsorized at +/-{cap}%: mean net {w.mean():+.3f}%  -> ~{w.sum() * 1e6 / 100 / 5e6 * 100 / days * 3:+.2f}% per 3 days at 1M/trade")
    g = d.groupby('pair').net.agg(['count', 'mean', 'sum']).sort_values('sum', ascending=False)
    g['count/day'] = g['count'] / days
    print('  per pair:', ', '.join(f"{p} {r['count/day']:.1f}/d {r['mean']:+.2f}%" for p, r in g.iterrows()))
    d['hold_min'] = (d.tx - d.te) / 60
    print(f"  median hold {d.hold_min.median():.0f} min; exits by convergence {((d.hold_min < 15)).mean()*100:.0f}%")
    print('  top 5 trades:', top.head(5)[['pair', 'gross', 'net']].assign(t=pd.to_datetime(top.head(5).te, unit='s').dt.strftime('%m-%d %H:%M')).to_dict('records'))
