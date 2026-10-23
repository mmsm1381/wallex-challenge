"""Kronos second opinion at each sweep fill: does the forecast tell good fills from bad ones?

The study reads the fills of `sweep_study.py` (depth 1.0%, both filters) and
asks Kronos (github.com/shiyu-coder/Kronos, MIT) for a forecast at each fill.

Rules:

* Input: the last LOOKBACK 1m candles before the fill minute (up to t-1), so
  the forecast does not see the fill minute. Source "wallex" uses the Wallex
  OHLCV of the market. Source "binance" uses the Binance OHLC of the coin.
* Output: N_PATHS sampled paths of the next 15 minutes (the time limit of a
  trade). Each path is one batch row, so the paths are independent samples.
* Per fill: the mean forecast return at minute 15 (from the last close), the
  share of paths that end up, and the spread (standard deviation) of the
  path returns.

Settings (environment): KRONOS_LOOKBACK (128), KRONOS_PATHS (6),
KRONOS_LIMIT (0 = all fills).

Usage:
    python -I research/scripts/kronos_second_opinion.py DATA_DIR KRONOS_REPO WEIGHTS_DIR \
        SOURCE MODEL OUT_JSON
DATA_DIR has `wallex_ohlc.json`, `binance/` and `sweep_events.json` from
`sweep_study.py DATA_DIR 1.0 1.0`. MODEL is "small" or "mini".
"""
import json
import os
import sys
import time

import numpy as np
import pandas as pd
import torch

DATA, REPO, WEIGHTS, SOURCE, MODEL, OUT = sys.argv[1:7]
sys.path.insert(0, REPO)
from model import Kronos, KronosPredictor, KronosTokenizer  # noqa: E402

LOOKBACK = int(os.environ.get("KRONOS_LOOKBACK", "128"))
PRED_LEN = 15
N_PATHS = int(os.environ.get("KRONOS_PATHS", "6"))
EVENTS_PER_BATCH = 16
LIMIT = int(os.environ.get("KRONOS_LIMIT", "0"))  # 0: all fills
DEPTH = 1.0
GLOBAL_DROP_PCT = -0.3
FX_SHOCK_PCT = 0.5

torch.manual_seed(0)
np.random.seed(0)
tok_name, ctx = (("Kronos-Tokenizer-2k", 2048) if MODEL == "mini"
                 else ("Kronos-Tokenizer-base", 512))
tokenizer = KronosTokenizer.from_pretrained(f"{WEIGHTS}/{tok_name}")
model = Kronos.from_pretrained(f"{WEIGHTS}/Kronos-{MODEL}")
predictor = KronosPredictor(model, tokenizer, device="cpu", max_context=ctx)

events = [e for e in json.load(open(f"{DATA}/sweep_events.json"))
          if e[1] == DEPTH and e[5] > GLOBAL_DROP_PCT and e[7] < FX_SHOCK_PCT]
wallex = json.load(open(f"{DATA}/wallex_ohlc.json"))["ohlc"]
binance_cache = {}


def history(market, t):
    """Returns the OHLCV frame of the LOOKBACK minutes before minute t, or None."""
    minutes = [t - 60 * k for k in range(LOOKBACK, 0, -1)]
    if SOURCE == "wallex":
        rows = wallex[market]
        if any(str(m) not in rows for m in minutes):
            return None
        data = [rows[str(m)] for m in minutes]
        cols = ["open", "high", "low", "close", "volume"]
    else:
        coin = market[:-3] if market.endswith("TMN") else market[:-4]
        if coin not in binance_cache:
            binance_cache[coin] = json.load(open(f"{DATA}/binance/{coin}USDT.json"))
        rows = binance_cache[coin]
        if any(str(m) not in rows for m in minutes):
            return None
        data = [rows[str(m)] for m in minutes]
        cols = ["open", "high", "low", "close"]
    frame = pd.DataFrame(data, columns=cols)
    return frame, pd.Series(pd.to_datetime(minutes, unit="s"))


usable = []
for e in events:
    h = history(e[0], e[2])
    if h is not None:
        usable.append((e, h))
if LIMIT:
    usable = usable[:LIMIT]
print(f"{len(events)} fills, {len(usable)} with {LOOKBACK} minutes of {SOURCE} history", flush=True)

results = []
start = time.time()
for i in range(0, len(usable), EVENTS_PER_BATCH):
    chunk = usable[i:i + EVENTS_PER_BATCH]
    dfs, xs, ys = [], [], []
    for e, (frame, stamps) in chunk:
        future = pd.Series(pd.to_datetime([e[2] + 60 * k for k in range(PRED_LEN)], unit="s"))
        for _ in range(N_PATHS):
            dfs.append(frame)
            xs.append(stamps)
            ys.append(future)
    preds = predictor.predict_batch(dfs, xs, ys, pred_len=PRED_LEN, T=1.0, top_p=0.9,
                                    sample_count=1, verbose=False)
    for j, (e, (frame, _)) in enumerate(chunk):
        last = frame["close"].iloc[-1]
        paths = preds[j * N_PATHS:(j + 1) * N_PATHS]
        rets = np.array([100 * (p["close"].iloc[-1] / last - 1) for p in paths])
        lows = np.array([100 * (p["low"].min() / last - 1) for p in paths])
        results.append({"market": e[0], "t": e[2], "net": e[3], "reason": e[4],
                        "f_mean": float(rets.mean()), "f_up": float((rets > 0).mean()),
                        "f_std": float(rets.std()), "f_low": float(lows.mean())})
    done = min(i + EVENTS_PER_BATCH, len(usable))
    print(f"  {done}/{len(usable)} fills, {time.time() - start:.0f} s", flush=True)

json.dump(results, open(OUT, "w"))
print(f"saved {len(results)} rows to {OUT}")
