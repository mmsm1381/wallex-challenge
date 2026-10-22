# lagcatcher: the shadow-test tool

This document describes the `lagcatcher` package. The project README is `../README.md`. The lag edge that this tool was built to test showed no profit after fees (see `RESEARCH.md`). The tool stays useful: its feeds, fair value, recorder and replay are the base of the sweep bot (see `DESIGN.md`).

## The original question

This tool is for the team that enters the Wallex challenge. It answers one question with live data: can a bot buy Wallex coins below the global price, and make money after the real fees?

The tool sends **no orders** and uses **no API key**. It paper-trades on the real Wallex order books and trades, and it records all data for later backtests.

## What it measures

The tool computes a fair value for each market.

* Fair value = median best-quote mid of the enabled venues: Bybit, KuCoin, and Gate here, and also OKX and Binance on the server.
* For a toman market, it also multiplies by the Wallex USDTTMN mid.
* It also multiplies by exp(premium), where the premium is the rolling 60-minute median of log(Wallex mid / fair value). This removes the slow premium of each market. The premium uses only samples with a Wallex spread of 0.3% or less, because the mid of a wide book is far from the real price.

Two paper strategies use the challenge account fees. These are maker 0.25% and taker 0.30% on toman markets, and maker 0.10% and taker 0.13% on USDT markets.

| Strategy | Entry | What it tells us |
|---|---|---|
| `taker_0.3s`, `taker_1s`, `taker_3s` | It buys when the best ask is below fair value by more than fees + 0.3% (minimum 0.6%). The fill uses the real book 0.3, 1, or 3 seconds later. | Whether we win the speed race, and how fast the edge decays |
| `resting` | It keeps a bid at fair value - (2 maker fees + 0.3%). The bid fills only when a seller trades below it. | Whether patient bids catch Wallex sellers without a race |

All strategies exit with a resting sell at fair value (maker fee). They also have a 1% stop and a 15-minute time limit, both of which pay the taker fee. The stop sells when fair value falls below the stop price, or when the best bid stays below it for 5 s. Bots on Wallex often remove their bids for 0.1-2 s. After a stop, the strategy does not buy that market again for 5 minutes. No strategy buys when the Wallex spread is wider than 0.5%. Each position is 1,000,000 toman.

The **gap episodes** show how long a buyable gap stays on the book, and how much toman of asks it holds. The thresholds are 0.5% and 1% below fair value.

The fill rules are strict on purpose:

* A trade at exactly our price is not a fill.
* A taker order fills only from asks at or below its limit, after its latency.

Paper results are still an estimate. Our real orders would change the book a little.

## When is the edge real? (original criteria)

Run the tool for at least 24 hours, and better 48 hours. The edge is real when a realistic strategy (`taker_1s` or `resting`) shows all of these:

* a median net per trade above +0.05% after fees
* about 10 or more fills a day across the markets
* no dependence on a few large trades (compare the median and the mean)

## Run it on your computer

1. Create the environment: `uv venv .venv && uv pip install --python .venv/bin/python -e '.[dev]'`
2. Run the tests: `.venv/bin/python -m pytest -q`
3. Check the feeds for 20 seconds: `.venv/bin/python -m lagcatcher check`
4. Run the shadow test: `.venv/bin/python -m lagcatcher run`. Stop it with Ctrl+C.
5. Read the report: `.venv/bin/python -m lagcatcher report`

The first 10 minutes are a warm-up for the premium. No signals come before that.

## Run it on a server

The server needs Linux, Python 3.10 or newer, and SSH access. The server must reach Wallex and at least two global venues. A server in Iran is closest to Wallex, but Binance and OKX block Iranian IPs.

1. From this folder, run `deploy/push.sh user@host`. The script copies the code and runs `deploy/install.sh` on the server.
2. The installer creates the environment and runs a 20-second feed check. Then it installs and starts the `lagcatcher` systemd service.
3. Watch the log: `journalctl -u lagcatcher -f`. A status line comes every minute.

On the shared server, the shadow test runs as the transient unit `lagcatcher-shadow` instead. `deploy/shadow_run.sh` starts it with CPU, memory and IO limits. Use `deploy/shadow_run.sh status | report | stop`. Do not run `deploy/push.sh` there: it runs the installer and starts the permanent service.
4. Read the report on the server: `cd lagcatcher && .venv/bin/python -m lagcatcher report`

The service restarts after a crash or a reboot. Open positions are not saved across a restart.

## Output files

The tool writes into `data/` (see `run.out_dir` in `config.toml`).

* `events.jsonl` holds every paper signal, fill, miss, and exit, with the net % and net toman.
* `episodes.jsonl` holds the closed gap episodes.
* `status.log` holds the status line every minute.
* `raw/wallex_book-*.jsonl.gz`, `raw/wallex_trade-*.jsonl.gz`, and `raw/global-*.jsonl.gz` hold the hourly raw data for replay and model training.

## Code map

| File | Job |
|---|---|
| `lagcatcher/feeds.py` | Websocket feeds and message parsers (Wallex Centrifugo, Bybit, KuCoin, Gate, OKX, Binance) |
| `lagcatcher/fairvalue.py` | Global median quote and the rolling premium |
| `lagcatcher/shadow.py` | Paper strategies, fill rules, fees, PnL |
| `lagcatcher/episodes.py` | Gap episodes |
| `lagcatcher/engine.py` | Wiring, timers, status line |
| `lagcatcher/recorder.py` | Raw data and event files |
| `lagcatcher/report.py` | The report tables |

Known facts about the endpoints (2026-10-21):

* The Wallex websocket answers HTTP 503 unless the client disables compression and sends a browser User-Agent. Sometimes it returns 503 for a short time anyway. The feed then connects again.
* A Wallex trade with `isBuyOrder=true` means a resting buy order was the maker, so a seller took the trade.
