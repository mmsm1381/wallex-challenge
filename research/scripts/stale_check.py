"""Are the candle 'gaps' real or stale prints? Split signals by whether Wallex traded in the signal minute."""
import sys, importlib.util
import numpy as np, pandas as pd
spec = importlib.util.spec_from_file_location('L', '../scripts/sim60_lib.py'); L = importlib.util.module_from_spec(spec); spec.loader.exec_module(L)
idx, spread, FEES, days, W = L.idx, L.spread, L.FEES, L.days, L.W
pairs = [s for s in L.PAIRS if (spread.get(s) or 9) <= 0.35]
rows = []
for sym in pairs:
    q = 'TMN' if sym.endswith('TMN') else 'USDT'; coin = sym[:-len(q)]
    r = W['wallex'][sym]
    vol = pd.Series({int(k): v[1] for k, v in r.items()}).reindex(idx).fillna(0).to_numpy()
    wc = L.wallex(sym).reindex(idx).ffill(); bc = L.binance(coin).reindex(idx).ffill()
    wp = wc / L.usdt if q == 'TMN' else wc
    b = (np.log(wp) - np.log(bc)) * 100
    dev = (b - b.rolling(60, min_periods=30).median().shift(1)).to_numpy(); px = wc.to_numpy()
    traded = vol > 0
    last_trade_age = np.zeros(len(idx)); age = 999
    for i in range(len(idx)):
        age = 0 if traded[i] else age + 1; last_trade_age[i] = age
    mk, tk = FEES[q]; hs = (spread.get(sym) or 0.3) / 2
    thr = max(1.0, tk + mk + 2 * hs + 0.3)
    for lat in (0, 1):
        i, N = 0, len(idx)
        while i < N - 18:
            if not np.isnan(dev[i]) and dev[i] < -thr:
                e = i + lat; j = e + 1
                while j < e + 15 and (np.isnan(dev[j]) or dev[j] < 0):
                    j += 1
                conv = not np.isnan(dev[j]) and dev[j] >= 0
                g = (px[j] / px[e] - 1) * 100
                net = g - (tk + 2 * hs + (mk if conv else tk + hs))
                rows.append(dict(pair=sym, lat=lat, fresh=bool(traded[e]), age=last_trade_age[e], net=net))
                i = j + 1
            else:
                i += 1
d = pd.DataFrame(rows)
for lat in (0, 1):
    x = d[d.lat == lat]
    print(f"\nlat={lat}: {len(x)} signals ({len(x)/days:.1f}/day)")
    for name, m in (('Wallex traded in the entry minute (fresh price)', x.fresh), ('no trade in the entry minute (stale price)', ~x.fresh)):
        y = x[m]
        print(f"  {name:48s} share {len(y)/len(x)*100:5.1f}%  median net {y.net.median():+.3f}%  mean (wins. 1%) {y.net.clip(-1,1).mean():+.3f}%  win {(y.net>0).mean()*100:5.1f}%")
    f = x[x.fresh]
    g = f.groupby('pair').net.agg(['size', 'median'])
    print('  fresh-price signals by pair (n, median net):', ' '.join(f"{p}:{int(r['size'])}/{r['median']:+.2f}" for p, r in g.iterrows()))
