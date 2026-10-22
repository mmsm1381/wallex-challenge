# Handoff: Wallex challenge and the lagcatcher shadow test

This document is for the next Claude session or a teammate. It holds everything needed to continue without the old chat. It was written on 2026-10-21 at about 20:30 UTC.

## 1. The task

The team enters the Wallex challenge of the StarCoach Agentic AI hackathon.

* 3 days of live algorithmic trading on a Wallex trade-credit account of 5,000,000 toman. That is about 19 USDT at 263,000 toman per USDT.
* The final value must be in toman. Every trade needs a decision log entry.
* The score is 300 points:
  * return rank 90 (only if the return is positive)
  * drawdown 30, activity 15, stability 15
  * strategy logic 40, real AI use 35, risk management 30
  * reproducibility 25, backtest and shadow test 20
* The prize is 80M toman for the top total score.
* Deliverables: a 5-minute demo video, a GitHub repo with an MD document, and the decision logs.

Account facts that the user confirmed:

* Spot only. **Margin is disabled**, so the bot can only buy and later sell.
* The organizers blocked many markets. The list is in `research/blocked_markets.txt`. 120 spot markets stay tradeable, among them BTC, ETH, XRP, SOL, PEPE, DOGE, BNB, ADA, SHIB, TRX, XAUT and USDTTMN.
* Fees: toman markets maker 0.25% and taker 0.30%. USDT markets maker 0.10% and taker 0.13%.

Still unknown: the challenge start date, the team members, and the liquidation limit and daily fee of the credit package.

## 2. What the research found

The scripts and data are in `research/` (see `research/README.md`).

1. **Prediction has no edge after fees.** The user's earlier BTC 5-minute model reached 52.6% accuracy. Another session tested 2,016 Supertrend settings on 21 coins. Long-only had no edge, and with Wallex fees every version lost money.
2. **Wallex prices lag global prices.** In 1-minute candles, Wallex moves only 15-35% of a Binance 1-minute move in the same minute. The rest comes over 1-5 minutes.
3. **The candle backtest looked good but is not proof.** This was a 60-day test, long-only, with real fees, on 15 pairs with a spread of 0.35% or less. A same-minute reaction to a 1% gap gave a median +0.24% per trade. One minute late gave -0.20%.
4. **Half of the candle signals were stale prices.** 46% of the signals had no trade in that minute, and they had zero edge. The real signals reverted inside one minute. Only live order books can show whether the cheap ask was buyable. This is why we built lagcatcher.
5. **USDT is a currency risk.** USDTTMN rose 141% in 12 months. Its 3-day change has a standard deviation of about 3%. A USDT float for the cheaper USDT markets carries this risk.

## 3. Strategy plan (version 2, not built yet)

* Long-only fair-value dip buying. The bot buys when the Wallex ask is below fair value by more than the cost hurdle and sells at fair value.
* Fair value = median of global mids x Wallex USDTTMN mid (toman markets) x exp(rolling premium).
* There are two books: USDT pairs (cheap fees, USDT float of about 30%, FX PnL reported separately) and toman pairs (no FX risk).
* AI parts:
  * a LightGBM model that separates real gaps from stale or thin ones
  * an LLM supervisor that sets pairs, thresholds, float size and pauses in a bounded JSON plan
  * a news watcher for toman shocks
  * a no-AI shadow twin that shows what the AI changes
* Hard limits: 1M toman per trade, at most 4 open, 1% stop, 15-minute time stop, daily loss stop -1.5%, equity floor -2.5% from peak or -4% from start. All coins go back to toman before the end.
* The plan waits for the shadow-test result. The hurdle must come from the data.

## 4. lagcatcher (built)

**What it is:** a paper-trading shadow test in `/home/mahdi-work/lagcatcher`. It sends no orders and uses no API key. See `LAGCATCHER.md` for the full description.

**What it does:**

