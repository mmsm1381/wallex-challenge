# Research results

This document collects the results that decide the strategy. Each result names the script and the data that produced it. `research/README.md` tells how to run the scripts.

All money figures use 1,000,000 toman per trade and the real fees of the challenge account:

| Quote | Maker | Taker | Round trip (maker in, maker out) |
|---|---|---|---|
| Toman (TMN) | 0.25% | 0.30% | 0.50% |
| USDT | 0.10% | 0.13% | 0.20% |

## 1. The scoring decides the shape of the strategy

The score has 300 points. Return gives 90 points, and only when the return is positive. The other 210 points are for drawdown (30), activity (15), stability (15), strategy logic (40), real AI use (35), risk management (30), reproducibility (25), and backtest and shadow test (20).

So the target is a small, steady, positive return, a small drawdown, regular trades, and a clear AI and risk story.

## 2. Dropped directions

| Idea | Evidence | Result |
|---|---|---|
| Buy the Wallex lag behind global prices as a taker | lagcatcher live paper test and replay, 2026-10-21 20:42 to 10-22 09:16 UTC, 13 variants | No variant had a positive median with more than 2 trades. Best taker: 18 trades, median -0.20%, -49.6k. |
| Same with a resting bid 0.5% to 0.8% below fair value | Same data | All variants negative (-5k to -31k after the rule fixes). |
| One exchange (Bybit, OKX, KuCoin, Gate or Binance) as the fair value | `replay_venue.py`, `leadlag.py` | Almost the same results for each venue. All venues move with Binance within 0.25 s. |
| Lag size | `leadlag.py`, 25 Binance moves of 5 bp or more in 1 s | Wallex USDT markets made 0% of the move after 1 s, 25% after 5 s, 58% after 30 s and 89% after 60 s. The lag is real, but the moves are small compared with the fees. |
| Loops inside Wallex: coin TMN against coin USDT x USDTTMN | `ideas_scan.py`, 13.7 h, one sample per second | The loop was above the 0.73% taker cost 0% of the time. The best 1% of samples reached +0.33%. |
| Hold USDT against the toman | 60 days of USDTTMN candles | +0.52% a day on average. 3-day windows: mean +1.49%, standard deviation 2.69%, worst -3.1%, best +8%, positive in 63%. This is a currency bet, not an edge. It stays an optional overlay. |

Artifacts found in the lag test, now fixed in `lagcatcher/shadow.py` and `lagcatcher/engine.py`:

* Bots on Wallex remove their bids for 0.1 to 2 s. A stop that sells on one low best bid sold up to 5% too low. The stop now needs fair value below the stop price, or a best bid below it for 5 s.
* Thin books (ADAUSDT, SHIBTMN, TRXTMN) gave a wrong premium, because the premium used the mid of a wide book. The premium now uses only books with a spread of 0.3% or less. No entry happens when the spread is wider than 0.5%.
* After a stop, a strategy bought the same market again at once, 12 times in 90 s. A 5-minute cooldown now follows each stop.

## 3. Chosen direction: deep resting bids that catch sweeps

A sweep is a large market sell that walks down the Wallex book in a few milliseconds. The price comes back within seconds. A bid that rests far below fair value fills in the sweep and sells back at fair value.

Example, BTCTMN on 2026-10-22 at 07:03:53 UTC (recorded by lagcatcher):

* 85 seller trades in 20 ms took the price from 22.01B to 21.66B toman, 1.5% below fair value (21.996B). One trade was 645M toman.
* 2.6 s later, a buyer paid 22.018B.
* A bid 1.3% below fair value filled and sold at fair value 1 s later: +0.82% after fees.

### 3.1 Order-book replay

Script: `replay_deep_bids.py`. Data: lagcatcher raw files, 2026-10-21 20:42 to 10-22 09:16 UTC (13.6 h).

| Depth below fair value (TMN) | Trades | Win | Median | Net |
|---|---|---|---|---|
| 0.8% | 5 | 60% | +0.18% | +1.5k |
| 0.9% | 4 | 75% | +0.34% | +5.8k |
| 1.0% | 4 | 75% | +0.45% | +9.9k |
| 1.1% | 3 | 100% | +0.61% | +18.4k |
| 1.3% | 3 | 100% | +0.82% | +24.5k |
| 1.5% | 2 | 100% | +0.96% | +19.1k |
| 1.7% and deeper | 0 | - | - | 0 |

