# Implementation plan: Wallex sweep bot

This plan turns `DESIGN.md` into tasks. Do the tasks in order. Each task names its files, its tests and its done condition. Mark a task done in this file (`[x]`) in the same commit as its code.

Rules for every task:

1. Read `AGENTS.md` before you start.
2. Write the tests first, then the code. Use the given/when/then style of `tests/`.
3. Run `.venv/bin/python -m pytest -q`. All tests must pass before a commit.
4. Do not change a number from `RESEARCH.md` (depth, filter thresholds, limits) without a new backtest. Record the new backtest in `RESEARCH.md`.

## Milestone 0: repository and data (done or open)

- [x] 0.1 Put the project in git. Keep data and secrets out (`.gitignore`).
- [x] 0.2 Write `README.md`, `AGENTS.md`, `docs/RESEARCH.md`, `docs/DESIGN.md`, this plan.
- [ ] 0.3 Copy the lagcatcher raw files from the server to a local folder (see `research/README.md` section 3). The recording runs until about 2026-10-23 04:52 UTC. Ask the team before a new run on the shared server.
- [ ] 0.4 Re-run `research/scripts/sweep_study.py` with the new raw files and `replay_deep_bids.py` on all recorded hours. Add the new numbers to `RESEARCH.md` section 3.

## Milestone 1: paper sweep bot (about 1 day)

- [x] 1.1 `sweepbot/config.py` and `config.sweep.toml`: markets, default plan (5 markets, depth 1.3%), hard limits (DESIGN section 7), filter thresholds (section 6), mode. Tests: defaults load, TOML override merges, invalid values raise.
- [x] 1.2 `sweepbot/market.py`: wrap the fair value and premium of `lagcatcher.engine.Engine` without the paper strategies. Add the implied USDT rate (median coin TMN mid / coin USDT mid) for the nightly USDTTMN close. Tests: fair value of a toman market, implied rate when USDTTMN is frozen.
- [x] 1.3 `sweepbot/filters.py`: global drop filter and FX shock filter, with start and end events. Tests: a 0.3% Binance drop trips the global filter. A 0.5% USDTTMN move in 5 minutes trips the FX filter. The filters clear after their wait time.
- [x] 1.4 `sweepbot/broker.py`: `Broker` protocol and `PaperBroker` with the lagcatcher fill rules (strictly below for a bid, strictly above for a sell, or a book cross). Tests: the fill rules, part fills, cancel.
- [ ] 1.5 `sweepbot/slots.py` and `sweepbot/exits.py`: the slot state machine of DESIGN section 5, the requote rule (0.2%), the order budget, the exit at fair value, the time limit and the cooldowns. Tests: one test per state change in the diagram.
- [x] 1.6 `sweepbot/risk.py`: hard limits, daily loss stop, equity floor, end-of-challenge schedule. Tests: one test per limit.
- [x] 1.7 `sweepbot/store.py`: SQLite schema of DESIGN section 10. Tests: write and read back each table.
- [ ] 1.8 `sweepbot/__main__.py`: `replay` mode on lagcatcher raw files. Done when: the replay of 2026-10-22 07:00 to 07:10 UTC gives one BTCTMN fill near 21.71B and an exit at fair value, and the replay of all recorded hours matches `replay_deep_bids.py` at the same depth within one trade per market.
- [ ] 1.9 `paper` mode on live feeds, as a transient systemd unit next to lagcatcher (CPU 50%, memory 400M). Done when: 24 h paper run with no crash, and its fills match a replay of the same hours.

## Milestone 2: Wallex order client (about 0.5 day)

- [ ] 2.1 Read the Wallex API documentation. Record in `docs/WALLEX_API.md`: base URL, auth header, order place, cancel, open orders, balances, price and quantity ticks per market, rate limits, error codes. Do not guess any of these.
- [ ] 2.2 Check that the order API accepts requests from the server. If it does not, write the options in `docs/WALLEX_API.md` and ask the team (a server in Iran is the fallback).
- [ ] 2.3 `WallexBroker` in `sweepbot/broker.py`. The API key comes from the environment variable `WALLEX_API_KEY`. Tests with a fake HTTP transport: place, cancel, part fill, errors, retries.
- [ ] 2.4 Live smoke test, with a team member present: place one bid of the minimum size far below the market, read it back, cancel it. Record the result in `docs/WALLEX_API.md`.

## Milestone 3: AI part (about 1 to 1.5 days)

- [x] 3.1 `sweepbot/ai/schema.py` and `sweepbot/ai/validator.py`: the `Plan` schema of DESIGN section 8.3 and the clamp rules. Tests: one test per limit, a valid plan passes unchanged.
- [ ] 3.2 `sweepbot/ai/provider.py` and `sweepbot/ai/claude.py`: `ModelProvider` protocol and the Claude adapter (`anthropic` SDK, `claude-opus-5-5`, structured output, web search tool, 60 s timeout). The key comes from `ANTHROPIC_API_KEY`. Log tokens and cost in `ai_calls`. Tests with a fake provider. One manual call with the real key.
- [ ] 3.3 `sweepbot/ai/supervisor.py`: state snapshot, timer (15 minutes), alert triggers, fallback to the last valid plan, then to the default plan. Tests: timeout keeps the last plan, two failures use the default plan, an alert starts a call.
- [ ] 3.4 Hold or cut: a request after 5 minutes without an exit fill. The executor acts on "cut" at once. Tests with a fake provider.
- [ ] 3.5 Decision log and daily review: one `logs` row per trade and per plan change, one review per day. `sweepbot/report.py` writes the daily Markdown report.
- [ ] 3.6 `sweepbot/twin.py`: the no-AI twin on `PaperBroker` with the default plan. The daily report shows the bot and the twin side by side.
- [ ] 3.7 Spending limit: stop model calls when the cost in `ai_calls` reaches a limit from the config. Also set a limit in the Claude Console.
- [ ] 3.8 24 h paper run with the AI part on. Done when: plans, hold-or-cut answers and logs are complete, and the cost per day is as expected.

## Milestone 4: rehearsal and challenge

- [ ] 4.1 Rehearsal: live mode with the smallest allowed size for 12 h. Done when: no errors, all orders known, logs complete.
- [ ] 4.2 Challenge: live mode for 3 days. A team member checks the daily report every day.
- [ ] 4.3 End of the challenge: all coins sold, all USDT converted to toman before the last USDTTMN close.

## Milestone 5: deliverables

- [ ] 5.1 Final report in `docs/RESULTS.md`: return, drawdown, trades, the bot against the twin, AI cost.
- [ ] 5.2 Decision logs exported from SQLite to `docs/logs/`.
- [ ] 5.3 Repository clean: README up to date, tests pass, no secrets.
- [ ] 5.4 5-minute demo video.

## Open questions for the team

* When does the challenge start? This sets the deadline for milestones 1 to 3.
* Does the Wallex order API accept requests from the server outside Iran?
* What are the order limits of the challenge account, and do open orders lock funds?
* What are the liquidation limit and the daily fee of the credit package?
* Do we add the USDT overlay (hold part of the equity in USDT)? It can add return, but also drawdown.
* Who owns which milestone?
