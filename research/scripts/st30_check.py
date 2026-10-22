"""Check the other session's st30 Supertrend results: regime, long-only, multiple testing."""
import glob, sys
import numpy as np, pandas as pd

D = sys.argv[1]  # other session's scratchpad
rows = []
for f in sorted(glob.glob(f"{D}/work/st30/*/ta_results.csv")):
    coin = f.split('/')[-2]
    r = pd.read_csv(f)
    # buy & hold per period from the 1m data
    cols = ["t", "o", "h", "l", "c", "v", "ct", "qv", "n", "tbv", "tbq", "x"]
    df = pd.concat(pd.read_csv(p, header=None, names=cols, usecols=[0, 4]) for p in sorted(glob.glob(f"{D}/data/{coin}USDT/*.csv")))
    t = df["t"].to_numpy(); df.index = pd.to_datetime(np.where(t > 10**14, t // 1000, t), unit="ms")
    c = df["c"].sort_index()
    bh = lambda a, b: 100 * (c.loc[a:b].iloc[-1] / c.loc[a:b].iloc[0] - 1)
    fx = r[(r.params == "(14, 3.0)") & (r.exit == "SL 2.0ATR, TP 4.0ATR")].set_index("sides")
    wide = r.exit.str.contains(r"SL 2.0ATR, TP 4.0ATR|SL 2.5ATR, TP 5.0ATR")
    lw = r[(r.sides == "long") & wide]
    rows.append({"coin": coin,
                 "hold_tune%": bh("2026-08-21", "2026-10-05"), "hold_test%": bh("2026-10-06", "2026-10-20"),
                 "both_test%": fx.loc["both", "test_return_%"], "long_test%": fx.loc["long", "test_return_%"],
                 "long_tune%": fx.loc["long", "tune_return_%"], "long_full%": fx.loc["long", "full_return_%"],
                 "long_test_trades": fx.loc["long", "test_trades"],
                 "long_family_test_med%": lw["test_return_%"].median(),
                 "all96_both_halves+%": 100 * ((r["tune_return_%"] > 0) & (r["test_return_%"] > 0)).mean(),
                 "all96_full+%": 100 * (r["full_return_%"] > 0).mean()})
d = pd.DataFrame(rows).sort_values("hold_test%")
pd.set_option("display.width", 250)
print(d.round(2).to_string(index=False))
print("\nmean over coins:", d.drop(columns="coin").mean().round(2).to_dict())
print("corr(hold_test, both_test) =", round(d["hold_test%"].corr(d["both_test%"]), 2),
      " corr(hold_test, long_test) =", round(d["hold_test%"].corr(d["long_test%"]), 2))
# persistence: does tune performance predict test performance across all 2016 configs?
allr = pd.concat(pd.read_csv(f).assign(coin=f.split('/')[-2]) for f in glob.glob(f"{D}/work/st30/*/ta_results.csv"))
print("\nall configs:", len(allr), " corr(tune_return, test_return) =", round(allr["tune_return_%"].corr(allr["test_return_%"]), 3),
      " rank corr =", round(allr["tune_return_%"].rank().corr(allr["test_return_%"].rank()), 3))
for side in ("both", "long"):
    a = allr[allr.sides == side]
    top = a[a["tune_return_%"] >= a["tune_return_%"].quantile(0.9)]
    bot = a[a["tune_return_%"] <= a["tune_return_%"].quantile(0.1)]
    print(f"  {side:4s}: test mean of top-10% tune configs {top['test_return_%'].mean():+.2f}%  vs bottom-10% {bot['test_return_%'].mean():+.2f}%  vs all {a['test_return_%'].mean():+.2f}%")