The good fills were BTCTMN at 01:40, BTCTMN at 07:03 and PEPETMN at 09:13 UTC. The shallow bids also filled on real price moves in DOGEUSDT and ADAUSDT, and lost. In the replay the depth is set as a buffer: bid = fair value x (1 - 2 x maker fee - buffer). So the depth is the buffer + 0.5% on toman markets and the buffer + 0.2% on USDT markets.

### 3.2 60-day candle study

Script: `sweep_study.py`. Data: Wallex 1m OHLC for 18 markets and USDTTMN, and Binance 1m klines, 2026-08-22 to 2026-10-22. The script docstring gives the simulation rules. The two most important rules:

* The bid uses the fair value of the minute before, so it does not see the future.
* A fill needs a Wallex trade strictly below the bid. Such a trade proves that our higher bid filled first and in full.

The study finds the same two BTCTMN sweeps as the order-book replay.

Losing cases without filters, at a depth of 1.3%:

| Fills | Count | Win | Mean |
|---|---|---|---|
| Binance moved less than 0.3% in the same minute | 316 | 79% | +0.41% |
| Binance fell 0.3% or more in the same minute (a real move) | 85 | 39% | -0.55% |
| Binance rose 0.3% or more | 21 | 81% | +0.84% |

The worst day was 2026-10-01. At 05:36 UTC, USDTTMN fell from 253k to 235k toman in one minute (the market opened at 05:30 that day). All toman-market bids lost about -4.4%.

Two filters remove these cases:

* **Global filter:** no bid when Binance fell 0.3% or more in the last minute.
* **FX filter:** no bid on toman markets when USDTTMN moved 0.5% or more in the last 5 minutes.

Results with both filters:

| Depth | Fills/day | Win | Median | Net/day | Max drawdown | Net/day, last 31 days |
|---|---|---|---|---|---|---|
| 0.8% | 20.7 | 68% | +0.18% | +18.0k | -90k | +16.4k |
| 1.0% | 10.8 | 76% | +0.37% | +26.5k | -32k | +26.3k |
| **1.3%** | **5.0** | **81%** | **+0.67%** | **+25.4k** | **-11k** | **+22.5k** |
| 1.6% | 3.0 | 86% | +0.98% | +23.5k | -9k | +21.4k |
| 2.0% | 1.4 | 86% | +1.40% | +16.3k | -6k | +15.7k |
| 2.5% | 0.8 | 94% | +1.97% | +14.1k | -11k | +13.0k |

Without filters, the 1.3% depth gave +16.3k a day with a maximum drawdown of -186k.

Net per market at 1.3% with both filters (61 days): ADATMN +420k, BTCTMN +188k, ETHUSDT +166k, TRXTMN +120k, ETHTMN +118k, PEPEUSDT +116k, SHIBTMN +111k, ADAUSDT +83k. All 16 markets with fills were positive.

**Funds limit.** An open bid locks its funds, so a 5M account keeps about 5 bids of 1M. Out-of-sample test: the 5 best markets of the first 30 days (ADATMN, ETHUSDT, TRXTMN, ETHTMN, PEPEUSDT) gave 2.7 fills a day, 78% win, median +0.64%, **+11.6k a day** and a maximum drawdown of -16k in the last 31 days. This is the realistic expectation at 1M per bid.

### 3.3 Sweeps in the live recording

Script: `sweep_scan.py` with a minimum drop of 0.5%. Data: lagcatcher raw files, 2026-10-21 19:46 to 2026-10-22 11:46 UTC (16 h). The drop is measured from the best bid before the burst.