* It reads live Wallex books and trades. Wallex v2 (Centrifugo) is the first choice, with a fallback to the old v1 websocket.
* It reads best quotes from Bybit, OKX, KuCoin and Gate.
* It paper-trades with the real fees:
  * `taker_0.3s`, `taker_1s`, `taker_3s`: buy the ask after a latency. The hurdle is max(0.6%, fees + 0.3%).
  * `resting`: a bid at fair value - (2 maker fees + 0.3%).
  * All of them exit with a resting sell at fair value, a 1% stop, or a 15-minute time stop.
* It records the gap episodes and the raw data.

**Markets:**

* USDT pairs: BTC ETH XRP SOL PEPE DOGE BNB ADA
* Toman pairs: BTC ETH XRP SOL PEPE DOGE BNB ADA SHIB TRX
* USDTTMN, which converts USDT to toman for the fair value

**Commands** (run from the project folder):

* `.venv/bin/python -m pytest -q` runs the 55 tests.
* `.venv/bin/python -m lagcatcher check --config F` checks the feeds for 20 s.
* `.venv/bin/python -m lagcatcher run --config F [--duration S]` runs the live shadow test.
* `.venv/bin/python -m lagcatcher report --out DIR` prints the live results.
* `.venv/bin/python -m lagcatcher replay --raw DIR --out DIR --config F` replays recorded raw data with a grid of 13 variants:
  * taker at 0.3 s and 1 s, with buffers 0, 0.05, 0.12, 0.22 and 0.37%
  * resting with buffers 0.05, 0.15 and 0.3%
  * gap episodes at 0.15, 0.25, 0.35 and 0.5%
  * A buffer of 0 means fees only: 0.23% on USDT pairs and 0.55% on toman pairs.

**Configs:** `config.toml` is for this machine. `config.server.toml` is for the server (OKX on, lower disk limits).

**Process records:**

* the plan: `docs/superpowers/plans/2026-10-21-lagcatcher-mvp.md`
* the build ledger with all rulings: `.superpowers/sdd/2026-10-21-lagcatcher-mvp/progress.md`
* The folder is not a git repo, and there are no commits. The user did not ask for them.

## 5. The server

* `root@65.109.212.3`: Hetzner, outside Iran, Ubuntu 22.04, Python 3.10, 2 cores, 3.8 GB RAM. The SSH key of this machine works (BatchMode).
* **The user's trading bot "cassandra" also runs on it. Do not touch it.**
  * It is `cassandra.service` (user cassandra, /opt/cassandra, data in /var/lib/cassandra, 32 GB), plus a `cassandra.timefeed` process.
  * Baseline: MainPID 343584, NRestarts 0, about 20% CPU, about 400 MB RAM.
  * Check it before and after each action with `systemctl show cassandra -p ActiveState -p MainPID -p NRestarts`.
* lagcatcher is in `/root/lagcatcher` with its own venv (made with uv and the system Python 3.10).
* It runs as the transient unit `lagcatcher-shadow`, started by `deploy/shadow_run.sh`.
  * Limits: CPUQuota 50%, MemoryMax 400M, IOWeight 10, nice 10.
  * The unit does not start at boot.
* **Current run:** started 2026-10-21 20:24:30 UTC for 18 hours. It ends by itself at about 2026-10-22 14:24 UTC (17:54 Tehran). The hard limit is 18h15m.
  * An earlier 8-hour run (19:46 to 20:24 UTC) was stopped to extend into Tehran daytime. Its data is in the same `data/` folder.
* **Commands on the server:**
  * `/root/lagcatcher/deploy/shadow_run.sh status | report | stop` (status, live results, stop now)
  * `journalctl -u lagcatcher-shadow -f` shows the log, with one status line per minute.
* **Disk warning:** the disk is 98% full (1053 MB free at 20:24 UTC). It shrinks by about 60-70 MB per hour.
  * Most of the growth is system logs: journald (512 MB and growing), syslog, ufw.log, and btmp (failed SSH logins).
  * lagcatcher writes only about 6 MB per hour.
  * At this rate the disk can be full around 10:00-12:00 UTC on 2026-10-22, and that would stop cassandra.
  * The lagcatcher disk guard turns raw recording off below 400 MB free and on again above 500 MB. It stops the run below 250 MB.
  * Recommendation, which needs the user's OK: `journalctl --vacuum-size=200M`, a `SystemMaxUse` limit for journald, and a check of the ufw logging and SSH brute force (fail2ban).

