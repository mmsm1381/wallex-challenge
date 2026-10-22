# Lagcatcher MVP (live shadow test) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Answer "is the Wallex lag edge real?" on live data. Paper-trade the two entry styles (taker on a gap, resting bid below fair value) against real Wallex order books and trades, and record everything for later replay.

**Architecture:** One asyncio process. Websocket feeds (Wallex Centrifugo v2, Bybit, OKX, KuCoin, Gate) update in-memory state. A fair-value module turns global mids into a Wallex fair value, with a rolling premium per market. Paper strategies and a gap-episode tracker react to every update. A recorder writes raw data and paper events to disk. A report CLI summarizes them. No real orders are sent.

**Tech Stack:** Python 3.12, asyncio, `websockets` (the only runtime dependency), `tomllib` config, `pytest` for tests.

**Spec:** the conversation's final plan v2 (spot only, long only, real fees) and the recorder/shadow test agreed on 2026-10-21. The decisions are copied into Global Constraints below.

## Global Constraints

- No real orders and no API key. This is paper trading only.
- Fees in percent per side: TMN maker 0.25, taker 0.30; USDT maker 0.10, taker 0.13.
- Markets: USDT = BTC ETH XRP SOL PEPE DOGE BNB ADA; TMN = BTC ETH XRP SOL PEPE DOGE BNB ADA SHIB TRX; USDTTMN is always subscribed. None of these are on the organizers' blocked list.
- Wallex websocket: `wss://api.wallex.ir/connection/websocket`.
  - It requires `compression=None` and a browser User-Agent, or the server answers HTTP 503.
  - Reply `{}` to `{}` within 8 s.
  - Max 64 channels per connection.
  - A depth push is a full top-50 side snapshot under `data.message`.
  - Trade `isBuyOrder=true` means the buy order was the maker (a sell-initiated trade).
- Global venues: Bybit `orderbook.1`, OKX `bbo-tbt`, KuCoin `/spotMarket/level1` (bullet token), Gate `spot.book_ticker`. Always pass `proxy=None`; the machine has a dead `ALL_PROXY`.
- Paper notional is 1,000,000 toman per trade (USDT pairs convert it at the USDTTMN mid). At most one position per market per strategy.
- The order budget per strategy is 15 actions per 10 s (Wallex allows 20).

## Review Focus

1. A feed silently stops (no exception, no messages). Fair value must go stale and no new entries may open. Test: `test_fair_value_none_when_quotes_stale`.
2. A trade at exactly our resting price must not count as a fill (queue position is unknown). Tests: `test_resting_bid_no_fill_at_equal_price`, `test_exit_no_fill_at_equal_price`.
3. Only part of our size is on the book at our limit. Fill at the VWAP of the levels at or below the limit, and miss if less than 50% is there. Test: `test_taker_partial_depth`.
4. The Wallex message shape varies (with or without the `message` wrapper, dict or list trades). The parsers must accept all of them. Test: `test_parse_wallex_shapes`.
5. A USDT pair's PnL must be converted to toman at the exit-time USDTTMN mid. Test: `test_pnl_usdt_pair_in_toman`.

---

### Task 1: Config + fair value

**Files:** Create `pyproject.toml`, `config.toml`, `lagcatcher/__init__.py`, `lagcatcher/config.py`, `lagcatcher/fairvalue.py`, `tests/test_fairvalue.py`

**Interfaces (produces):**
- `load_config(path: str) -> dict`: plain dict from TOML, with defaults filled in.
- `fee_rates(quote: str, cfg: dict) -> tuple[float, float]`: (maker, taker) as fractions.
- `class Quotes`: `update(venue, coin, bid, ask, ts)` and `global_mid(coin, now) -> float | None`. The latter is the median of venue mids no older than `max_quote_age_s`, or None if fewer than `min_venues` are fresh.
- `class Premium(window_s, min_samples)`: `add(ts, log_basis)` and `value(now) -> float | None`, the median over the window, or None before warm-up.

- [ ] Tests:
  - `test_global_mid_median_of_fresh` (three venues, one stale, gives the median of two)
  - `test_fair_value_none_when_quotes_stale`
  - `test_premium_warmup_and_window` (None below min_samples; median after; samples older than the window are dropped)
- [ ] Implement; `pytest tests/test_fairvalue.py` passes.

### Task 2: Paper strategies + episodes

**Files:** Create `lagcatcher/shadow.py`, `lagcatcher/episodes.py`, `tests/test_shadow.py`, `tests/test_episodes.py`