| Time (UTC, 10-22) | Market | Drop | Seller trades | Volume | Best bid after 60 s | Result for a bid 1.3% below fair value |
|---|---|---|---|---|---|---|
| 01:40 | BTCTMN | 1.48% | 37 | 381M toman | -0.46% | Fill, +0.68% |
| 03:05 | ETHTMN | 0.70% | 12 | 130M toman | -0.54% | No fill. The price did not come back: a real move. |
| 07:03 | BTCTMN | 1.59% | 85 | 1,820M toman | 0.00% | Fill, +0.82% |
| 09:13 | PEPETMN | 0.80% and 0.85% (two bursts) | 14 and 8 | 345M and 124M toman | +0.09% | Fill, +0.95% |
| 10:06 | ADAUSDT | 0.55% | 3 | 6M toman | +0.09% | No fill. Too small. |

* From 10:06 to 11:46 UTC, no burst dropped 0.5% or more.
* 3 usable sweeps in 16 h is fewer than the 5 fills a day of the backtest. ADATMN gave a third of the backtest fills, and it had no sweep in this recording. One quiet day is inside the normal range of the backtest. Keep the count every day.
* All three usable sweeps came back fast and fully. The one burst that did not come back (ETHTMN 03:05) did not reach a 1.3% bid.
* Sweeps come in clusters with long quiet times between them. The bot must keep its bids up day and night.

### 3.4 Depth 1.0% against 1.3%

Data: `sweep_study.py`, both filters, 1M toman per trade.

| Depth | Fills/day | Win | Median | Net/day | Max drawdown | Time exits (mean) |
|---|---|---|---|---|---|---|
| 1.0%, all 18 markets | 10.8 | 76% | +0.37% | +26.5k | -32k | 144 of 658 (-0.36%) |
| 1.3%, all 18 markets | 5.0 | 81% | +0.67% | +25.4k | -11k | 76 of 306 (-0.20%) |
| 1.0%, 5 markets, out of sample | 6.1 | 77% | +0.40% | +14.1k | -33k | |
| 1.3%, 5 markets, out of sample | 2.7 | 78% | +0.64% | +11.6k | -16k | |

The 5 markets were chosen on the first 30 days and tested on the last 31 days. At 1.0% they were PEPEUSDT, ETHUSDT, ADATMN, TRXTMN and ETHTMN. At 1.3% they were the same 5 markets.

On the live recording, a 1.0% bid on toman markets catches the same three sweeps with less profit (+0.38%, +0.51%, +0.65%, about +15k against +24.5k at 1.3%). It finds no extra sweep.

Conclusions:

* 1.0% gives about twice the trades and a little more net per day. This helps the activity score.
* 1.0% has a smaller margin over the fees (median +0.37% against a toman round trip of 0.50%), more trades that end at the time limit, and three times the drawdown. This hurts the drawdown and stability scores.
* Proposal, **not tested yet**: 1.3% on toman markets (round trip 0.50%) and 1.0% on USDT markets (round trip 0.20%). The supervisor can move each market between 1.0% and 1.6%. `IMPLEMENTATION_PLAN.md` task 0.5 is the backtest of this proposal.

### 3.5 USDT rate from other Iranian exchanges (2026-10-23)

Question: does a USDT/toman rate from other Iranian exchanges give a better fair value than the Wallex USDTTMN market alone?

Data access (from Iran and from the server):

* Bitpin: 1m USDT/IRT bars for 61 days (`api.bitpin.ir/v1/mkt/tv/get_bars`, no key). Used.
* Tabdeal: no USDT chart. The answer says the central bank ordered the price charts off. Only the live book and the last trades are available.
* Nobitex: `api.nobitex.ir` does not connect, and `apiv2.nobitex.ir` gives a Cloudflare page.

Scripts: `bitpin_fetch.py`, `usdt_fv_study.py`, then `sweep_study.py` on the variant folders.

Wallex USDTTMN against Bitpin, 68,363 minutes where both traded:

* Basis Wallex/Bitpin: p5 -0.48%, median +0.04%, p95 +0.49%.
* The correlation of the 1m returns is low at every lag (+0.08 at lag 0, +0.07 when Bitpin leads by 1 minute, +0.06 when Wallex leads). Neither exchange leads the other in a useful way.
* Bitpin USDT also stops trading at night now (about half of the recent minutes have no volume), so it does not help during the nightly USDTTMN close.