## 6. Results so far

The live paper strategies had 0 signals in the first 25 minutes.

The replay of the first 23 usable minutes (19:57 to 20:20 UTC, Tehran night, a quiet market):

| Variant | Trades | Net after fees |
|---|---|---|
| taker, fees-only hurdle (0.3 s and 1 s) | 1 (PEPEUSDT) | +0.009% (about break-even, the price came back fully) |
| taker, buffer of 0.05% or more | 0 | - |
| resting, 0.25% below fair value (USDT) | 1 (PEPEUSDT) | +0.045% |

Gap episodes in the same 23 minutes, over 18 markets:

* At 0.15% or more: about 25 (mostly XRPTMN, ADATMN, PEPETMN, ETHUSDT).
* At 0.25% or more: 3. At 0.35% or more: 1 (TRXTMN, 28 s). At 0.5%: none.
* Gaps last 1-35 s, longer than the bot latency.
* The depth at the gap is tens to hundreds of millions of toman, so size is not the limit.
* So far the limit is the gap size.

## 7. Next steps

1. **Collect the data** (from the project folder on this machine):
   * `rsync -az root@65.109.212.3:/root/lagcatcher/data/raw/ <local_raw_dir>/`
   * `.venv/bin/python -m lagcatcher replay --raw <local_raw_dir> --out <out_dir> --config config.server.toml`
   * The replay takes about 30 s per hour of data.
2. **Decide if the edge is real.** The bar: a realistic variant (taker 1 s, or resting) has a median net per trade above +0.05% after fees, and about 10 or more fills a day. Compare the median and the mean, so that one big trade does not decide it. Compare Tehran daytime with night.
3. If the edge is real, build the live bot from plan version 2 with the hurdle from the data. If not, test other ideas on the same raw data, such as market making on USDT pairs with the 0.10% maker fee, or deeper resting bids.
4. Ask the organizers about the liquidation limit, the daily fee, and the start and end times.
5. Ask the user before any disk cleanup on the server.

## 8. Gotchas

* **This machine** has an Iranian IP and a dead `ALL_PROXY=socks://127.0.0.1:10808`.
  * Use `curl --noproxy '*'`. Pass `proxy=None` to websockets.
  * The Binance and OKX APIs and Bybit REST are blocked here. The Bybit websocket, KuCoin, Gate and MEXC work.
  * The network sometimes has TLS timeouts.
* **Wallex v2 websocket:**
  * It answers HTTP 503 unless the client sets `compression=None` and a browser User-Agent.
  * It also answers 503 for minutes at a time from both Iran and the server.
  * The v1 endpoint `wss://api.wallex.ir/ws` works then. It closes each connection after 30 minutes and accepts at most 50 subscribe commands, so the feed uses groups of 16 markets.
* **Wallex trade field:** `isBuyOrder=true` means the buy order was the maker, so a seller took the trade.
* **Stale .pyc files:** after quick `sed` edits in the same second, Python can use an old `.pyc`. Delete `__pycache__` before you trust a test result.
* **pkill:** `pkill -f <pattern>` also kills your own shell when the pattern is in your command line.
* **Pyright:** it shows import errors because it does not use `.venv`. Ignore them.

## 9. Check at 2026-10-21 20:40 UTC (corrections)

* **Disk: the main growth is cassandra data, not system logs.** Section 5 is wrong about this.
  * Cassandra writes about 50-60 MB per hour, mostly `/var/lib/cassandra/data/books/btc-updown-5m`. It keeps all files (27 days, 22 GB of `changes`).
  * journald stays at 512 MB. The other logs grow only a few MB per hour. lagcatcher writes about 6 MB per hour.
  * The free space goes down about 50-60 MB per hour (1040 MB free at 20:39 UTC).
  * A journald vacuum to 200 MB frees about 310 MB once. It does not stop the growth. Only the user can decide about the cassandra data.