**Interfaces:**
- Consumes `fee_rates`.
- `Book` holds `bids`/`asks` as lists of `(price, qty)`, best first, plus `ts`.
- `class TokenBucket(capacity=15, per_s=10)`: `take(now) -> bool`.
- Every strategy has `on_tick(m, now, book, fv, usdttmn)`, `on_trade(m, now, price, qty, buyer_maker, book, usdttmn)` and `on_timer(now, books, fvs, usdttmn)`. Each returns a list of event dicts (`type` is one of `signal`, `fill`, `miss`, `exit`).
- `TakerStrategy(name, latency_s, cfg)`:
  - Signal when `1 - best_ask/fv >= max(min_gap_pct, taker + maker + buffer_pct)/100`. The limit is the ask at signal time.
  - At `t + latency`, fill at the VWAP of asks at or below the limit, if at least 50% of the size is there; otherwise record a miss.
- `RestingBidStrategy(name, latency_s, cfg)`:
  - Bid = `fv * (1 - (2*maker + buffer_pct)/100)`. It is not placed if that would cross the best ask. It is requoted when it moves more than `requote_bps`, and the new price is live only after the latency.
  - Fill at our bid when a sell-initiated trade (`buyer_maker=True`) prints strictly below it, or when the best ask falls to or below it.
- Shared exit:
  - Resting sell at `fv`. Fill on a buy-initiated trade strictly above it, or when best bid ≥ target. Maker fee.
  - Stop when best bid ≤ entry × (1 − stop_pct/100). Time stop at `max_hold_s`. Both exit at the best bid with the taker fee.
  - Exit events carry `net_quote`, `net_pct` and `net_tmn`.
- `class Episodes(thresholds_pct)`: `update(m, now, gap, depth_tmn) -> list[dict]`. Returns the closed episodes, with start, duration_s, max_gap and depth at max.

- [ ] Tests:
  - `test_taker_fills_when_ask_still_there`
  - `test_taker_misses_when_ask_moved`
  - `test_taker_needs_gap_above_hurdle`
  - `test_taker_partial_depth`
  - `test_resting_bid_fills_on_sell_through`
  - `test_resting_bid_no_fill_at_equal_price`
  - `test_resting_requote_old_price_live_until_latency`
  - `test_exit_maker_fill_and_fee`
  - `test_exit_no_fill_at_equal_price`
  - `test_stop_and_time_exit_taker`
  - `test_pnl_usdt_pair_in_toman`
  - `test_token_bucket`
  - `test_episode_duration_and_max_gap`
- [ ] Implement; `pytest` passes.

### Task 3: Feeds, recorder, engine, CLI

**Files:** Create `lagcatcher/feeds.py`, `lagcatcher/recorder.py`, `lagcatcher/engine.py`, `lagcatcher/__main__.py`, `tests/test_feeds.py`

**Interfaces:**
- `parse_wallex(line: str) -> tuple | None` returns one of:
  - `('book', market, side, levels)`
  - `('trade', market, price, qty, buyer_maker, trade_ts)`
  - `('ping',)`
  - `None`
- One `run_<venue>(coins, on_quote, stats)` coroutine per global venue, plus `run_wallex(markets, on_book, on_trade, stats)`. Each reconnects with backoff and puts message counts and the last message time into `stats[venue]`.
- `Recorder(out_dir, levels)`:
  - Hourly gzip JSONL raw streams: `wallex_book`, `wallex_trade`, `global` (throttled to 250 ms per venue/coin).
  - Plain JSONL `events.jsonl` and `episodes.jsonl`.
- `Engine(cfg)` wires everything together:
  - Fair value = global mid × USDTTMN mid (TMN markets), times exp(premium).
  - Premium samples every 5 s.
  - A 100 ms timer runs the strategies.
  - A status line every 60 s.
- CLI: `python -m lagcatcher run|check|report [--config] [--out]`.

- [ ] Test `test_parse_wallex_shapes` (depth with and without `message`, dict and list trades, ping).
- [ ] `python -m lagcatcher check` shows Wallex plus at least 2 venues OK.
- [ ] `python -m lagcatcher run` for at least 20 minutes locally. Status lines show live books and fair values; `report` prints tables.

### Task 4: Report + deploy

**Files:** Create `lagcatcher/report.py`, `deploy/install.sh`, `deploy/lagcatcher.service`, `README.md`

- Report (stdlib only):
  - per strategy and market: signals, fills, fill rate, trades, win %, median/mean net %, total net toman and as % of 5M
  - episodes per market and threshold: count per hour, median/p90 duration, median depth
- `install.sh` creates the venv, installs, and writes and enables the systemd unit, which runs as the invoking user and restarts always.
- README covers: what the MVP measures, local run, server deploy, reading the report, and what counts as "edge is real". That bar is median net per filled trade above +0.05% at a realistic latency, with fill rate and depth that support about 10+ trades/day.

## Self-review notes
- Commits are skipped because the user has not asked for them. The plan otherwise follows the skill.
- Every spec item maps to a task: fees and markets in Task 1 config, taker and resting fills in Task 2, recording in Task 3, the report and server run in Task 4.