Sweep study on the coin markets with three USDT rates (both filters, all markets):

| USDT rate | Depth 1.0%: net/day, max drawdown | Depth 1.3%: net/day, max drawdown | 5 markets, last 31 days |
|---|---|---|---|
| **Wallex USDTTMN (current)** | **+26.6k, -32k** | **+25.5k, -11k** | **+11.9k/day** |
| Bitpin USDT/IRT | +21.8k, -41k | +24.3k, -17k | +10.6k/day |
| Mean of Wallex and Bitpin | +25.3k, -43k | +24.1k, -17k | +10.1k/day |

Sweep bids on the Wallex USDTTMN market itself (depths 0.2% to 1.0%, fair value from the Wallex close or from Bitpin): every variant loses. The dips of USDTTMN do not come back within 15 minutes, so most trades end at the time limit at about -0.5% (the fees).

Conclusion: keep the Wallex USDTTMN rate. A rate from Bitpin, alone or in a mean, gives less profit and a larger drawdown. USDTTMN is not a market for sweep bids. Tabdeal and Nobitex can only be tested after a live recording of their books.

### 3.6 Limits of the evidence

* We chose the FX filter after we saw the 10-01 crash. It is partly fitted to that one event.
* Both filters use information from the same minute as the fill. A live bot sees these moves within milliseconds, but a sweep and a shock can come in the same second.
* Candles do not show the order of trades inside a minute. The order-book replay has only 3 good events so far.
* Other bots can rest bids at the same price before us. Then they fill first. The candle study cannot see the queue.
* The live tool keeps recording. Each new day of raw data makes the order-book check stronger.

### 3.7 Market priority by fill count (2026-10-23)

Question: two markets qualify for one place, and the funds allow only one bid. Which market gets the bid?

