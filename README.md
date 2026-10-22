# Wallex sweep bot

A trading bot for the Wallex live-trading challenge of the StarCoach Agentic AI hackathon. The bot keeps buy orders resting far below fair value on Wallex. When a large market sell sweeps the order book, a bid fills, and the bot sells back at fair value seconds or minutes later. Fast code places the orders. Claude (or another model) sets the plan, watches the news, decides on trades that do not recover, and writes the decision log.

## Status

| Part | State |
|---|---|
| Research: lag edge | Done. No profit after fees. Dropped. |
| Research: sweep bids | Done. 60-day backtest is positive with two filters. See `docs/RESEARCH.md`. |
| `lagcatcher` (feeds, fair value, recorder, replay, paper tests) | Built, 63 tests. Records live data on the shadow-test server. |
| `sweepbot` (the bot) | Paper bot, replay, risk guard, AI supervisor (Claude), twin and daily report are built and tested. Open: runs on the server, the server access check (task 2.2). The live smoke test passed. See `docs/IMPLEMENTATION_PLAN.md`. |

## Key numbers

60-day candle study, 2026-08-22 to 2026-10-22, bid 1.3% below fair value, 1,000,000 toman per trade, real fees, both filters:

| Fills per day | Win | Median per trade | Net per day | Max drawdown |
|---|---|---|---|---|
| 5.0 | 81% | +0.67% | +25.4k toman | -11k toman |

With only 5 bids at the same time (funds limit), out of sample on the last 31 days: +11.6k toman a day, maximum drawdown -16k. Details and limits of the evidence: `docs/RESEARCH.md` section 3.

## The challenge

* 3 days of live spot trading on a Wallex account of 5,000,000 toman. Margin is disabled.
* Fees: toman markets maker 0.25%, taker 0.30%. USDT markets maker 0.10%, taker 0.13%.
* Score (300 points): return 90 (only if positive), strategy logic 40, real AI use 35, drawdown 30, risk management 30, reproducibility 25, backtest and shadow test 20, activity 15, stability 15.
* Deliverables: decision logs, a GitHub repo with an MD document, a 5-minute demo video.

## Documents

| File | Content |
|---|---|
| `AGENTS.md` | Rules and workflow for agents and contributors. Read it first. |
| `docs/IMPLEMENTATION_PLAN.md` | Task list with done conditions. The next open task is the place to start. |
| `docs/DESIGN.md` | Architecture, state machine, filters, risk limits, AI interface, schemas, stack |
| `docs/RESEARCH.md` | All results with their scripts and data |
| `docs/plan/sweep_plan.html` | Team presentation of the plan with charts (open it in a browser) |
| `docs/LAGCATCHER.md` | The `lagcatcher` tool: what it measures, how to run it |
| `docs/HANDOFF.md` | History log of the project and the shadow-test server |
| `research/README.md` | Research scripts and how to make the data again |

## Quick start

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/python -m pytest -q                 # all tests
.venv/bin/python -m lagcatcher check          # 20 s feed check
.venv/bin/python -m lagcatcher run            # live paper test, Ctrl+C to stop
.venv/bin/python -m lagcatcher report         # results of the paper test
```

Sweep bot:

```bash
.venv/bin/python -m sweepbot replay --raw data/raw --markets BTCTMN \
    --start 2026-10-22T07:00:00Z --end 2026-10-22T07:10:00Z   # replay on raw files
.venv/bin/python -m sweepbot run --mode paper                 # live data, paper broker, twin
.venv/bin/python -m sweepbot run --mode paper --ai            # with the Claude supervisor
.venv/bin/python -m sweepbot report --day 2026-10-23          # daily report (Tehran day)
.venv/bin/python -m sweepbot smoke --market BTCTMN            # task 2.4: one far bid, read back, cancel
.venv/bin/python -m sweepbot run --mode live --confirm-live   # real orders, team member present
```

`config.sweep.toml` holds the sweep bot settings. The defaults are in `sweepbot/config.py`.

Reproduce the main backtest (standard library only, about 8 minutes, network needed):

```bash
mkdir -p research/data/sweep
python3 -I research/scripts/sweep_fetch.py research/data/sweep
python3 -I research/scripts/sweep_study.py research/data/sweep
```

## Layout

```
lagcatcher/        live feeds, fair value, paper strategies, recorder, replay, report
sweepbot/          the sweep bot: market, filters, slots, broker, risk, store, AI, runner
tests/             pytest tests of lagcatcher and sweepbot
research/scripts/  research and backtest scripts
deploy/            server scripts for the shadow test
docs/              design, plan, research, history, team page
config.toml        local config of lagcatcher
config.server.toml server config of lagcatcher
config.sweep.toml  config of the sweep bot
```

`data/` and `research/data/` hold recorded and downloaded data. They are not in git.

## Secrets

The bot needs `WALLEX_API_KEY`, `WALLEX_SUB_ACCOUNT_ID` (the challenge trade-credit account) and `ANTHROPIC_API_KEY`. Put them in the environment or in a `.env` file. Git ignores `.env`. Never commit a key.
