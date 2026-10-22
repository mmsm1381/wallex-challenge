"""Re-run the fixed Supertrend(14,3) 30m setting with Wallex-like costs and a random-entry baseline.

Backtest logic copied from the other session's st30_backtest.py (next-bar entry, stop before target,
3-day max hold, 1% risk, 1x max), with the costs made into parameters.
"""
import glob, sys
import numpy as np, pandas as pd

D = sys.argv[1] if len(sys.argv) > 1 else ""
START, SPLIT, END = "2026-08-21", "2026-10-06", "2026-10-21"
RISK, MAX_LEV, MAX_HOLD_H, SHORT_APR = 0.01, 1.0, 72, 0.10

def load(coin):
    cols = ["t", "o", "h", "l", "c", "v", "ct", "qv", "n", "tbv", "tbq", "x"]
    df = pd.concat(pd.read_csv(p, header=None, names=cols, usecols=range(6)) for p in sorted(glob.glob(f"{D}/data/{coin}USDT/*.csv")))
    t = df["t"].to_numpy(); df.index = pd.to_datetime(np.where(t > 10**14, t // 1000, t), unit="ms")
    m1 = df[["o", "h", "l", "c", "v"]].sort_index().loc[lambda d: ~d.index.duplicated()]
    return m1.resample("30min").agg({"o": "first", "h": "max", "l": "min", "c": "last", "v": "sum"}).dropna()

def atr(d, n=14):
    tr = pd.concat([d.h - d.l, (d.h - d.c.shift()).abs(), (d.l - d.c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()

def supertrend_sig(d, n=14, mult=3.0):
    a = atr(d, n).to_numpy(); hl2 = ((d.h + d.l) / 2).to_numpy(); c = d.c.to_numpy()
    up, dn = hl2 - mult * a, hl2 + mult * a
    st = np.ones(len(c))
    for i in range(1, len(c)):
        up[i] = max(up[i], up[i - 1]) if c[i - 1] > up[i - 1] else up[i]
        dn[i] = min(dn[i], dn[i - 1]) if c[i - 1] < dn[i - 1] else dn[i]
        st[i] = 1 if c[i] > dn[i - 1] else (-1 if c[i] < up[i - 1] else st[i - 1])
    sig = np.zeros(len(c)); sig[1:] = np.where((st[1:] == 1) & (st[:-1] == -1), 1, np.where((st[1:] == -1) & (st[:-1] == 1), -1, 0))
    return sig

def backtest(d, a, s, long_only, entry_cost, maker_exit_cost, taker_exit_cost, start, end, sl_k=2.0, tp_k=4.0):
    o, h, l, c = d.o.to_numpy(), d.h.to_numpy(), d.l.to_numpy(), d.c.to_numpy()
    idx = d.index; bar_h = 0.5; max_bars = int(MAX_HOLD_H / bar_h)
    lo_i, hi_i = idx.searchsorted(pd.Timestamp(start)), idx.searchsorted(pd.Timestamp(end))
    eq, peak, mdd, n_tr, pos = 1.0, 1.0, 0.0, 0, None
    for i in range(lo_i, hi_i):
        if pos is not None:
            dirn, entry, stop, tgt, qty, n0 = pos
            exit_px = None
            if dirn == 1:
                if l[i] <= stop: exit_px, mk = min(o[i], stop), False
                elif h[i] >= tgt: exit_px, mk = max(o[i], tgt), True
            else:
                if h[i] >= stop: exit_px, mk = max(o[i], stop), False
                elif l[i] <= tgt: exit_px, mk = min(o[i], tgt), True
            if exit_px is None and i - n0 >= max_bars: exit_px, mk = c[i], False
            if exit_px is not None:
                interest = qty * entry * SHORT_APR * (i - n0 + 1) * bar_h / 8760 if dirn == -1 else 0.0
                eq += qty * (exit_px - entry) * dirn - qty * entry * entry_cost - qty * exit_px * (maker_exit_cost if mk else taker_exit_cost) - interest
                n_tr += 1; pos = None
        if pos is None and i + 1 < hi_i and s[i] != 0 and (not long_only or s[i] == 1) and np.isfinite(a[i]):
            dirn, entry = int(s[i]), o[i + 1]; dist = sl_k * a[i]
            qty = min(eq * RISK / dist, eq * MAX_LEV / entry)
            pos = (dirn, entry, entry - dirn * dist, entry + dirn * tp_k * a[i], qty, i + 1)
        mark = eq + (pos[4] * (c[i] - pos[1]) * pos[0] if pos else 0.0)
        peak = max(peak, mark); mdd = max(mdd, 1 - mark / peak)
    return 100 * (eq - 1), n_tr