Rule (the user's choice): the market with more fills in the backtest gets the place. Equal counts use the net result.

Script: `sweep_study.py DATA_DIR 1.0 1.0`, depth 1.0% (the live depth), both filters, 61 days.

| Market | Fills | Win | Median | Net (1M per trade) |
|---|---|---|---|---|
| ADATMN | 232 | 72% | +0.34% | +354k |
| PEPETMN | 80 | 56% | +0.17% | +35k |
| SHIBTMN | 59 | 71% | +0.33% | +100k |
| PEPEUSDT | 55 | 73% | +0.56% | +214k |
| TRXTMN | 45 | 89% | +0.44% | +149k |
| ETHTMN | 38 | 92% | +0.39% | +108k |
| BTCTMN | 34 | 91% | +0.45% | +121k |
| ETHUSDT | 27 | 89% | +0.72% | +158k |
| DOGETMN | 25 | 84% | +0.33% | +71k |
| XRPTMN | 12 | 92% | +0.33% | +44k |
| ADAUSDT | 11 | 91% | +0.78% | +81k |
| DOGEUSDT | 10 | 70% | +0.95% | +64k |
| SOLTMN | 9 | 100% | +0.29% | +26k |
| BTCUSDT | 6 | 83% | +0.61% | +34k |
| XRPUSDT | 6 | 67% | +0.46% | +17k |
| BNBTMN | 5 | 100% | +0.15% | +14k |
| SOLUSDT | 1 | 100% | +1.10% | +11k |
| BNBUSDT | 0 | - | - | - |

`allocation.ranking` in `sweepbot/config.py` uses this order.

Effect with the live places (6 toman, 4 USDT) when all markets qualify:

* Toman: PEPETMN replaces DOGETMN. The 6 toman markets had 488 fills in 61 days instead of 433 (about +0.9 a day). Their net was +867k instead of +903k (about -0.6k a day).
* USDT: the same 4 markets as before (PEPEUSDT, ETHUSDT, ADAUSDT, DOGEUSDT). Only the order changes.
* The order also decides which market keeps a bid when some markets do not qualify (wide spread, filter, cooldown).

Limits:

* PEPETMN has the weakest quality of the top markets (56% win, median +0.17%). More fills do not mean more profit.
* The counts are in-sample. The study counts each market alone. It does not simulate the places.

### 3.8 Kronos second opinion at each fill (2026-10-23)

Question: does a forecast of the Kronos model tell good fills from bad fills? The plan was to log the forecast next to each trade as a second opinion, without a trade decision.

Model: Kronos-mini (4.1M parameters, MIT license, github.com/shiyu-coder/Kronos, weights `NeoQuasar/Kronos-mini` on HuggingFace). It is a transformer, pretrained on 1m to daily candles of many exchanges.

Scripts: `kronos_second_opinion.py` (forecasts), then `kronos_eval.py` (evaluation). Data: the fills of `sweep_study.py DATA_DIR 1.0 1.0` (depth 1.0%, both filters, 61 days).

Rules:

* Input: the last 128 1m candles before the fill minute. The forecast does not see the fill minute.
* Two inputs: the Wallex candles of the market (with volume), or the Binance candles of the coin.
* Output: 6 sampled paths of the next 15 minutes (the time limit of a trade).
* Signals per fill:
  * `f_mean`: mean forecast return at minute 15.
  * `f_up`: share of paths that end up.
  * `f_std`: spread of the path returns.
  * `f_low`: mean lowest forecast price.
* Run time: about 1 to 6 s per fill on 12 CPU cores. Kronos-small was about 7 times slower and was not run on all fills.

Rank correlation of each signal with the real net result per fill (permutation p-value in brackets):

| Signal | Wallex candles (640 fills) | Binance candles (655 fills) |
|---|---|---|
| `f_mean` (direction) | -0.04 (0.30) | +0.07 (0.07) |
| `f_up` (direction) | -0.03 (0.45) | +0.08 (0.03) |
| `f_std` (volatility) | +0.11 (0.01) | -0.02 (0.55) |
| `f_low` (volatility) | -0.14 (0.00) | -0.01 (0.84) |

Results:

* **Direction has no useful signal.** On Wallex candles, the correlation is zero. On Binance candles, `f_up` has +0.08 (p 0.03). Its lowest third won 73% against 80% for the highest third. With 8 tests, one p-value of 0.03 is expected by chance.
* **The volatility signals only repeat the recent volatility.** A simple statistic does better: the mean high-low range of the last 128 Wallex minutes has a rank correlation of +0.16 with the net result. After the realized volatility and range are removed, `f_std` keeps +0.04 and `f_low` keeps -0.08.
* **As a filter, it adds nothing.** A cut on `f_up`, chosen on the first 30 days, is "no cut" for both inputs. So the last 30 days do not change (+25.3k a day on Wallex candles, +26.4k on Binance candles).

Conclusion: Kronos does not tell good fills from bad fills. Do not use it for a trade decision. A logged second opinion is free of trade risk, but it carries no information. If the write-up mentions it, it must say this result.

## 4. Market facts

* Wallex USDTTMN closes every night from 23:30 to 11:30 Tehran time (20:00 to 08:00 UTC). On some days it opens earlier (05:30 UTC on 2026-10-01). During the close, coin TMN markets keep trading.
* Median daytime spreads (2026-10-22, 05:00 to 09:00 UTC): BTCTMN 0.05%, BTCUSDT 0.06%, PEPETMN 0.07%, ETHTMN 0.12%, ETHUSDT 0.11%, SOLUSDT 0.17%, XRPUSDT 0.18%, DOGETMN 0.19%, BNBTMN 0.22%, TRXTMN 0.23%, SOLTMN 0.25%, XRPTMN 0.31%, DOGEUSDT 0.35%, BNBUSDT 0.36%, ADATMN 0.43%, PEPEUSDT 0.45%, ADAUSDT 0.50%, SHIBTMN 0.85%.
* Trade flow (13.7 h): USDTTMN about 5.5B toman an hour, BTCTMN 4.9B, PEPETMN 4.2B, ETHTMN 3.5B, BTCUSDT 3.4B. ADAUSDT, DOGEUSDT and SHIBTMN have less than 0.2B an hour.
* Binance bookTicker works from the server on port 443 (`data-stream.binance.vision`). Port 9443 times out. PEPE, SHIB, ADA and TRX have coarse Binance ticks (spread 3 to 22 bp), so their Binance mid jumps by half a tick.
