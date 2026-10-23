"""USDT fair value from other Iranian exchanges: is it better than Wallex alone?

Inputs: the Wallex 1m OHLC of `sweep_fetch.py` (USDTTMN) and the Bitpin 1m
USDT/IRT bars of `bitpin_fetch.py`.

Part 1. Wallex USDTTMN against Bitpin: the basis and the lead-lag of the 1m
returns.

Part 2. Sweep bids on Wallex USDTTMN itself. The bid is FV x (1 - depth). It
fills when the Wallex low of the minute is strictly below it. The exit is a
maker sell at FV when a later high is strictly above it (15 minutes), else a
taker sell at the close of minute 15 minus 0.1%. Fees: toman market, maker
0.25%, taker 0.30%. Size 1M toman. Two fair values:
  * own: the Wallex close of the minute before.
  * bitpin: the Bitpin close of the minute before x exp(premium). The premium
    is the median of log(Wallex close / Bitpin close) over the 60 minutes
    before, over minutes where both traded.
Filter for both: skip the fill when Bitpin fell 0.3% or more in the minute.

Part 3 (the coin markets) is in `sweep_study.py`: run it on a data folder
whose USDTTMN candles are replaced (`--write-variants` writes such folders).

Usage:
    python3 -I research/scripts/usdt_fv_study.py WALLEX_OHLC_JSON BITPIN_JSON [--write-variants DIR]
"""
import bisect, json, math, os, statistics, sys

wallex_path, bitpin_path = sys.argv[1], sys.argv[2]
W_ALL = json.load(open(wallex_path))
W = {int(t): v for t, v in W_ALL["ohlc"]["USDTTMN"].items()}
B = {int(t): v for t, v in json.load(open(bitpin_path)).items()}
MAKER, TAKER = 0.0025, 0.0030

# Part 1 ---------------------------------------------------------------------
both = sorted(t for t in W if t in B and W[t][4] > 0 and B[t][4] > 0)
basis = [100 * (W[t][3] / B[t][3] - 1) for t in both]
q = lambda v, x: sorted(v)[int(x * (len(v) - 1))]
print(f"Minutes where both traded: {len(both)}")
print(f"Basis Wallex/Bitpin %: p5 {q(basis,.05):+.3f} median {q(basis,.5):+.3f} p95 {q(basis,.95):+.3f}")


def returns(src, ts):
    out = {}
    for t in ts:
        if t - 60 in src:
            out[t] = math.log(src[t][3] / src[t - 60][3])
    return out


rw, rb = returns(W, both), returns(B, both)
print("Correlation of 1m returns, Wallex at t against Bitpin at t+lag:")
for lag in (-3, -2, -1, 0, 1, 2, 3):
    pairs = [(rw[t], rb[t + 60 * lag]) for t in rw if t + 60 * lag in rb]
    if len(pairs) > 100:
        xs, ys = zip(*pairs)
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        cov = sum((x - mx) * (y - my) for x, y in pairs)
        sd = math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
        print(f"  lag {lag:+d} min: {cov / sd:+.3f}  (n={len(pairs)})")
print("  A positive lag with a high value means Bitpin moves after Wallex.")

# Part 2 ---------------------------------------------------------------------
w_ts = sorted(W)
b_ts = sorted(B)


def b_close(t):
    i = bisect.bisect_right(b_ts, t) - 1
    return B[b_ts[i]][3] if i >= 0 else None


fv_bitpin = {}
window = []
for t in w_ts:
    window = [(s, x) for s, x in window if t - s <= 3600]
    bc = b_close(t)
    if bc and len(window) >= 20:
        fv_bitpin[t] = bc * math.exp(statistics.median(x for _, x in window))
    if W[t][4] > 0 and t in B and B[t][4] > 0:
        window.append((t, math.log(W[t][3] / B[t][3])))
fv_own = {t: W[t][3] for t in w_ts}


def simulate(fv, depth):
    trades, busy = [], 0
    for t in w_ts:
        if t < busy or t - 60 not in fv or W[t][4] <= 0:
            continue
        bid = fv[t - 60] * (1 - depth / 100)
        if not W[t][2] < bid:
            continue
        bt, bp = B.get(t), B.get(t - 60)
        if bt and bp and bt[3] / bp[3] - 1 <= -0.003:
            continue
        exit_price, reason, end = None, "time", t + 900
        for k in range(1, 16):
            tk = t + 60 * k
            if tk in W and tk - 60 in fv and W[tk][1] > fv[tk - 60]:
                exit_price, reason, end = fv[tk - 60], "target", tk
                break
        if exit_price is None:
            i = bisect.bisect_right(w_ts, end) - 1
            exit_price = W[w_ts[i]][3] * 0.999
        fee = MAKER if reason == "target" else TAKER
        trades.append(100 * ((exit_price * (1 - fee)) / (bid * (1 + MAKER)) - 1))
        busy = end + 60
    return trades


days = (w_ts[-1] - w_ts[0]) / 86400
print(f"\nSweep bids on Wallex USDTTMN, {days:.0f} days, 1M toman per trade:")
print("fair value  depth   fills/day  win   median    mean   net/day")
for name, fv in (("own", fv_own), ("bitpin", fv_bitpin)):
    for depth in (0.2, 0.3, 0.5, 0.8, 1.0):
        n = simulate(fv, depth)
        if not n:
            print(f"{name:10s} {depth:5.1f}%   no fills")
            continue
        print(f"{name:10s} {depth:5.1f}%   {len(n)/days:8.2f}  {100*sum(x>0 for x in n)/len(n):3.0f}%"
              f"  {statistics.median(n):+.2f}%  {statistics.fmean(n):+.2f}%  {sum(n)*10/days:+7.1f}k")

# Variant data folders for sweep_study.py -----------------------------------
if "--write-variants" in sys.argv:
    out = sys.argv[sys.argv.index("--write-variants") + 1]
    src_dir = os.path.dirname(os.path.abspath(wallex_path))
    for name in ("bitpin", "mean"):
        folder = os.path.join(out, name)
        os.makedirs(folder, exist_ok=True)
        link = os.path.join(folder, "binance")
        if not os.path.exists(link):
            os.symlink(os.path.join(src_dir, "binance"), link)
        usdt = {}
        for t in w_ts:
            bt = B.get(t)
            if bt is None:
                continue
            w = W[t]
            if name == "bitpin":
                usdt[str(t)] = bt
            else:
                usdt[str(t)] = [(w[i] + bt[i]) / 2 for i in range(4)] + [w[4]]
        data = dict(W_ALL)
        data["ohlc"] = dict(W_ALL["ohlc"])
        data["ohlc"]["USDTTMN"] = usdt
        json.dump(data, open(os.path.join(folder, "wallex_ohlc.json"), "w"))
        print("wrote", folder)
