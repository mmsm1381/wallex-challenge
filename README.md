# Wallex sweep bot

A trading bot for the Wallex live-trading challenge of the StarCoach Agentic AI hackathon. The bot keeps buy orders resting far below fair value on Wallex. When a large market sell sweeps the order book, a bid fills, and the bot sells back at fair value seconds or minutes later. Fast code places the orders. Claude (or another model) sets the plan, watches the news, decides on trades that do not recover, and writes the decision log.

## Status

| Part | State |
|---|---|
| Research: lag edge | Done. No profit after fees. Dropped. |
| Research: sweep bids | Done. 60-day backtest is positive with two filters. See `docs/RESEARCH.md`. |
| `lagcatcher` (feeds, fair value, recorder, replay, paper tests) | Built, 63 tests. Records live data on the shadow-test server. |
| `sweepbot` (the bot) | Designed (`docs/DESIGN.md`). Not built. Next task: `docs/IMPLEMENTATION_PLAN.md` milestone 1. |

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
.venv/bin/python -m pytest -q                 # 63 tests
.venv/bin/python -m lagcatcher check          # 20 s feed check
.venv/bin/python -m lagcatcher run            # live paper test, Ctrl+C to stop
.venv/bin/python -m lagcatcher report         # results of the paper test
```

Reproduce the main backtest (standard library only, about 8 minutes, network needed):

```bash
mkdir -p research/data/sweep
python3 -I research/scripts/sweep_fetch.py research/data/sweep
python3 -I research/scripts/sweep_study.py research/data/sweep
```

## Layout

```
lagcatcher/        live feeds, fair value, paper strategies, recorder, replay, report
tests/             pytest tests of lagcatcher
research/scripts/  research and backtest scripts
deploy/            server scripts for the shadow test
docs/              design, plan, research, history, team page
config.toml        local config of lagcatcher
config.server.toml server config of lagcatcher
```

`data/` and `research/data/` hold recorded and downloaded data. They are not in git.

## Secrets

The bot will need `WALLEX_API_KEY` and `ANTHROPIC_API_KEY`. Put them in the environment or in a `.env` file. Git ignores `.env`. Never commit a key.
