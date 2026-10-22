"""Supertrend(14,3) 30m long+short with Wallex margin costs, on coins Wallex lets you short in TMN.

Margin costs from Wallex's public dry-run (2026-10-21): 0.25% trade fee per side, 0.015% open fee,
0.015% extension fee per started 4 hours, minimum position 3M toman. Half-spreads from a live
Wallex order-book snapshot where known, else 0.15%.
"""
import math, sys, importlib.util
import numpy as np, pandas as pd
spec = importlib.util.spec_from_file_location("rr", "scripts/st30_rerun_lib.py"); rr = importlib.util.module_from_spec(spec); spec.loader.exec_module(rr)

FEE, OPEN, EXT4H, SLIP = 0.0025, 0.00015, 0.00015, 0.0005
HALF_SPREAD = {"BTC": 0.0003, "ETH": 0.0018, "SOL": 0.002, "XRP": 0.003, "PEPE": 0.001, "DOGE": 0.0015, "BNB": 0.0018, "ARB": 0.0013}
COINS = ["ADA", "ARB", "BNB", "BTC", "DOGE", "DOT", "ETH", "PEPE", "SHIB", "SOL", "XRP"]  # TMN margin markets on Wallex

def bt(d, a, s, hs, start, end, sl_k=2.0, tp_k=4.0, min_frac=0.0):
    o, h, l, c = d.o.to_numpy(), d.h.to_numpy(), d.l.to_numpy(), d.c.to_numpy()
    idx = d.index; max_bars = 144
    lo_i, hi_i = idx.searchsorted(pd.Timestamp(start)), idx.searchsorted(pd.Timestamp(end))
    eq, peak, mdd, n, pos = 1.0, 1.0, 0.0, 0, None
    for i in range(lo_i, hi_i):
        if pos is not None:
            dirn, entry, stop, tgt, qty, n0 = pos; ex = None
            if dirn == 1:
                if l[i] <= stop: ex, mk = min(o[i], stop), False
                elif h[i] >= tgt: ex, mk = max(o[i], tgt), True
            else:
                if h[i] >= stop: ex, mk = max(o[i], stop), False
                elif l[i] <= tgt: ex, mk = min(o[i], tgt), True
            if ex is None and i - n0 >= max_bars: ex, mk = c[i], False
            if ex is not None:
                blocks = math.ceil((i - n0 + 1) * 0.5 / 4)
                cost = qty * entry * (FEE + OPEN + hs + EXT4H * blocks) + qty * ex * (FEE + hs + (0 if mk else SLIP))
                eq += qty * (ex - entry) * dirn - cost; n += 1; pos = None
        if pos is None and i + 1 < hi_i and s[i] != 0 and np.isfinite(a[i]):
            dirn, entry = int(s[i]), o[i + 1]; dist = sl_k * a[i]
            qty = min(eq * 0.01 / dist, eq / entry)
            qty = max(qty, eq * min_frac / entry)  # Wallex minimum position (3M of 5M toman = 0.6 of equity)
            pos = (dirn, entry, entry - dirn * dist, entry + dirn * tp_k * a[i], qty, i + 1)
        mark = eq + (pos[4] * (c[i] - pos[1]) * pos[0] if pos else 0.0)
        peak = max(peak, mark); mdd = max(mdd, 1 - mark / peak)
    return 100 * (eq - 1), n, 100 * mdd

rows = []
for coin in COINS:
    d = rr.load(coin); a = rr.atr(d).to_numpy(); s = rr.supertrend_sig(d); hs = HALF_SPREAD.get(coin, 0.0015)
    r = {"coin": coin, "half_spread%": hs * 100}
    for name, (st, en) in {"tune": (rr.START, rr.SPLIT), "test": (rr.SPLIT, rr.END), "full": (rr.START, rr.END)}.items():
        ret, n, dd = bt(d, a, s, hs, st, en, min_frac=0.6)
        r[f"{name}%"], r[f"{name}_trades"], r[f"{name}_dd%"] = ret, n, dd
    ret0, _, _ = bt(d, a, s, 0.0, rr.START, rr.END, min_frac=0.6)
    r["full_no_spread%"] = ret0
    rows.append(r)
df = pd.DataFrame(rows)
pd.set_option("display.width", 250)
print(df.round(2).to_string(index=False))
print("\nmean:", df.drop(columns="coin").mean().round(2).to_dict())
print("coins positive  tune:", int((df['tune%'] > 0).sum()), " test:", int((df['test%'] > 0).sum()), " full:", int((df['full%'] > 0).sum()), " full without spread:", int((df['full_no_spread%'] > 0).sum()), "of", len(df))