* **The USDTTMN market closes every night** (the user confirmed this). The last trade was at 20:00:30 UTC (23:30 Tehran). It opens again at 11:30 Tehran (08:00 UTC). The book stays at the last quotes (266720/266758), so the feed is correct.
  * During the close, the toman fair value uses a frozen USDTTMN mid. The USDT rate implied by the coin markets (median of coin TMN mid / coin USDT mid) moved only -0.03% to +0.09% from it in the first 40 minutes. The 60-minute premium can follow a slow drift, but a long night can give a larger error.
  * Fix to test in the replay: during the close, use the implied rate instead of the USDTTMN mid.
  * For the live bot: USDT and toman cannot be exchanged on USDTTMN at night. Plan the USDT float and the final conversion to toman before 23:30 Tehran.
* **TRXTMN gaps are a spread effect, not a lag.**
  * The TRXTMN spread changes between about 0.03% and 1.6%. The premium uses the mid, so it follows the wide-spread mid.
  * When a seller puts an ask near the bid, the tool shows a gap of 0.3-0.5%. That ask is still about 0.6% above the global price x USDTTMN.
  * Other markets with wide night spreads (median): SHIBTMN 1.27%, TRXTMN 0.77%, PEPEUSDT 0.65%, BNBUSDT 0.45%, SOLTMN 0.39%, DOGEUSDT 0.36%.
  * Report these markets separately in the replay, or use a premium that does not depend on the spread.
* The raw data has no hole at the restart (2 s). All 19 books and 4 venues are present.

## 10. Binance added, run restarted (2026-10-22 04:52 UTC)

* The night replay (20:24 to 04:28 UTC) showed no edge after fees. Only the taker at 0.3 s with a buffer of 0.22% was positive: 4 trades, median +0.22%. The resting bid lost in all variants. A fair value from one venue only (Bybit, OKX, KuCoin, or Gate) gave almost the same results.
* The 1% stop uses the best bid. On ADAUSDT, bots show bids for about 0.1 s, so the stop sold 5% lower three times. A live bot must not use the best bid of one moment for its stop.
* Binance is now a fifth venue (`feeds.run_binance`, bookTicker on `wss://data-stream.binance.vision`). Port 9443 of stream.binance.com times out from the server. The tests pass (57).
* The run started again at 04:51:54 UTC for 24 hours. It ends at about 04:52 UTC on 2026-10-23. The raw files append, so the data stays continuous.
* The disk had 17.8 GB free (52%) at 04:53 UTC after a cleanup that the user or cassandra did.

## 11. New paper rules and replay (2026-10-22, data to 08:58 UTC)

The live run at 08:35 UTC lost 330k toman on ADAUSDT in 90 s (12 taker fills, all stops at -2.5%). The cause:

* Wallex ADAUSDT has a bid near the global price and an ask about 2.5% above it. The mid-based premium was +1.2%, so fair value was too high.
* An ask at the global price looked like a 1.1% gap.
* The bid fell to 0.2101 for about 0.1 s many times. Each low bid fired the 1% stop. Nothing stopped a new buy after the stop.

Four new rules (local code, 63 tests pass):

| Rule | Config key | Default |
|---|---|---|
| The premium uses only mids of books with a spread up to this limit | `premium.max_spread_pct` | 0.3 |
| No entry when the Wallex spread is wider than this limit | `paper.max_entry_spread_pct` | 0.5 |
| The stop sells at once when fair value falls below the stop price, or when the best bid stays below it for this time | `exit.stop_confirm_s` | 5.0 |
| No new buy in the same market for this time after a stop | `exit.cooldown_s` | 300 |

* A stop confirm of 1 s was not enough. On ADAUSDT at 05:20 UTC, low bids stayed for 1.1 s. A confirm of 5 s gave the same result as a stop on fair value only: no stops in 12 hours.
* `ExitRules` and the strategy constructors use the old behavior when these values are not given.
* The live run on the server still uses the old code. The new code is not on the server.

Replay from 20:42 to 08:58 UTC (12.3 h), all four rules on:

