"""Random-entry baseline for the tune window (same signal counts, exits, original costs)."""
import sys, glob, importlib.util
import numpy as np
spec = importlib.util.spec_from_file_location("rr", "scripts/st30_rerun_lib.py"); rr = importlib.util.module_from_spec(spec); spec.loader.exec_module(rr)
D = sys.argv[1]; rng = np.random.default_rng(11)
res = {"st_both": [], "st_long": [], "rnd_both": [], "rnd_long": []}
for coin in sorted(c.split('/')[-2] for c in glob.glob(f"{D}/work/st30/*/ta_results.csv")):
    d = rr.load(coin); a = rr.atr(d).to_numpy(); s = rr.supertrend_sig(d)
    args = (0.001, 0.001, 0.0018, rr.START, rr.SPLIT)
    res["st_both"].append(rr.backtest(d, a, s, False, *args)[0]); res["st_long"].append(rr.backtest(d, a, s, True, *args)[0])
    nl, ns = int((s == 1).sum()), int((s == -1).sum()); rb, rl = [], []
    for k in range(20):
        r = np.zeros_like(s); p = rng.choice(len(s), nl + ns, replace=False); r[p[:nl]] = 1; r[p[nl:]] = -1
        rb.append(rr.backtest(d, a, r, False, *args)[0]); rl.append(rr.backtest(d, a, r, True, *args)[0])
    res["rnd_both"].append(np.mean(rb)); res["rnd_long"].append(np.mean(rl))
print("TUNE window (Aug 21 - Oct 5), mean % per coin over", len(res["st_both"]), "coins:", {k: round(float(np.mean(v)), 2) for k, v in res.items()})
print("coins where Supertrend beat random (both / long):", sum(x > y for x, y in zip(res["st_both"], res["rnd_both"])), "/", sum(x > y for x, y in zip(res["st_long"], res["rnd_long"])))
