# Research scripts and data (Wallex challenge)

These scripts produced the results in `docs/RESEARCH.md`. They are one-off research code, not part of the `lagcatcher` package. The data files are not in git because they are large. Each section below says how to make the data again.

## 1. Sweep study (current direction)

| Script | What it does | Input |
|---|---|---|
| `scripts/sweep_fetch.py` | Downloads 61 days of 1m OHLC: Wallex (udf API, 8 parallel workers, about 7 minutes) and Binance (data.binance.vision daily klines) | network |
| `scripts/sweep_study.py` | Simulates resting bids at depths below fair value, with and without the two filters. Prints all tables of `docs/RESEARCH.md` section 3 | output of `sweep_fetch.py`, optional lagcatcher raw folder |
| `scripts/replay_deep_bids.py` | Replays lagcatcher raw order books with resting bids only, at the buffers in `RESTING_BUFFERS_PCT` | lagcatcher raw folder |
| `scripts/bitpin_fetch.py` | Downloads 1m USDT/IRT bars of Bitpin (no key) | network |
| `scripts/usdt_fv_study.py` | USDT rate from Bitpin against Wallex USDTTMN: basis, lead-lag, USDTTMN sweep bids, variant folders for `sweep_study.py` | Wallex OHLC, Bitpin bars |
| `scripts/sweep_scan.py` | Lists every sell sweep in the raw trades: drop, size, recovery after 5 s and 60 s | lagcatcher raw folder |

Commands (from the repo root, standard library only, about 8 minutes in total):

```bash
mkdir -p research/data/sweep
python3 -I research/scripts/sweep_fetch.py research/data/sweep
python3 -I research/scripts/sweep_study.py research/data/sweep 0.8,1.0,1.3,1.6,2.0,2.5 1.3 [RAW_DIR]
```

`RAW_DIR` is a local copy of the lagcatcher raw files (see section 3). It adds global prices for the current day, which data.binance.vision publishes only on the next day.

## 2. Lag-edge studies (dropped direction)

| Script | What it does | Input |
|---|---|---|
| `scripts/replay_rules.py` | Replays the 13-variant grid with the new paper rules on or off | lagcatcher raw folder |
| `scripts/replay_venue.py` | Same grid with the fair value from one venue only | lagcatcher raw folder |
| `scripts/leadlag.py` | Event study: share of each Binance move that other venues and Wallex made after 0.25 to 60 s | lagcatcher raw folder |
| `scripts/ideas_scan.py` | Loops inside Wallex (coin TMN vs coin USDT x USDTTMN) and trade flow per market | lagcatcher raw folder |
| `scripts/wallex_api_probe.py` | Read-only probe of the Wallex API for milestone 2: markets, headers, account answers, docs | `WALLEX_API_KEY` (optional) |
| `scripts/wallex_docs_fetch.py` | Downloads the Wallex API documentation (developers.wallex.ir) as one Markdown file | none |
| `scripts/spreads.py` | Time-weighted Wallex spread per market | lagcatcher raw folder |
| `scripts/datacheck.py` | Coverage, gaps and crossed books of the raw files | lagcatcher raw folder |
| `scripts/trace_market.py` | Prints book, venue mids, premium and gap of one market during a replay | lagcatcher raw folder |
| `scripts/snap.py` | One order-book snapshot of Wallex markets: spreads, depth, triangle gaps | live Wallex API |
| `scripts/fetch_candles2.py` | 7 days of 1m candles: Wallex (udf API) and Binance (data.binance.vision) | network |
| `scripts/analyze.py` | Lead-lag, basis, and rough convergence study (7 days) | `candles.json` |
| `scripts/fetch_wallex_60d.py` | 61 days of Wallex 1m closes and live spreads for 27 markets | network |
| `scripts/sim60*.py`, `scripts/sim60_lib.py` | 60-day long-only fair-value backtest with real fees, portfolio limits, weekly and outlier checks | `wallex60.json` and Binance 1m files |
| `scripts/stale_check.py` | Splits candle signals into fresh trades and stale prices | same as sim60 |
| `scripts/real_fees.py` | Gap trades with the challenge fees, 7 days | `candles.json` |
| `scripts/universe.py` | Tradeable markets after the blocked list | `blocked_markets.txt`, markets JSON |
| `scripts/st30_*.py` | Checks of another session's Supertrend study (regime, long-only, Wallex costs, random baseline) | that session's Binance data |
| `scripts/fv_two_sided.py` | Margin (short) version. Obsolete: margin is disabled | `candles.json` |

The scripts that import `lagcatcher` run from the repo root with `PYTHONPATH=. .venv/bin/python research/scripts/<name>.py`. The `sim60*` and `st30_*` scripts need numpy and pandas. The Binance 1m files that they read came from a temporary folder of an earlier session. Download them again from `https://data.binance.vision/data/spot/` (monthly and daily `klines/<SYMBOL>/1m/` files).

## 3. Data

Nothing in `research/data/` is in git.

* `data/sweep/`: output of `sweep_fetch.py` (about 90 MB).
* `data/candles.json`: Wallex and Binance 1m candles, 2026-10-14 to 10-20 (16 MB). Make it again with `fetch_candles2.py`.
* `data/wallex60.json`: Wallex 1m closes for 27 markets, 2026-08-21 to 10-21, plus live spreads (72 MB). Make it again with `fetch_wallex_60d.py`.
* lagcatcher raw files: they are on the shadow-test server in `/root/lagcatcher/data/raw/` (hourly gzip JSONL, about 6 MB per hour). Copy them with `rsync -a <server>:/root/lagcatcher/data/raw/ <local_raw_dir>/`. The recording started on 2026-10-21 at 19:46 UTC.

`blocked_markets.txt` is the list of markets that the challenge account cannot trade, from the organizers. It is in git.