| Strategy | Trades | Median net | Net toman |
|---|---|---|---|
| taker_0.3s_b0 | 18 | -0.20% | -49.6k |
| taker_0.3s_b0.22 | 6 | -0.35% | -18.9k |
| taker_1s_b0 | 13 | -0.26% | -47.9k |
| resting_b0.05 | 14 | -0.14% | -30.7k |
| resting_b0.3 | 4 | -0.03% | -5.0k |

With the old rules, the same data gave -413k (taker_0.3s_b0) and -496k (resting_b0.05).

* The rules remove the artifact losses. They do not make an edge. No variant has a positive median with more than 2 trades.
* Narrow markets only (no ADA, BNBUSDT, DOGEUSDT, PEPEUSDT, SHIBTMN, XRPTMN): taker_0.3s_b0 has 6 trades, median -0.09%. resting_b0.3 has 2 trades, both positive (+0.24%).
* ADAUSDT gave 177 of the 204 taker signals and 173 misses. Its ask flickers too.
* From 08:00 to 08:58 UTC (USDTTMN open), no taker filled in a narrow market.

## 12. New direction: deep resting bids that catch sweeps (2026-10-22)

The user stopped the lag-arbitrage direction. The new idea: keep bids far below fair value and catch large market sells that sweep the book and come back within seconds.

Order-book replay (20:42 to 09:16 UTC, `resting` with large buffers): the real depth below fair value is buffer + 2 maker fees (+0.5% on toman markets, +0.2% on USDT markets).

| Real depth (toman) | Trades | Median | Net at 1M per trade |
|---|---|---|---|
| 0.8% | 5 | +0.18% | +1.5k |
| 1.1% | 3 | +0.61% | +18.4k |
| 1.3% | 3 | +0.82% | +24.5k |
| 1.5% | 2 | +0.96% | +19.1k |

The three good fills were real sweeps: BTCTMN 01:40 and 07:03, PEPETMN 09:13 UTC.

60-day candle study (`sweep/sweeps.py` and `sweep/fetch.py` in the session scratchpad):

* Data: Wallex 1m OHLC for 18 markets and USDTTMN (udf API, 8 parallel workers, about 7 minutes), and Binance 1m klines from data.binance.vision.
* Rules: the bid uses the fair value of the minute before. A Wallex low strictly below the bid is a full fill, because our higher bid had priority. The exit is a maker sell at fair value when a later high is strictly above it, else a taker sell after 15 minutes.
* The study found the same two BTCTMN sweeps as the order-book replay.
* Losing cases:
  * Binance fell 0.3% or more in the same minute: 39% win, mean -0.55%. A real move, not a sweep.
  * USDTTMN shocks. On 10-01 at 05:36 UTC, USDTTMN fell 7% in one minute (the market opened at 05:30 that day). All toman markets lost about -4.4%.
* With both filters (skip when Binance fell 0.3% or more, skip toman markets when USDTTMN moved 0.5% or more in 5 minutes), at a depth of 1.3%: 5.0 fills a day, 81% win, median +0.67%, +25k toman a day at 1M per trade, maximum drawdown -11k. The last 31 days alone: +22.5k a day.
* The USDTTMN filter was chosen after we saw 10-01, so it is partly fitted. Both filters use information from the same minute.
* Open orders lock funds, so a 5M account can keep only about 5 bids of 1M. The top 5 markets of the first 30 days (ADATMN, ETHUSDT, TRXTMN, ETHTMN, PEPEUSDT) gave +11.6k a day in the last 31 days.

## 13. Repository prepared for GitHub (2026-10-22)

* The project is a git repository now. The project README is the root `README.md`. The old README of the tool is `docs/LAGCATCHER.md`.
* New documents: `AGENTS.md` (rules for agents), `docs/RESEARCH.md` (all results), `docs/DESIGN.md` (sweep bot design), `docs/IMPLEMENTATION_PLAN.md` (tasks), `docs/plan/sweep_plan.html` (team page).
* The sweep study scripts are in `research/scripts/` (`sweep_fetch.py`, `sweep_study.py`, `replay_deep_bids.py` and the other analysis scripts of this session).
* `data/` and `research/data/` are not in git. `research/README.md` tells how to make the data again.
* This file names the server address. Remove it before the repository becomes public.
